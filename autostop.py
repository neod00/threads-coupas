"""
자동 정지 (PLAN v2 규칙 10). 정지 상태는 킬 스위치(AUTO_PUBLISH_DISABLED)와 같게 취급해 발행을 멈추고 알림을 보낸다.
해제는 사람이 사유를 입력해야만 가능하고, 해제 기록은 events 에 남는다.

조건                                        판정 위치
  1 내 게시물 답글에 스팸·신고·광고충·사기·허위    watch_replies.py (Step 8)
  2 발행 실패 2회 연속                         publisher.py (Step 6) → record_publish_result
  3 하루 생성분 중 held 비율 50% 초과            generate 파이프라인 → check_held_ratio
  4 AI 검수자 응답 3회 연속 형식 오류             generate 파이프라인 → record_review_status
  5 시험지에 케이스 추가 (재시험 합격 전까지)       exam.exam_status 가 케이스 해시 불일치로 불합격 → 발행기(Step 6)가 거부하고 정지
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("threads_coupas")

STATE_KEY = "auto_stop"
FORMAT_ERROR_KEY = "reviewer_format_errors_in_a_row"
PUBLISH_FAIL_KEY = "publish_failures_in_a_row"
MAX_FORMAT_ERRORS = 3
MAX_PUBLISH_FAILURES = 2
MAX_HELD_RATIO = 0.5
REPLY_STOP_WORDS = ("스팸", "신고", "광고충", "사기", "허위")


def status(store) -> dict:
    return store.get_state(STATE_KEY) or {"active": False, "reasons": []}


def is_stopped(store) -> bool:
    return bool(status(store).get("active"))


def trigger(store, notifier, reason: str, detail: dict | None = None) -> None:
    """정지를 켜고 알림을 보낸다. 이미 정지 중이면 사유만 더한다."""
    state = status(store)
    now = datetime.now(timezone.utc).isoformat()
    first = not state.get("active")
    state = {
        "active": True,
        "since": state.get("since") if not first else now,
        "reasons": [*state.get("reasons", []), {"reason": reason, "at": now, "detail": detail or {}}],
    }
    store.set_state(STATE_KEY, state)
    store.log_event("auto_stop", {"reason": reason, "detail": detail or {}})
    log.error(f"[자동 정지] {reason}")
    try:
        notifier.send(f"[자동 정지] 발행을 멈췄습니다.\n사유: {reason}\n"
                      "해제: 모니터링 화면에서 사유를 입력하고 해제하세요.")
    except Exception as e:      # 알림이 실패해도 정지 상태는 유지한다
        log.error(f"[자동 정지] 알림 전송 실패: {e}")


def clear(store, reason: str, by: str) -> None:
    """사람이 해제한다. 사유 필수, events 에 기록."""
    if not (reason or "").strip():
        raise ValueError("자동 정지 해제 사유를 입력해야 합니다")
    if not (by or "").strip():
        raise ValueError("해제한 사람을 입력해야 합니다")
    previous = status(store)
    store.set_state(STATE_KEY, {"active": False, "reasons": []})
    store.set_state(FORMAT_ERROR_KEY, 0)
    store.set_state(PUBLISH_FAIL_KEY, 0)
    store.log_event("auto_stop_cleared", {"reason": reason.strip(), "by": by.strip(), "previous": previous})


def _count(store, key: str, failed: bool) -> int:
    n = (store.get_state(key, 0) or 0) + 1 if failed else 0
    store.set_state(key, n)
    return n


def record_review_status(store, notifier, review_status: str | None) -> None:
    """검수 결과마다 호출. 형식 오류가 3회 연속이면 정지 (조건 4). 정상 응답이 오면 0으로 되돌린다."""
    if review_status is None:
        return
    n = _count(store, FORMAT_ERROR_KEY, review_status == "format_error")
    if n >= MAX_FORMAT_ERRORS:
        trigger(store, notifier, f"AI 검수자 응답 형식 오류 {n}회 연속")


def record_publish_result(store, notifier, success: bool, detail: dict | None = None) -> None:
    """발행 시도마다 호출. 2회 연속 실패면 정지 (조건 2)."""
    n = _count(store, PUBLISH_FAIL_KEY, not success)
    if n >= MAX_PUBLISH_FAILURES:
        trigger(store, notifier, f"발행 실패 {n}회 연속", detail)


def check_held_ratio(store, notifier, generated: list[dict]) -> float:
    """하루 생성분 중 held 비율이 50%를 넘으면 정지 (조건 3). 비율을 돌려준다."""
    if not generated:
        return 0.0
    ratio = sum(p["status"] == "held" for p in generated) / len(generated)
    if ratio > MAX_HELD_RATIO:
        trigger(store, notifier, f"하루 생성분 중 held 비율 {ratio:.0%} (50% 초과, 생성기 이상 신호)",
                {"generated": len(generated), "held": [p["id"] for p in generated if p["status"] == "held"]})
    return ratio


def check_reply_text(store, notifier, reply_text: str, detail: dict | None = None) -> bool:
    """내 게시물에 달린 답글 한 개를 검사. 정지 단어가 있으면 정지 (조건 1). watch_replies.py 가 부른다."""
    hits = [w for w in REPLY_STOP_WORDS if w in (reply_text or "")]
    if hits:
        trigger(store, notifier, f"내 게시물 답글에 '{', '.join(hits)}' 포함", detail)
    return bool(hits)
