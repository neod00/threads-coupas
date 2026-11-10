"""
자동 정지 테스트 (규칙 10). 8장 "자동 정지 5개 조건" 항목은 발행기(조건 2)·답글 감시(조건 1)·시험지(조건 5)가
생기는 Step 6·8 에서 발행이 실제로 멈추는지까지 함께 증명한다. 여기서는 상태·알림·해제와 조건 판정을 검증한다.
"""
import pytest

import autostop
from notify import CollectingNotifier
from store import MemoryStore


@pytest.fixture
def env():
    return MemoryStore(), CollectingNotifier()


def test_trigger_sets_state_and_notifies(env):
    store, notifier = env
    assert not autostop.is_stopped(store)
    autostop.trigger(store, notifier, "테스트 사유")
    assert autostop.is_stopped(store)
    assert "테스트 사유" in notifier.sent[0]
    assert store.events[-1]["kind"] == "auto_stop"


def test_second_trigger_keeps_first_time_and_adds_reason(env):
    store, notifier = env
    autostop.trigger(store, notifier, "첫 사유")
    since = autostop.status(store)["since"]
    autostop.trigger(store, notifier, "둘째 사유")
    state = autostop.status(store)
    assert state["since"] == since
    assert [r["reason"] for r in state["reasons"]] == ["첫 사유", "둘째 사유"]


def test_clear_requires_reason_and_name_and_is_logged(env):
    store, notifier = env
    autostop.trigger(store, notifier, "사유")
    with pytest.raises(ValueError, match="사유"):
        autostop.clear(store, "  ", "운영자")
    with pytest.raises(ValueError, match="사람"):
        autostop.clear(store, "원인 확인함", "")
    assert autostop.is_stopped(store)
    autostop.clear(store, "원인 확인함", "운영자")
    assert not autostop.is_stopped(store)
    event = store.events[-1]
    assert event["kind"] == "auto_stop_cleared"
    assert event["detail"]["reason"] == "원인 확인함" and event["detail"]["by"] == "운영자"


def test_notify_failure_still_stops(env):
    store, _ = env

    class Broken:
        def send(self, text):
            raise RuntimeError("텔레그램 장애")

    autostop.trigger(store, Broken(), "사유")
    assert autostop.is_stopped(store)


# 조건 4: 검수자 형식 오류 3회 연속
def test_three_format_errors_in_a_row_stop(env):
    store, notifier = env
    for _ in range(2):
        autostop.record_review_status(store, notifier, "format_error")
    assert not autostop.is_stopped(store)
    autostop.record_review_status(store, notifier, "format_error")
    assert autostop.is_stopped(store)
    assert "형식 오류 3회 연속" in notifier.sent[-1]


def test_valid_review_resets_format_error_count(env):
    store, notifier = env
    for status in ("format_error", "format_error", "approved", "format_error", "format_error"):
        autostop.record_review_status(store, notifier, status)
    assert not autostop.is_stopped(store)


# 조건 2: 발행 실패 2회 연속 (Step 6 발행기가 부른다)
def test_two_publish_failures_in_a_row_stop(env):
    store, notifier = env
    autostop.record_publish_result(store, notifier, False)
    autostop.record_publish_result(store, notifier, True)
    autostop.record_publish_result(store, notifier, False)
    assert not autostop.is_stopped(store)
    autostop.record_publish_result(store, notifier, False)
    assert autostop.is_stopped(store)


# 조건 3: 하루 생성분 held 비율 50% 초과
@pytest.mark.parametrize("statuses, stopped", [
    (["held", "held", "held", "approved"], True),
    (["held", "held", "approved", "approved"], False),     # 정확히 50% 는 정지하지 않음
    ([], False),
])
def test_held_ratio(env, statuses, stopped):
    store, notifier = env
    posts = [{"id": str(i), "status": s} for i, s in enumerate(statuses)]
    autostop.check_held_ratio(store, notifier, posts)
    assert autostop.is_stopped(store) is stopped


def _api_error_post(i, kind):
    if kind == "llm":
        return {"id": f"e{i}", "status": "held",
                "guard_log": {"attempts": [{"problems": ["LLM 호출 실패: 503 UNAVAILABLE"]}]}}
    return {"id": f"e{i}", "status": "held", "guard_log": {"attempts": [{"problems": ["[검수] 응답 형식 오류 2회"]}]},
            "review_result": {"status": "format_error", "items": None,
                              "errors": ["호출 실패: credit balance is too low", "호출 실패: credit balance is too low"]}}


