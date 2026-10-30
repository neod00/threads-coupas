"""
Step 6 발행기 테스트. Threads API 는 가짜로 바꾼다 (실제 호출 없음).

8장 체크리스트 (발행)
  - 승인되지 않은 글 발행 시도 → 거부
  - 시험지 불합격 상태에서 발행 시도 → 거부
  - 일일 최대 개수·최소 간격 초과 시도 → 거부
  - AUTO_PUBLISH_DISABLED=true → 발행 작업 즉시 종료
  - 링크 확인 실패 → held
  - DRY_RUN 로그에 컨테이너 생성·발행 요청 내용이 정확히 찍힘
  - 같은 글 두 번 발행 시도 → 두 번째는 건너뜀
  (실제 1건 발행 후 threads_media_id 저장·24시간 뒤 인사이트 수집은 DRY_RUN 해제 후 사람이 확인)
8장 자동 정지: 발행 실패 2회 연속 → 발행이 실제로 멈추고 알림
"""
import json
import logging
import types as pytypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autostop
import publisher
from notify import CollectingNotifier
from publisher import NetworkError, PublishRefused, ThreadsApiError
from settings import KST, PROJECT_ROOT
from store import MemoryStore

NOW = datetime(2026, 11, 3, 8, 15, tzinfo=KST)          # 08:10 슬롯 직후 실행
REAL = {"DRY_RUN": "false"}
DRY = {}
EXAM_OK = (True, "합격")


class FakeApi:
    """호출을 기록한다. fail={"create": [예외, ...], "publish": [...], "status": [...]} 로 차례대로 실패시킨다."""

    def __init__(self, fail=None, status="FINISHED"):
        self.calls, self.fail, self.status, self.n = [], fail or {}, status, 0

    def _maybe_fail(self, kind):
        queue = self.fail.get(kind) or []
        if queue:
            raise queue.pop(0)

    def create_container(self, params):
        self.calls.append(("create", params))
        self._maybe_fail("create")
        self.n += 1
        return f"c{self.n}"

    def container_status(self, cid):
        self.calls.append(("status", cid))
        self._maybe_fail("status")
        return self.status, None

    def publish(self, cid):
        self.calls.append(("publish", cid))
        self._maybe_fail("publish")
        return f"m-{cid}"


def link_ok(url):
    return None


@pytest.fixture
def setup(make_post):
    store, notifier = MemoryStore(), CollectingNotifier()

    def add(**kw):
        kw.setdefault("scheduled_at", datetime(2026, 11, 3, 8, 10, tzinfo=KST).astimezone(timezone.utc).isoformat())
        return store.insert_post(make_post(**kw))

    return pytypes.SimpleNamespace(store=store, notifier=notifier, add=add)


def run(s, cfg, *, env=REAL, api=None, link_checker=link_ok, exam=EXAM_OK, now=NOW):
    api = api or FakeApi()
    result = publisher.run_once(s.store, cfg, env=env, api=api, link_checker=link_checker, notifier=s.notifier,
                                now=now.astimezone(timezone.utc), exam_status=exam, sleep=lambda _: None)
    return result, api


# ── 정상 발행 ──────────────────────────────────────────────────────────────

def test_publishes_due_post_and_saves_media_id(cfg, setup):
    post = setup.add()
    result, api = run(setup, cfg)
    assert result["action"] == "published"
    saved = setup.store.get_post(post["id"])
    assert saved["status"] == "published" and saved["threads_media_id"] == "m-c1"
    assert saved["published_at"] == NOW.astimezone(timezone.utc).isoformat()
    assert [c[0] for c in api.calls] == ["create", "status", "publish"]
    assert api.calls[0][1] == {"media_type": "TEXT", "text": post["text"], "link_attachment": post["link_url"]}


def test_only_one_post_per_run(cfg, setup):
    setup.add()
    setup.add(scheduled_at=(NOW - timedelta(hours=5)).astimezone(timezone.utc).isoformat())
    result, api = run(setup, cfg)
    assert result["action"] == "published"
    assert sum(c[0] == "publish" for c in api.calls) == 1


