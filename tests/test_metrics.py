"""
Step 8 지표·답글 감시·쿠팡 적재 테스트. Threads API 는 가짜로 바꾼다.

8장 체크리스트
  - 자동 정지 5개 조건을 각각 발생시켜 발행이 멈추고 알림이 오는지 확인
  - 실제 1건 발행 후 threads_media_id 저장, 24시간 뒤 인사이트 수집 확인
    → 여기서는 가짜 API 로 흐름을 증명한다. 실제 확인은 DRY_RUN 을 사람이 끈 뒤에만 가능하다.
"""
import json
import types as pytypes
from datetime import datetime, timedelta, timezone

import pytest

import autostop
import import_coupang
import metrics
import pipeline
import publisher
import reviewer as reviewer_mod
import watch_replies
from notify import CollectingNotifier
from settings import KST, ConfigError
from store import MemoryStore
from test_pipeline import smart_llm

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=KST).astimezone(timezone.utc)
TODAY = NOW.date()


class FakeThreads:
    """발행·인사이트·답글을 흉내 낸다."""

    def __init__(self, replies=None, insights=None, fail_insights=()):
        self.replies = replies or {}
        self.values = insights or {"views": 321, "likes": 12, "replies": 3, "reposts": 1, "quotes": 0, "shares": None}
        self.fail_insights = set(fail_insights)
        self.calls, self.n = [], 0

    def create_container(self, params):
        self.calls.append(("create", params))
        self.n += 1
        return f"c{self.n}"

    def container_status(self, cid):
        return "FINISHED", None

    def publish(self, cid):
        self.calls.append(("publish", cid))
        return f"media-{cid}"

    def insights(self, media_id):
        self.calls.append(("insights", media_id))
        if media_id in self.fail_insights:
            raise publisher.ThreadsApiError("HTTP 500")
        return dict(self.values)

    def conversation(self, media_id):
        self.calls.append(("conversation", media_id))
        return list(self.replies.get(media_id, []))


def published(make_post, store, hours_ago, media_id):
    return store.insert_post(make_post(status="published", threads_media_id=media_id,
                                       published_at=(NOW - timedelta(hours=hours_ago)).isoformat()))


# ── 인사이트 ────────────────────────────────────────────────────────────

def test_parse_insights_formats():
    data = {"data": [
        {"name": "views", "values": [{"value": 1500}]},
        {"name": "likes", "total_value": {"value": 40}},
        {"name": "shares", "values": []},
        {"name": "unknown", "values": [{"value": 9}]},
    ]}
    assert publisher.parse_insights(data) == {"views": 1500, "likes": 40, "replies": None, "reposts": None,
                                              "quotes": None, "shares": None}


def test_checkpoints_24_and_72(make_post):
    store = MemoryStore()
    young = published(make_post, store, 10, "m-young")
    published(make_post, store, 30, "m-mid")
    published(make_post, store, 80, "m-old")
    due = metrics.due_checkpoints(store.list_posts(["published"]), set(), NOW)
    assert {(p["threads_media_id"], h) for p, h in due} == {("m-mid", 24), ("m-old", 24), ("m-old", 72)}
    assert young["threads_media_id"] not in {p["threads_media_id"] for p, _ in due}


def test_collect_saves_once_per_checkpoint(make_post):
    store = MemoryStore()
    published(make_post, store, 30, "m-1")
    api = FakeThreads()
    rows = metrics.collect(store, api, NOW)
    assert rows[0]["views"] == 321 and rows[0]["shares"] is None and rows[0]["hours_since_publish"] == 24
    assert metrics.collect(store, api, NOW + timedelta(hours=1)) == []            # 같은 체크포인트는 다시 안 모음
    assert len(store.metrics) == 1


def test_collect_continues_after_one_failure(make_post):
    store = MemoryStore()
    published(make_post, store, 30, "m-bad")
    published(make_post, store, 30, "m-good")
    rows = metrics.collect(store, FakeThreads(fail_insights={"m-bad"}), NOW)
    assert [r["threads_media_id"] for r in rows] == ["m-good"]


def test_collect_dry_run_saves_nothing(make_post):
    store = MemoryStore()
    published(make_post, store, 30, "m-1")
    assert len(metrics.collect(store, FakeThreads(), NOW, save=False)) == 1 and store.metrics == []