def test_api_error_holds_do_not_count_toward_ratio(env):
    # A안: API 호출 실패로 생긴 held 는 비율에서 빼고 알림만 (2026-10-01)
    store, notifier = env
    posts = [_api_error_post(0, "llm"), _api_error_post(1, "credit"), _api_error_post(2, "llm"),
             {"id": "ok", "status": "approved"}]
    assert autostop.check_held_ratio(store, notifier, posts) == 0.0
    assert not autostop.is_stopped(store)
    assert "API 호출 오류로 보류된 글 3개" in notifier.sent[-1]


def test_quality_holds_still_stop_even_with_api_errors(env):
    store, notifier = env
    quality = {"id": "q", "status": "held", "guard_log": {"attempts": [{"problems": ["[R3] uncertain: 안전 암시"]}]},
               "review_result": {"status": "uncertain", "items": {}, "errors": []}}
    posts = [_api_error_post(0, "llm"), quality, dict(quality, id="q2"), {"id": "ok", "status": "approved"}]
    assert autostop.check_held_ratio(store, notifier, posts) == pytest.approx(2 / 3)
    assert autostop.is_stopped(store)


def test_only_api_errors_no_stop(env):
    store, notifier = env
    autostop.check_held_ratio(store, notifier, [_api_error_post(i, "credit") for i in range(4)])
    assert not autostop.is_stopped(store)


# 조건 1: 답글 키워드 (Step 8 답글 감시가 부른다)
@pytest.mark.parametrize("text, stopped", [
    ("이거 광고충 계정이네", True), ("스팸 신고합니다", True), ("허위 아닌가요", True),
    ("전기요 계산 도움 됐어요", False),
])
def test_reply_keywords(env, text, stopped):
    store, notifier = env
    assert autostop.check_reply_text(store, notifier, text) is stopped
    assert autostop.is_stopped(store) is stopped


# PLAN 1-3 (2026-11-11): 스팸·신고·사기는 고발하는 문맥일 때만 정지
ACCUSATIONS = [
    "이거 사기네", "완전 사기야", "사기꾼 계정", "사기 치지 마", "사기 아닌가요", "사기당했어요", "사기성 광고 같아",
    "신고합니다", "신고할게요", "신고했어요", "신고 먹였다", "신고감이네",
    "스팸 계정이네", "스팸이네", "스팸 광고 그만", "스팸 같아요", "스팸 댓글 지워",
]
EVERYDAY = [
    "난 풀무원돌얼음이제일단단하고 편의점에서사기도수월해서그냥사먹어",   # 2026-11-10 실제 오탐 답글
    "사기 전에 리뷰 많이 봐", "이 가격이면 사기엔 좀 비싸", "나는 사기 싫더라", "사기 글쎄 고민돼",
    "슬리퍼 신고 다녀", "양말 신고 자면 따뜻해",
    "스팸 구워 먹을 때 좋아", "스팸 김밥 해먹어",
]


@pytest.mark.parametrize("text", ACCUSATIONS)
def test_accusing_reply_stops_with_reply_text_in_alert(env, text):
    store, notifier = env
    detail = {"username": "someone", "permalink": "https://www.threads.com/@someone/post/x"}
    assert autostop.check_reply_text(store, notifier, text, detail)
    assert autostop.is_stopped(store)
    assert "[자동 정지]" in notifier.sent[-1] and text[:10] in notifier.sent[-1]
    assert "@someone" in notifier.sent[-1] and detail["permalink"] in notifier.sent[-1]


@pytest.mark.parametrize("text", EVERYDAY)
def test_everyday_use_only_notifies(env, text):
    store, notifier = env
    assert not autostop.check_reply_text(store, notifier, text, {"permalink": "https://t/p"})
    assert not autostop.is_stopped(store)
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("[확인 필요]")
    assert "https://t/p" in notifier.sent[0]


def test_always_stop_words_need_no_context(env):
    store, notifier = env
    assert autostop.reply_stop_words("광고충") == (["광고충"], [])
    assert autostop.reply_stop_words("허위") == (["허위"], [])
    assert autostop.reply_stop_words("사기 전에 허위 아님") == (["허위"], ["사기"])   # 하나라도 정지면 정지


def test_reply_without_stop_words_sends_nothing(env):
    store, notifier = env
    assert not autostop.check_reply_text(store, notifier, "전기요 계산 도움 됐어요")
    assert notifier.sent == []