# ── 8장: 승인되지 않은 글 → 거부 ─────────────────────────────────────────────

@pytest.mark.parametrize("status", ["draft", "held", "cancelled", "failed"])
def test_unapproved_post_refused(cfg, setup, status):
    post = setup.add(status=status)
    api = FakeApi()
    with pytest.raises(PublishRefused, match="규칙 4"):
        publisher.publish_post(post, store=setup.store, cfg=cfg, api=api, link_checker=link_ok,
                               notifier=setup.notifier, now=NOW, dry_run=False)
    assert api.calls == []
    # 정기 실행도 이런 글은 고르지 않는다
    assert run(setup, cfg)[0]["action"] == "nothing_due"


# ── 8장: 시험지 불합격 → 거부 ────────────────────────────────────────────────

def test_exam_not_passed_refuses_real_publish(cfg, setup):
    post = setup.add()
    result, api = run(setup, cfg, exam=(False, "시험지 기록이 없습니다"))
    assert result["action"] == "refused" and "시험지" in result["reason"]
    assert api.calls == []
    assert setup.store.get_post(post["id"])["status"] == "approved"


def test_exam_not_passed_still_allows_dry_run_logging(cfg, setup):
    setup.add()
    result, _ = run(setup, cfg, env=DRY, api=publisher.DryRunApi("123"), exam=(False, "시험지 기록이 없습니다"))
    assert result["action"] == "dry_run"


def test_added_exam_case_triggers_auto_stop(cfg, setup):
    setup.add()
    reason = "시험 케이스가 바뀌었습니다(케이스 추가 포함). 재시험이 필요합니다"
    result, api = run(setup, cfg, exam=(False, reason))
    assert result["action"] == "auto_stopped" and api.calls == []
    assert autostop.is_stopped(setup.store)
    assert "재시험" in setup.notifier.sent[-1]


# ── 8장: 일일 최대 개수·최소 간격 → 거부 ───────────────────────────────────────

def published_at(s, dt):
    return s.add(status="published", threads_media_id=f"old-{dt:%d%H%M}",
                 published_at=dt.astimezone(timezone.utc).isoformat(), scheduled_at=None)


def test_daily_max_refused(cfg, setup):
    # 오늘(11-03 KST) 이미 4개 발행. 마지막 06:10 은 지금(08:15)과 2시간 넘게 떨어져 있어 간격 규칙은 걸리지 않는다
    for hour, minute in ((0, 30), (2, 30), (4, 30), (6, 10)):
        published_at(setup, datetime(2026, 11, 3, hour, minute, tzinfo=KST))
    post = setup.add()
    result, api = run(setup, cfg)
    assert result["action"] == "refused" and "오늘 이미 4개" in result["reason"]
    assert api.calls == [] and setup.store.get_post(post["id"])["status"] == "approved"


def test_min_gap_refused_then_published_later(cfg, setup):
    published_at(setup, NOW - timedelta(hours=1))
    post = setup.add()
    result, api = run(setup, cfg)
    assert result["action"] == "refused" and "2시간" in result["reason"]
    assert api.calls == []
    # 간격이 지나면 발행된다
    result, _ = run(setup, cfg, now=NOW + timedelta(hours=1, minutes=1))
    assert result["action"] == "published" and result["post_id"] == post["id"]


# ── 8장: 킬 스위치 → 즉시 종료 ───────────────────────────────────────────────

class ExplodingStore:
    def __getattr__(self, name):
        raise AssertionError(f"킬 스위치인데 저장소를 건드림: {name}")


def test_kill_switch_does_nothing(cfg):
    api = FakeApi()
    result = publisher.run_once(ExplodingStore(), cfg, env={"AUTO_PUBLISH_DISABLED": "true", "DRY_RUN": "false"},
                                api=api, link_checker=link_ok, notifier=CollectingNotifier(), exam_status=EXAM_OK)
    assert result == {"action": "kill_switch"} and api.calls == []


def test_kill_switch_exits_before_loading_anything(monkeypatch):
    import settings

    monkeypatch.setenv("AUTO_PUBLISH_DISABLED", "true")
    monkeypatch.setattr(settings, "load_config", lambda *a, **k: (_ for _ in ()).throw(AssertionError("설정을 읽음")))
    assert publisher.main() == 0


