"""
Step 5 일일 요약·취소·모니터링 조작 테스트.

8장 체크리스트
  - 일일 요약이 발송되고, cancel_post 로 예정 글이 취소됨
  - (Step 4-B) uncertain 판정 → held, 일일 요약에 표시
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import actions
import autostop
import digest
from notify import CollectingNotifier, split_message
from settings import KST, PROJECT_ROOT
from store import MemoryStore

NOW = datetime(2026, 11, 2, 21, 0, tzinfo=KST)     # 일일 요약 발송 시각


def kst(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=KST).astimezone(timezone.utc).isoformat()


UNCERTAIN_REVIEW = {
    "status": "uncertain",
    "items": {"R3": {"verdict": "uncertain", "reason": "'밤새 켜 둬도' 가 안전 주장인지 애매함", "hook_only": False},
              "R1": {"verdict": "pass", "reason": "", "hook_only": False}},
    "errors": [],
}


@pytest.fixture
def world(make_post):
    store = MemoryStore()
    posts = {
        "tomorrow": make_post(scheduled_at=kst(3, "08:10")),
        "tomorrow_tip": make_post(type="tip", has_link=False, link_url=None, variant=None,
                                  text="겨울 전기요금이 갑자기 뛰는 건 누진 구간 때문입니다\n\n본문", scheduled_at=kst(3, "12:30")),
        "later": make_post(scheduled_at=kst(4, "08:10")),
        "cancelled": make_post(status="cancelled", scheduled_at=kst(3, "18:40")),
        "uncertain": make_post(status="held", review_result=UNCERTAIN_REVIEW,
                               created_at=(NOW - timedelta(hours=2)).astimezone(timezone.utc).isoformat()),
        "guard_held": make_post(status="held", review_result=None,
                                guard_log={"attempts": [{"attempt": 3, "hook_style": "result",
                                                         "problems": ["[G3] 본문에 입력·계산 데이터에 없는 숫자: 7"]}]},
                                created_at=(NOW - timedelta(hours=3)).astimezone(timezone.utc).isoformat()),
        "old_held": make_post(status="held", created_at=(NOW - timedelta(days=8)).astimezone(timezone.utc).isoformat()),
        "link_held": make_post(status="held", created_at=(NOW - timedelta(hours=30)).astimezone(timezone.utc).isoformat(),
                               guard_log={"attempts": [{"attempt": "publish", "stage": "link", "hook_style": "result",
                                                        "problems": ["링크 최종 응답이 정상이 아닙니다: HTTP 404 (www.coupang.com)"]}]}),
        "published": make_post(status="published", published_at=kst(2, "18:40"), threads_media_id="m-1"),
    }
    for p in posts.values():
        store.insert_post(p)
    store.metrics.append({"threads_media_id": "m-1", "collected_at": "2026-11-02T11:00:00+00:00", "views": 321})
    return store, posts


def build(store, cfg, **kw):
    kw.setdefault("dry_run", True)
    kw.setdefault("token_issued_at", date(2026, 10, 25))
    return digest.build_digest(store, cfg, NOW, **kw)


# ── 8장: 일일 요약 발송 ─────────────────────────────────────────────────────

def test_digest_is_sent_with_counts_only(cfg, world):
    store, posts = world
    notifier = CollectingNotifier()
    text = digest.send_digest(store, cfg, notifier, NOW, dry_run=True, token_issued_at=date(2026, 10, 25),
                              exam=(False, "시험지 기록이 없습니다"))
    assert notifier.sent == [text]
    assert store.events[-1]["kind"] == "digest_sent"

    # 발행 현황·보류는 개수만 (최근 36시간 보류: uncertain·guard_held·link_held)
    assert "내일(11-03) 발행 예정 2개" in text
    assert "오늘 발행 1개" in text
    assert "보류(held) 3개" in text
    for p in posts.values():
        assert p["id"] not in text                              # post_id 는 텔레그램에 없다
    assert "2,000W" not in text and "[G3]" not in text and "R3" not in text and "조회" not in text

    # 상태: 시험지 불합격만 자세히
    assert "검수자 시험지: 불합격 — 발행 불가" in text
    assert "연습 모드 DRY_RUN" in text
    assert len(text.splitlines()) <= 8


def test_old_held_posts_are_not_counted(cfg, world):
    store, posts = world
    assert "보류(held) 3개" in build(store, cfg)                  # old_held(8일 전)는 빠진다
    assert "보류" not in build(MemoryStore(), cfg)                # 보류가 없으면 줄 자체가 없다


def test_healthy_status_is_one_line(cfg, world):
    store, _ = world
    text = build(store, cfg, exam=(True, "합격 (2026-10-18T08:07:48+00:00, 25/25 차단, 14/15 통과)"))
    assert text.splitlines()[1] == "상태: 정상"
    assert "시험지" not in text and "자동 정지" not in text


def test_reply_inbox_is_not_in_digest(cfg, make_post):
    store = MemoryStore()
    ours = make_post(status="published", published_at=kst(2, "15:12"), threads_media_id="m-9")
    store.insert_post(ours)
    at = (NOW - timedelta(hours=1)).isoformat()
    store.set_state("reply_inbox", [{"post_id": ours["id"], "reply_id": "r1", "username": "kim", "text": "저는 2번이요",
                                     "permalink": "https://www.threads.com/@kim/post/abc", "at": at}])
    text = build(store, cfg)
    assert "답해 줄 답글" not in text and "@kim" not in text and "threads.com" not in text
    assert "오늘 둘러볼 검색어:" in text


def test_digest_shows_auto_stop_and_token_warning(cfg, world):
    store, _ = world
    autostop.trigger(store, CollectingNotifier(), "발행 실패 2회 연속")
    text = build(store, cfg, token_issued_at=date(2026, 9, 5))      # 11-04 만료 → 2일 전
    assert "자동 정지: 정지 중" in text and "발행 실패 2회 연속" in text
    assert "토큰 경고: 스레드 토큰 만료 2일 전" in text


def test_digest_without_posts(cfg):
    text = build(MemoryStore(), cfg)
    assert "내일(11-03) 발행 예정 0개" in text and "오늘 발행 0개" in text and "보류" not in text


# ── 8장: cancel_post 로 예정 글 취소 ───────────────────────────────────────────

def test_cancel_post_removes_scheduled_post(cfg, world):
    store, posts = world
    pid = posts["tomorrow"]["id"]
    assert "내일(11-03) 발행 예정 2개" in build(store, cfg)

    cancelled = actions.cancel_post(store, pid)
    assert cancelled["status"] == "cancelled"
    assert store.events[-1] == {**store.events[-1], "kind": "cancel_post"}
    assert "내일(11-03) 발행 예정 1개" in build(store, cfg)


@pytest.mark.parametrize("key, message", [
    ("published", "이미 발행된 글"),
    ("uncertain", r"예정 글\(approved\)만"),
    ("cancelled", r"예정 글\(approved\)만"),
])
def test_cancel_rejects_non_scheduled_posts(world, key, message):
    store, posts = world
    with pytest.raises(actions.ActionError, match=message):
        actions.cancel_post(store, posts[key]["id"])


def test_cancel_unknown_id(world):
    store, _ = world
    with pytest.raises(actions.ActionError, match="찾을 수 없습니다"):
        actions.cancel_post(store, "00000000-0000-0000-0000-000000000000")


def test_cancel_workflow_calls_cancel_action():
    wf = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "cancel_post.yml").read_text(encoding="utf-8"))
    triggers = wf[True] if True in wf else wf["on"]      # YAML 1.1 은 'on' 을 True 로 읽는다
    assert "post_id" in triggers["workflow_dispatch"]["inputs"]
    assert "python actions.py" in wf["jobs"]["cancel"]["steps"][-1]["run"]


def test_cancel_dry_run_changes_nothing(world):
    store, posts = world
    pid = posts["tomorrow"]["id"]
    assert actions.cancel_post(store, pid, dry_run=True)["status"] == "approved"
    assert store.get_post(pid)["status"] == "approved"


# ── 모니터링 화면 조작 (actions) ──────────────────────────────────────────────

def test_manual_approve_reruns_guard_and_assigns_slot(cfg, world):
    store, posts = world
    pid = posts["uncertain"]["id"]
    updated = actions.manual_approve(store, cfg, pid, by="운영자", now=NOW)
    assert updated["status"] == "approved"
    # 지금(21:00) 이후 첫 슬롯은 오늘 21:30. 오늘 18:40 발행 글과 2시간 50분 간격이라 가능
    assert updated["scheduled_at"] == kst(2, "21:30")
    assert store.events[-1]["kind"] == "manual_approve"
    # 하나 더 승인하면: 내일 08:10·12:30 이 차 있고(링크 글 1개) 18:40 은 취소 글이라 비어 있음 → 18:40
    second = actions.manual_approve(store, cfg, posts["guard_held"]["id"], by="운영자", now=NOW)
    assert second["scheduled_at"] == kst(3, "18:40")


def test_manual_approve_blocked_by_guard(cfg, world, make_post):
    store, _ = world
    bad = make_post(status="held", text="링크 글인데 문구가 없습니다")
    store.insert_post(bad)
    with pytest.raises(actions.ActionError, match="코드 가드"):
        actions.manual_approve(store, cfg, bad["id"], by="운영자", now=NOW)
    assert store.get_post(bad["id"])["status"] == "held"


def test_manual_approve_needs_operator_name(cfg, world):
    store, posts = world
    with pytest.raises(actions.ActionError, match="승인한 사람"):
        actions.manual_approve(store, cfg, posts["uncertain"]["id"], by=" ", now=NOW)


def test_swap_hook(cfg, world):
    store, posts = world
    pid = posts["uncertain"]["id"]
    updated, problems = actions.swap_hook(store, cfg, pid, "question")
    assert problems == [] and updated["hook_style"] == "question"
    assert updated["text"].split("\n")[1] == "2,000W 히터 한 달 전기료는 얼마일까요?"
    # 가드에 걸리는 훅은 저장되지 않는다 (2026-10-10 부터 숫자 없는 훅은 허용, 30자 초과는 여전히 막는다)
    post = store.get_post(pid)
    candidates = [dict(h, text="전기히터를 하루 종일 켜 두면 한 달 요금은 과연 얼마나 나올까요?") if h["style"] == "compare" else h for h in post["hook_candidates"]]
    store.update_post(pid, {"hook_candidates": candidates})
    unchanged, problems = actions.swap_hook(store, cfg, pid, "compare")
    assert any(p.startswith("[G7]") for p in problems)
    assert store.get_post(pid)["hook_style"] == "question"


def test_discard(world):
    store, posts = world
    assert actions.discard(store, posts["guard_held"]["id"], by="운영자")["status"] == "cancelled"
    with pytest.raises(actions.ActionError):
        actions.discard(store, posts["tomorrow"]["id"], by="운영자")


# ── 알림 ─────────────────────────────────────────────────────────────────

def test_split_message_keeps_limit():
    text = "\n".join(f"줄 {i} " + "가" * 50 for i in range(200)) + "\n" + "나" * 9000
    chunks = split_message(text, limit=4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
