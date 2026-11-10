"""
자동 정지 (PLAN v2 규칙 10). 정지 상태는 킬 스위치(AUTO_PUBLISH_DISABLED)와 같게 취급해 발행을 멈추고 알림을 보낸다.
해제는 사람이 사유를 입력해야만 가능하고, 해제 기록은 events 에 남는다.

조건                                        판정 위치
  1 내 게시물 답글에 스팸·신고·광고충·사기·허위    watch_replies.py (Step 8) → check_reply_text
    (스팸·신고·사기는 고발하는 문맥일 때만 정지, 아니면 알림만 — PLAN 1-3, 2026-11-11 결정)
  2 발행 실패 2회 연속                         publisher.py (Step 6) → record_publish_result
  3 하루 생성분 중 held 비율 50% 초과            generate 파이프라인 → check_held_ratio
    (API 호출 실패로 생긴 held 는 비율에서 빼고 알림만 보낸다 — 2026-10-01 결정)
    (하루 생성분이 3개 미만이면 최근 3일 생성분을 합쳐 보고, 그래도 3개 미만이면 알림만 — PLAN 1-3, 2026-11-11)
  4 AI 검수자 응답 3회 연속 형식 오류             generate 파이프라인 → record_review_status
  5 시험지에 케이스 추가 (재시험 합격 전까지)       exam.exam_status 가 케이스 해시 불일치로 불합격 → 발행기(Step 6)가 거부하고 정지
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("threads_coupas")

STATE_KEY = "auto_stop"
FORMAT_ERROR_KEY = "reviewer_format_errors_in_a_row"
PUBLISH_FAIL_KEY = "publish_failures_in_a_row"
MAX_FORMAT_ERRORS = 3
MAX_PUBLISH_FAILURES = 2
MAX_HELD_RATIO = 0.5
# 하루 1개만 만드는 계정은 보류 1개가 곧 100% 라 정지했다 (2026-11-07 openbrain_en, 2026-10-04 openbrain).
# 표본이 이보다 적으면 최근 며칠 생성분을 합쳐 보고, 그래도 적으면 정지하지 않고 알림만 보낸다 (PLAN 1-3).
MIN_HELD_SAMPLE = 3
HELD_WINDOW_DAYS = 3
REPLY_STOP_WORDS = ("스팸", "신고", "광고충", "사기", "허위")
# 일상 표현과 겹치는 단어는 고발하는 문맥일 때만 정지한다 (PLAN 1-3).
# 2026-11-10 "편의점에서 사기도 수월해서"(구매)의 '사기'로 sabol.kka 가 오탐 정지됐다.
# 그 밖의 쓰임("사기 전에", "슬리퍼 신고", "스팸 구워")은 정지하지 않고 답글을 담아 알림만 보낸다.
REPLY_ACCUSATION_PATTERNS = {
    "사기": re.compile(r"사기(꾼|성|네|야|임|다|잖|래|라고)|사기\s?(치|쳤|친|칠|당|같|아니|아닌|계정|광고|업체|판매|글(?!쎄))"),
    "신고": re.compile(r"신고(합니|할게|할께|할래|할거|하겠|했어요|했습|했다|했음|함|하세|해요|해라|해야|해버|감|각)"
                       r"|신고\s?(먹|버튼|완료|누르|넣)"),
    "스팸": re.compile(r"스팸(이네|이야|임|이다|봇|성)|스팸\s?(계정|광고|댓글|글(?!쎄)|아니|아닌|같아|같네|같은\s?(계정|글|광고|댓글))"),
}
REPLY_EXCERPT_CHARS = 120


def status(store) -> dict:
    return store.get_state(STATE_KEY) or {"active": False, "reasons": []}


def is_stopped(store) -> bool:
    return bool(status(store).get("active"))


def trigger(store, notifier, reason: str, detail: dict | None = None, note: str = "") -> None:
    """정지를 켜고 알림을 보낸다. 이미 정지 중이면 사유만 더한다. note 는 알림에만 붙는 설명(예: 답글 원문)."""
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
        notifier.send(f"[자동 정지] 발행을 멈췄습니다.\n사유: {reason}\n" + (f"{note}\n" if note else "")
                      + "해제: 모니터링 화면에서 사유를 입력하고 해제하세요.")
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


def is_api_error_hold(post: dict) -> bool:
    """글 품질이 아니라 API 호출 실패(LLM 생성·검수 호출 거절, 서버 혼잡, 크레딧 부족)로 held 된 글인지."""
    if post.get("status") != "held":
        return False
    review = post.get("review_result") or {}
    if review.get("status") == "format_error" and review.get("errors") and \
            all("호출 실패" in e for e in review["errors"]):
        return True
    attempts = (post.get("guard_log") or {}).get("attempts") or []
    problems = attempts[-1]["problems"] if attempts else []
    return bool(problems) and all(p.startswith("LLM 호출 실패") or "호출 실패" in p for p in problems)


def check_held_ratio(store, notifier, generated: list[dict], now: datetime | None = None) -> float:
    """하루 생성분 중 held 비율이 50%를 넘으면 정지 (조건 3). 비율을 돌려준다.
    API 호출 실패로 생긴 held 는 생성기 품질 신호가 아니므로 비율 계산에서 뺀다 (2026-10-01 결정, A안).
    대신 그런 글이 있으면 정지하지 않고 알림만 보낸다.
    판정할 글이 3개 미만이면 최근 3일 안에 생성된 글을 합쳐 본다. 합쳐도 3개 미만이면 정지하지 않고,
    held 가 있으면 알림만 보낸다 (PLAN 1-3, 2026-11-11). 3개 이상인 날은 그날 생성분만 본다(예전과 같음)."""
    api_errors = [p for p in generated if is_api_error_hold(p)]
    judged = [p for p in generated if p not in api_errors]
    if api_errors:
        try:
            notifier.send(f"[알림] API 호출 오류로 보류된 글 {len(api_errors)}개 (자동 정지 대상 아님). "
                          "Gemini 혼잡·한도, Anthropic 크레딧을 확인하세요.")
        except Exception as e:
            log.error(f"[알림] 전송 실패: {e}")
    if not judged:
        return 0.0
    sample, scope = judged, "하루 생성분"
    if len(judged) < MIN_HELD_SAMPLE:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(days=HELD_WINDOW_DAYS)).astimezone(timezone.utc).isoformat()
        ids = {p.get("id") for p in generated}
        recent = [p for p in store.list_posts(since=since) if p.get("id") not in ids and not is_api_error_hold(p)]
        sample, scope = judged + recent, f"최근 {HELD_WINDOW_DAYS}일 생성분"
    held = [p for p in sample if p["status"] == "held"]
    ratio = len(held) / len(sample)
    if len(sample) < MIN_HELD_SAMPLE:
        if held:
            hooks = "\n".join(f"- {(p.get('text') or '').strip().splitlines()[0][:60]}" if (p.get("text") or "").strip()
                              else f"- (초안 없음) {str(p.get('id'))[:8]}" for p in held)
            try:
                notifier.send(f"[확인 필요] 보류된 글 {len(held)}개 ({scope} {len(sample)}개 중). "
                              f"판정할 글이 {MIN_HELD_SAMPLE}개 미만이라 발행을 멈추지 않았어요.\n{hooks}")
            except Exception as e:
                log.error(f"[알림] 전송 실패: {e}")
        return ratio
    if ratio > MAX_HELD_RATIO:
        trigger(store, notifier, f"{scope} 중 held 비율 {ratio:.0%} (50% 초과, 생성기 이상 신호)",
                {"generated": len(generated), "api_error_held": len(api_errors), "sample": len(sample),
                 "held": [p["id"] for p in held]})
    return ratio


def reply_stop_words(reply_text: str) -> tuple[list[str], list[str]]:
    """답글에 든 정지 단어를 (정지할 단어, 알림만 보낼 단어) 로 나눈다 (PLAN 1-3).
    광고충·허위는 들어 있기만 해도 정지, 스팸·신고·사기는 고발하는 문맥일 때만 정지."""
    text = reply_text or ""
    stop, notify_only = [], []
    for w in REPLY_STOP_WORDS:
        if w not in text:
            continue
        pattern = REPLY_ACCUSATION_PATTERNS.get(w)
        (stop if pattern is None or pattern.search(text) else notify_only).append(w)
    return stop, notify_only


def _reply_note(reply_text: str, detail: dict | None) -> str:
    excerpt = " ".join((reply_text or "").split())[:REPLY_EXCERPT_CHARS]
    who = (detail or {}).get("username")
    link = (detail or {}).get("permalink")
    return f"답글{f' (@{who})' if who else ''}: {excerpt}" + (f"\n{link}" if link else "")


def check_reply_text(store, notifier, reply_text: str, detail: dict | None = None) -> bool:
    """내 게시물에 달린 답글 한 개를 검사. 고발하는 정지 단어가 있으면 정지 (조건 1). watch_replies.py 가 부른다.
    정지 단어가 일상 표현으로만 쓰였으면 정지하지 않고 사람이 확인하도록 알림만 보낸다."""
    stop, notify_only = reply_stop_words(reply_text)
    note = _reply_note(reply_text, detail)
    if stop:
        trigger(store, notifier, f"내 게시물 답글에 '{', '.join(stop)}' 포함", detail, note=note)
    elif notify_only:
        log.warning(f"[답글 감시] '{', '.join(notify_only)}' 단어가 있지만 고발 문맥이 아니라 정지하지 않음")
        try:
            notifier.send(f"[확인 필요] 답글에 '{', '.join(notify_only)}' 단어가 있지만 고발하는 문맥이 아니라 "
                          f"발행을 멈추지 않았어요.\n{note}\n악성 답글이면 킬 스위치를 켜 주세요.")
        except Exception as e:
            log.error(f"[알림] 전송 실패: {e}")
    return bool(stop)