# ── 8장: 링크 확인 실패 → held ──────────────────────────────────────────────

def test_link_check_failure_holds_post(cfg, setup):
    post = setup.add()
    result, api = run(setup, cfg, link_checker=lambda url: "링크 최종 도메인이 coupang.com 이 아닙니다: evil.example")
    assert result["action"] == "held"
    saved = setup.store.get_post(post["id"])
    assert saved["status"] == "held"
    assert saved["guard_log"]["attempts"][-1]["stage"] == "link"
    assert api.calls == []


def test_tip_post_skips_link_check(cfg, setup):
    setup.add(type="tip", has_link=False, link_url=None, variant=None,
              text="겨울 전기요금이 갑자기 뛰는 건 누진 구간 때문입니다\n\n지난달 고지서의 사용량부터 확인해 보세요.")
    checked = []
    result, _ = run(setup, cfg, link_checker=lambda url: checked.append(url))
    assert result["action"] == "published" and checked == []


@pytest.mark.parametrize("url, ok", [
    ("https://link.coupang.com/a/bXyZ12", True),
    ("https://coupa.ng/abc123", True),
    ("http://link.coupang.com/a/bXyZ12", False),              # https 아님
    ("https://www.coupang.com/vp/products/1", False),         # 파트너스 간편링크 아님 (수수료 추적 안 됨)
    ("https://link.coupang.com.evil.example/a/x", False),
    ("https://evil.example/link.coupang.com", False),
    ("https://link.coupang.com/", False),                     # 상품 경로 없음
    ("", False),
])
def test_check_link_format(url, ok):
    assert (publisher.check_link(url) is None) is ok


def test_real_link_check_makes_no_http_request(cfg, setup, monkeypatch):
    # 간편링크에 자동 접속하면 파트너스 클릭으로 잡힐 수 있어 링크 확인은 네트워크를 쓰지 않는다
    import requests

    def no_network(*a, **k):
        raise AssertionError("링크 확인이 HTTP 요청을 보냄")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "head", no_network)
    monkeypatch.setattr(requests.Session, "request", no_network)
    setup.add()
    result, _ = run(setup, cfg, link_checker=publisher.check_link)
    assert result["action"] == "published"


def test_bad_link_format_holds_post_in_real_run(cfg, setup):
    post = setup.add(link_url="https://www.coupang.com/vp/products/1")
    result, api = run(setup, cfg, link_checker=publisher.check_link)
    assert result["action"] == "held" and api.calls == []
    assert "간편링크" in setup.store.get_post(post["id"])["guard_log"]["attempts"][-1]["problems"][0]


# ── 8장: DRY_RUN 로그 ───────────────────────────────────────────────────────

def test_dry_run_logs_exact_requests_and_changes_nothing(cfg, setup, caplog):
    post = setup.add()
    api = publisher.DryRunApi("17841400000000000")
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        result, _ = run(setup, cfg, env=DRY, api=api)
    assert result["action"] == "dry_run"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DRY_RUN]")]
    base = "https://graph.threads.net/v1.0"
    body = json.dumps({"media_type": "TEXT", "text": post["text"], "link_attachment": post["link_url"]}, ensure_ascii=False)
    assert lines == [
        f"[DRY_RUN] POST {base}/17841400000000000/threads {body}",
        f"[DRY_RUN] GET {base}/<컨테이너 ID 1> " + json.dumps({"fields": "status,error_message"}),
        f"[DRY_RUN] POST {base}/17841400000000000/threads_publish " + json.dumps({"creation_id": "<컨테이너 ID 1>"}, ensure_ascii=False),
    ]
    saved = setup.store.get_post(post["id"])
    assert saved["status"] == "approved" and saved["threads_media_id"] is None     # DB 는 그대로
    assert "access_token" not in caplog.text