def test_publish_then_collect_insights_after_24h(cfg, make_post):
    # 8장: 발행 → threads_media_id 저장 → 24시간 뒤 인사이트 수집 (가짜 API)
    store, notifier, api = MemoryStore(), CollectingNotifier(), FakeThreads()
    post = store.insert_post(make_post(scheduled_at=(NOW - timedelta(minutes=5)).isoformat()))
    result = publisher.run_once(store, cfg, env={"DRY_RUN": "false"}, api=api, link_checker=publisher.check_link,
                                notifier=notifier, now=NOW, exam_status=(True, "합격"), sleep=lambda _: None)
    media_id = store.get_post(post["id"])["threads_media_id"]
    assert result["action"] == "published" and media_id == "media-c1"
    assert metrics.collect(store, api, NOW + timedelta(hours=23)) == []            # 아직 24시간 전
    rows = metrics.collect(store, api, NOW + timedelta(hours=25))
    assert rows[0]["threads_media_id"] == media_id and rows[0]["views"] == 321
    assert store.latest_metrics([media_id])[media_id]["views"] == 321             # 일일 요약이 읽는 값


# ── 답글 감시 ────────────────────────────────────────────────────────────

def test_stop_word_reply_stops(make_post):
    store, notifier = MemoryStore(), CollectingNotifier()
    published(make_post, store, 5, "m-1")
    api = FakeThreads(replies={"m-1": [{"id": "r1", "text": "광고충 계정 신고합니다", "username": "x"}]})
    result = watch_replies.watch(store, api, notifier, NOW)
    assert result["stopped"] and autostop.is_stopped(store)
    assert "광고충" in notifier.sent[-1]


def test_own_and_old_replies_ignored(make_post):
    store, notifier = MemoryStore(), CollectingNotifier()
    published(make_post, store, 5, "m-new")
    published(make_post, store, 80, "m-old")                                        # 72시간 밖
    api = FakeThreads(replies={
        "m-new": [{"id": "mine", "text": "[광고] 스팸 아님", "is_reply_owned_by_me": True}],
        "m-old": [{"id": "r-old", "text": "스팸"}],
    })
    result = watch_replies.watch(store, api, notifier, NOW)
    assert not result["stopped"] and result["new_replies"] == 0
    assert ("conversation", "m-old") not in api.calls


def test_seen_reply_does_not_stop_again_after_clear(make_post):
    store, notifier = MemoryStore(), CollectingNotifier()
    published(make_post, store, 5, "m-1")
    api = FakeThreads(replies={"m-1": [{"id": "r1", "text": "사기 아닌가요"}]})
    watch_replies.watch(store, api, notifier, NOW)
    autostop.clear(store, "악성 답글 아님 확인", "운영자")
    watch_replies.watch(store, api, notifier, NOW + timedelta(hours=1))
    assert not autostop.is_stopped(store)


# ── 8장: 자동 정지 5개 조건 → 발행 중단 + 알림 ───────────────────────────────────

def _run_publisher(store, cfg, notifier, reader=None, exam=(True, "합격")):
    api = FakeThreads()
    result = publisher.run_once(store, cfg, env={"DRY_RUN": "false"}, api=api, link_checker=publisher.check_link,
                                notifier=notifier, now=NOW, exam_status=exam, sleep=lambda _: None,
                                reply_reader=reader)
    return result, api


def _cond1_reply(store, cfg, notifier, make_post, tariff, products):
    published(make_post, store, 5, "m-r")
    return {"reader": FakeThreads(replies={"m-r": [{"id": "r", "text": "허위 정보네요"}]})}


def _cond2_publish_failures(store, cfg, notifier, make_post, tariff, products):
    autostop.record_publish_result(store, notifier, False)
    autostop.record_publish_result(store, notifier, False)
    return {}


def _cond3_held_ratio(store, cfg, notifier, make_post, tariff, products):
    items = {c: {"verdict": "pass", "reason": "", "hook_only": False} for c in reviewer_mod.ITEM_CODES}
    items["R3"]["verdict"] = "uncertain"
    pipeline.run_generate(store, cfg, tariff, products, complete=smart_llm,
                          review_call=lambda s, u: ("end_turn", json.dumps(items)), notifier=notifier,
                          now=NOW - timedelta(hours=13), env={})
    return {}