def test_dry_run_link_in_reply_logs_reply_request(cfg, setup, caplog):
    reply = f"{cfg.disclosure_text}\n\n소비전력 표기는 상품 페이지에서 확인할 수 있습니다."
    post = setup.add(variant="link_in_reply", reply_text=reply)
    api = publisher.DryRunApi("123")
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        run(setup, cfg, env=DRY, api=api)
    creates = [r["params"] for r in api.requests if r["path"].endswith("/threads")]
    assert creates[0] == {"media_type": "TEXT", "text": post["text"]}                    # 본문에는 링크 없음
    assert creates[1] == {"media_type": "TEXT", "text": reply, "reply_to_id": "<미디어 ID 2>",
                          "link_attachment": post["link_url"]}
    assert [r["path"] for r in api.requests].count("123/threads_publish") == 2


# ── 8장: 같은 글 두 번 → 두 번째는 건너뜀 ─────────────────────────────────────

def test_second_publish_of_same_post_is_skipped(cfg, setup):
    post = setup.add()
    run(setup, cfg)
    published = setup.store.get_post(post["id"])
    api = FakeApi()
    result = publisher.publish_post(published, store=setup.store, cfg=cfg, api=api, link_checker=link_ok,
                                    notifier=setup.notifier, now=NOW + timedelta(hours=3), dry_run=False)
    assert result["action"] == "skipped" and api.calls == []
    # 정기 실행으로 다시 돌려도 새 요청이 없다
    result, api = run(setup, cfg, now=NOW + timedelta(hours=3))
    assert result["action"] == "nothing_due" and api.calls == []


# ── 오류 처리 + 8장 자동 정지 (발행 실패 2회 연속) ─────────────────────────────────

def test_api_failure_marks_failed_without_republish(cfg, setup):
    post = setup.add()
    result, api = run(setup, cfg, api=FakeApi(fail={"create": [ThreadsApiError("HTTP 400 invalid")]}))
    assert result["action"] == "failed"
    assert setup.store.get_post(post["id"])["status"] == "failed"
    assert run(setup, cfg, now=NOW + timedelta(hours=1))[0]["action"] == "nothing_due"     # 자동 재발행 안 함


def test_two_failures_in_a_row_stop_publishing(cfg, setup):
    setup.add()
    run(setup, cfg, api=FakeApi(fail={"create": [ThreadsApiError("1")]}))
    setup.add(scheduled_at=(NOW + timedelta(hours=3)).astimezone(timezone.utc).isoformat())
    run(setup, cfg, api=FakeApi(fail={"publish": [ThreadsApiError("2")]}), now=NOW + timedelta(hours=3))
    assert autostop.is_stopped(setup.store)
    assert "발행 실패 2회 연속" in setup.notifier.sent[-1]
    # 다음 글이 있어도 발행하지 않는다
    setup.add(scheduled_at=(NOW + timedelta(hours=6)).astimezone(timezone.utc).isoformat())
    result, api = run(setup, cfg, now=NOW + timedelta(hours=6))
    assert result["action"] == "auto_stopped" and api.calls == []


def test_network_error_retried_once(cfg, setup):
    setup.add()
    result, api = run(setup, cfg, api=FakeApi(fail={"create": [NetworkError("reset")]}))
    assert result["action"] == "published"
    assert [c[0] for c in api.calls].count("create") == 2


def test_network_error_twice_fails(cfg, setup):
    setup.add()
    result, _ = run(setup, cfg, api=FakeApi(fail={"create": [NetworkError("a"), NetworkError("b")]}))
    assert result["action"] == "failed"


def test_publish_network_error_does_not_double_post(cfg, setup):
    # 발행 요청이 서버에선 성공했을 수 있음 → 컨테이너가 PUBLISHED 면 다시 발행하지 않는다
    setup.add()

    class Api(FakeApi):
        def container_status(self, cid):
            self.calls.append(("status", cid))
            return ("PUBLISHED" if any(c[0] == "publish" for c in self.calls) else "FINISHED"), None

    api = Api(fail={"publish": [NetworkError("timeout")]})
    result, _ = run(setup, cfg, api=api)
    assert result["action"] == "failed"
    assert [c[0] for c in api.calls].count("publish") == 1


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED", "PUBLISHED"])
def test_bad_container_status_fails_without_publishing(cfg, setup, status):
    setup.add()
    result, api = run(setup, cfg, api=FakeApi(status=status))
    assert result["action"] == "failed"
    assert not any(c[0] == "publish" for c in api.calls)


def test_reply_failure_retried_alone_next_run(cfg, setup):
    reply = f"{cfg.disclosure_text}\n\n소비전력 표기는 상품 페이지에서 확인할 수 있습니다."
    post = setup.add(variant="link_in_reply", reply_text=reply)
    # 본문 성공, 답글 컨테이너 생성 실패
    class ReplyFails(FakeApi):
        def create_container(self, params):
            if "reply_to_id" in params:
                self.calls.append(("create", params))
                raise ThreadsApiError("reply fail")
            return super().create_container(params)

    result, _ = run(setup, cfg, api=ReplyFails())
    saved = setup.store.get_post(post["id"])
    assert result["action"] == "published" and saved["status"] == "published" and saved["threads_reply_id"] is None
    # 다음 실행: 본문은 다시 만들지 않고 답글만
    result, api = run(setup, cfg, now=NOW + timedelta(hours=1))
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates == [{"media_type": "TEXT", "text": reply, "reply_to_id": saved["threads_media_id"],
                        "link_attachment": post["link_url"]}]
    assert setup.store.get_post(post["id"])["threads_reply_id"] == "m-c1"


# ── 규칙 2·3·6 재확인 ──────────────────────────────────────────────────────

def test_guard_rerun_at_publish_holds_edited_post(cfg, setup):
    # 규칙 3: 승인 뒤 본문에서 대가성 문구가 빠졌다면 발행하지 않는다
    post = setup.add()
    setup.store.update_post(post["id"], {"text": post["text"].split("\n", 1)[1]})
    result, api = run(setup, cfg)
    assert result["action"] == "held" and api.calls == []
    assert any("[G2]" in p for p in result["problems"])


def test_request_params_ignore_media_fields_in_post(make_post):
    # 규칙 2: 글 행에 남의 이미지·영상 주소가 섞여 있어도 요청에 들어가지 않는다.
    # 이미지·영상은 media.py 가 data/media/ 파일을 올려 만든 items 로만 붙는다
    post = make_post(image_url="https://example.com/a.jpg", video_url="x", children=["1"])
    params = publisher.container_params(post)
    assert set(params) <= publisher.ALLOWED_PARAMS and params == {"media_type": "TEXT", "text": post["text"],
                                                                   "link_attachment": post["link_url"]}


def test_reply_only_to_own_post(make_post):
    post = make_post(variant="link_in_reply", reply_text="x", threads_media_id="mine")
    with pytest.raises(PublishRefused, match="규칙 6"):
        publisher.container_params(post, reply_to_id="someone-else")


def test_no_code_for_other_accounts_interactions():
    # 규칙 6: 멘션·키워드 검색·남의 답글 관리·팔로우 엔드포인트를 코드에 두지 않는다
    forbidden = ("/mentions", "manage_reply", "keyword_search", "/follow", "threads_manage_mentions")
    for path in PROJECT_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not [w for w in forbidden if w in source], path.name


# ── 실제 API 클라이언트 요청 형태 ────────────────────────────────────────────────

class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def _resp(self, method, url, **kw):
        self.calls.append((method, url, kw))
        status, data = self.responses.pop(0)
        return pytypes.SimpleNamespace(status_code=status, json=lambda: data, text=json.dumps(data))

    def post(self, url, **kw):
        return self._resp("POST", url, **kw)

    def get(self, url, **kw):
        return self._resp("GET", url, **kw)


def test_threads_api_request_shape():
    http = FakeHttp([(200, {"id": "c1"}), (200, {"status": "FINISHED"}), (200, {"id": "m1"})])
    api = publisher.ThreadsApi("123", "TOKEN", session=http)
    assert api.create_container({"media_type": "TEXT", "text": "t"}) == "c1"
    assert api.container_status("c1") == ("FINISHED", None)
    assert api.publish("c1") == "m1"
    (m1, u1, k1), (m2, u2, k2), (m3, u3, k3) = http.calls
    assert (m1, u1) == ("POST", "https://graph.threads.net/v1.0/123/threads")
    assert k1["data"] == {"media_type": "TEXT", "text": "t", "access_token": "TOKEN"}
    assert (m2, u2, k2["params"]["fields"]) == ("GET", "https://graph.threads.net/v1.0/c1", "status,error_message")
    assert (m3, u3, k3["data"]["creation_id"]) == ("POST", "https://graph.threads.net/v1.0/123/threads_publish", "c1")