def _cond4_format_errors(store, cfg, notifier, make_post, tariff, products):
    pipeline.run_generate(store, cfg, tariff, products, complete=smart_llm,
                          review_call=lambda s, u: ("end_turn", "형식 오류"), notifier=notifier,
                          now=NOW - timedelta(hours=13), env={})
    return {}


def _cond5_exam_case_added(store, cfg, notifier, make_post, tariff, products):
    return {"exam": (False, "시험 케이스가 바뀌었습니다(케이스 추가 포함). 재시험이 필요합니다")}


@pytest.mark.parametrize("trigger, message", [
    (_cond1_reply, "허위"),
    (_cond2_publish_failures, "발행 실패 2회 연속"),
    (_cond3_held_ratio, "held 비율"),
    (_cond4_format_errors, "형식 오류 3회 연속"),
    (_cond5_exam_case_added, "재시험"),
])
def test_each_auto_stop_condition_halts_publishing(cfg, make_post, tariff, products, trigger, message):
    store, notifier = MemoryStore(), CollectingNotifier()
    # 발행할 글이 준비돼 있는 상태에서
    store.insert_post(make_post(scheduled_at=(NOW - timedelta(minutes=5)).isoformat()))
    extra = trigger(store, cfg, notifier, make_post, tariff, products)
    result, api = _run_publisher(store, cfg, notifier, extra.get("reader"), extra.get("exam", (True, "합격")))
    assert result["action"] == "auto_stopped"
    assert not any(c[0] in ("create", "publish") for c in api.calls)              # 발행 요청 없음
    assert any(message in m for m in notifier.sent), notifier.sent                 # 알림이 왔다


# ── 실제 API 클라이언트 (읽기) ──────────────────────────────────────────────────

class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw["params"]))
        data = self.responses.pop(0)
        return pytypes.SimpleNamespace(status_code=200, json=lambda: data, text=json.dumps(data))


def test_conversation_pagination_and_fields():
    http = FakeHttp([
        {"data": [{"id": "1"}], "paging": {"cursors": {"after": "A"}, "next": "https://example"}},
        {"data": [{"id": "2"}], "paging": {"cursors": {"after": "B"}}},
    ])
    replies = publisher.ThreadsApi("123", "T", session=http).conversation("m1")
    assert [r["id"] for r in replies] == ["1", "2"]
    assert http.calls[0][0] == "https://graph.threads.net/v1.0/m1/conversation"
    assert http.calls[0][1]["fields"] == "id,text,username,permalink,timestamp,is_reply_owned_by_me"
    assert http.calls[1][1]["after"] == "A"


def test_insights_request():
    http = FakeHttp([{"data": [{"name": "views", "values": [{"value": 7}]}]}])
    values = publisher.ThreadsApi("123", "T", session=http).insights("m1")
    assert values["views"] == 7
    assert http.calls[0][1]["metric"] == "views,likes,replies,reposts,quotes,shares"


# ── 쿠팡 리포트 ─────────────────────────────────────────────────────────────

def test_korean_headers_and_money_format():
    rows = import_coupang.parse_rows(today=TODAY, rows=[
        {"날짜": "2026.11.01", "클릭수": "1,204", "주문수": "3", "수익": "12,340원"},
        {"날짜": "2026-11-02", "클릭수": "0", "주문수": "0", "수익": "0"},
    ])
    assert rows[0] == {"date": "2026-11-01", "clicks": 1204, "orders": 3, "commission": 12340}


def test_upsert_overwrites_same_date():
    store = MemoryStore()
    store.upsert_coupang([{"date": "2026-11-01", "clicks": 1, "orders": 0, "commission": 0}])
    store.upsert_coupang([{"date": "2026-11-01", "clicks": 5, "orders": 1, "commission": 900}])
    assert store.coupang["2026-11-01"]["clicks"] == 5


@pytest.mark.parametrize("rows, message", [
    ([{"date": "2026-11-01", "clicks": "1", "orders": "0"}], "commission"),
    ([{"date": "2026-11-01", "clicks": "-1", "orders": "0", "commission": "0"}], "정수"),
    ([{"date": "2026-11-01", "clicks": "1", "orders": "0", "commission": "0"}] * 2, "두 번"),
    ([{"date": "2099-01-01", "clicks": "1", "orders": "0", "commission": "0"}], "미래"),
    ([], "행이 없습니다"),
])
def test_bad_coupang_csv_rejected(rows, message):
    with pytest.raises(ConfigError, match=message):
        import_coupang.parse_rows(rows, today=TODAY)