def test_threads_api_errors():
    import requests

    api = publisher.ThreadsApi("123", "T", session=FakeHttp([(400, {"error": {"message": "Invalid parameter"}})]))
    with pytest.raises(ThreadsApiError, match="Invalid parameter"):
        api.create_container({"media_type": "TEXT", "text": "t"})

    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("down")

    with pytest.raises(NetworkError):
        publisher.ThreadsApi("123", "T", session=Down()).create_container({"media_type": "TEXT", "text": "t"})


# ── 링크 답글 지연 (reply_delay_hours, 2026-10-18) ──────────────────────────

import dataclasses


def delayed(cfg, hours=3.0):
    return dataclasses.replace(cfg, reply_delay_hours=hours)


def test_delayed_reply_waits_then_posts_only_after_delay(cfg, setup):
    cfg = delayed(cfg)
    reply = f"{cfg.disclosure_text}\n\n제품은 여기서 볼 수 있어."
    post = setup.add(variant="link_in_reply", reply_text=reply)
    result, api = run(setup, cfg)
    saved = setup.store.get_post(post["id"])
    assert result["action"] == "published" and result["reply"] is None
    assert saved["status"] == "published" and saved["threads_reply_id"] is None
    assert not any("reply_to_id" in c[1] for c in api.calls if c[0] == "create")

    # 2시간 40분 뒤: 아직 (15분 여유를 빼도 2시간 45분 전)
    _, api = run(setup, cfg, now=NOW + timedelta(hours=2, minutes=40))
    assert not any("reply_to_id" in c[1] for c in api.calls if c[0] == "create")
    assert setup.store.get_post(post["id"])["threads_reply_id"] is None

    # 3시간 뒤: 답글만 달린다 (본문은 다시 만들지 않는다)
    _, api = run(setup, cfg, now=NOW + timedelta(hours=3, minutes=1))
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates == [{"media_type": "TEXT", "text": reply, "reply_to_id": saved["threads_media_id"],
                        "link_attachment": post["link_url"]}]
    assert setup.store.get_post(post["id"])["threads_reply_id"] == "m-c1"


def test_delayed_reply_goes_out_in_run_a_few_seconds_before_the_delay(cfg, setup):
    """30분 주기 실행이 정확히 3시간에서 몇 초 모자라도 답글을 단다 (15분 여유, 2026-10-24)."""
    cfg = delayed(cfg)
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\nx")
    run(setup, cfg)
    _, api = run(setup, cfg, now=NOW + timedelta(hours=3) - timedelta(seconds=20))
    assert any("reply_to_id" in c[1] for c in api.calls if c[0] == "create")
    assert setup.store.get_post(post["id"])["threads_reply_id"] is not None


def test_default_zero_delay_posts_reply_immediately(cfg, setup):
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\nx")
    result, _ = run(setup, cfg)
    assert result["reply"] == "m-c2" and setup.store.get_post(post["id"])["threads_reply_id"] == "m-c2"


def test_delayed_reply_is_never_posted_twice(cfg, setup):
    cfg = delayed(cfg)
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\nx")
    run(setup, cfg)
    run(setup, cfg, now=NOW + timedelta(hours=4))
    _, api = run(setup, cfg, now=NOW + timedelta(hours=5))
    assert not any(c[0] == "create" for c in api.calls)


def test_dry_run_still_logs_reply_request_with_delay(cfg, setup, caplog):
    cfg = delayed(cfg)
    setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\nx")
    with caplog.at_level(logging.INFO):
        result, _ = run(setup, cfg, env=DRY, api=publisher.DryRunApi("123"))
    assert result["action"] == "dry_run" and result["reply"] is not None
