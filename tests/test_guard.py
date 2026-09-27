"""
Step 4 코드 가드 테스트.

8장 체크리스트 (가드)
  - 대가성 문구 누락 / 금지어 / 입력에 없는 숫자 / 501자 / 이모지 / URL 2개 → 모두 차단
  - 훅: 숫자 없는 훅 / 31자 훅 / 금지 훅 문구 → 모두 차단
  - 가드 실패 시 다른 훅 후보로 먼저 교체 시도 (실제 가드 + 생성기)
"""
import json
from datetime import datetime, timezone

import pytest

import generator
import guard

LINK = "https://link.coupang.com/a/aaa111"
CALC_HOOK = "2,000W 히터 하루 4시간이면 한 달 240kWh"
CALC_BODY = (
    "누진 2단계 가정, 기본요금·감면 제외 추정으로 약 5만 8,600원이 추가로 나옵니다.\n\n"
    "같은 시간 1,000W 저전력 모드로 돌리면 사용량이 줄어듭니다.\n"
    "작은 방이라면 저전력 모드부터 써보는 게 낫습니다."
)
REPLY_LINE = "소비전력 표기는 상품 페이지에서 확인할 수 있습니다."


@pytest.fixture
def calc_post(cfg, tariff, products):
    return {
        "type": "calc",
        "text": f"{cfg.disclosure_text}\n{CALC_HOOK}\n\n{CALC_BODY}",
        "reply_text": None, "has_link": True, "link_url": LINK, "variant": "link_in_body",
        "calc_payload": generator.build_payload("calc", [products[0]], tariff),
    }


@pytest.fixture
def compare_post(cfg, tariff, products):
    # a 240kWh 58,600원 / b 96kWh 23,400원 / 차이 35,200원
    body = ("A사 전기히터는 한 달 240kWh로 약 5만 8,600원, B사 미니히터는 96kWh로 약 2만 3,400원입니다.\n"
            "누진 2단계 가정, 기본요금·감면 제외 추정입니다.\n\n책상 밑만 데운다면 소형 쪽이 요금 부담이 적습니다.")
    return {
        "type": "compare",
        "text": f"{cfg.disclosure_text}\n두 히터 한 달 전기료 차이 약 3만 5,200원\n\n{body}",
        "reply_text": None, "has_link": True, "link_url": LINK, "variant": "link_in_body",
        "calc_payload": generator.build_payload("compare", products[:2], tariff),
    }


@pytest.fixture
def tip_post(tariff, products):
    return {
        "type": "tip",
        "text": ("겨울 전기요금이 갑자기 뛰는 건 누진 구간 때문입니다\n\n"
                 "사용량이 200kWh와 400kWh를 넘을 때마다 단가가 올라갑니다 (2026년 10월 1일 요금표 기준).\n"
                 "지난달 고지서의 사용량부터 확인해 보세요."),
        "reply_text": None, "has_link": False, "link_url": None, "variant": None,
        "calc_payload": generator.build_payload("tip", [products[0]], tariff, generator.TIP_TOPICS[0]),
    }


def codes(problems):
    return {p[1:3] for p in problems}


# ── 정상 글은 통과 ───────────────────────────────────────────────────────

def test_good_posts_pass(cfg, calc_post, compare_post, tip_post):
    for post in (calc_post, compare_post, tip_post):
        assert guard.check_post(post, cfg) == [], post["type"]


def test_good_link_in_reply_passes(cfg, calc_post):
    calc_post.update(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n{REPLY_LINE}")
    assert guard.check_post(calc_post, cfg) == []


# ── 8장: 기본 6종 차단 ──────────────────────────────────────────────────

def _drop_first_line(post, cfg):
    post["text"] = post["text"].split("\n", 1)[1]


def _banned_word(post, cfg):
    post["text"] = post["text"].replace("써보는 게 낫습니다", "써보는 게 최저가보다 낫습니다")


def _unknown_number(post, cfg):
    post["text"] = post["text"].replace("5만 8,600원", "7만 2,300원")


def _over_500(post, cfg):
    post["text"] += "\n" + "가" * (501 - len(post["text"]) - 1)
    assert len(post["text"]) == 501


def _emoji(post, cfg):
    post["text"] = post["text"].replace("낫습니다.", "낫습니다 🔥")


def _two_urls(post, cfg):
    post["text"] += "\n자세한 내용 https://example.com/heater"


@pytest.mark.parametrize("mutate, code", [
    (_drop_first_line, "G2"),       # 대가성 문구 누락
    (_banned_word, "G4"),           # 금지어
    (_unknown_number, "G3"),        # 입력에 없는 숫자
    (_over_500, "G1"),              # 501자
    (_emoji, "G1"),                 # 이모지
    (_two_urls, "G5"),              # URL 2개 (본문 URL + 링크 첨부)
])
def test_checklist_basic_violations_blocked(cfg, calc_post, mutate, code):
    mutate(calc_post, cfg)
    problems = guard.check_post(calc_post, cfg)
    assert problems, "차단되지 않음"
    assert code in codes(problems), problems


def test_exactly_500_chars_allowed(cfg, calc_post):
    calc_post["text"] += "\n" + "가" * (500 - len(calc_post["text"]) - 1)
    assert len(calc_post["text"]) == 500
    assert "G1" not in codes(guard.check_post(calc_post, cfg))


# ── 8장: 훅 3종 차단 ────────────────────────────────────────────────────

def _with_hook(post, hook):
    lines = post["text"].split("\n")
    lines[1] = hook
    post["text"] = "\n".join(lines)


def test_hook_without_number_blocked(cfg, calc_post):
    _with_hook(calc_post, "전기히터 한 달 요금은 얼마일까요?")
    problems = guard.check_post(calc_post, cfg)
    assert any(p.startswith("[G7]") and "숫자가 없습니다" in p for p in problems)


def test_31_char_hook_blocked(cfg, calc_post):
    hook = "240kWh " + "가" * 24
    assert len(hook) == 31
    _with_hook(calc_post, hook)
    assert any(p.startswith("[G7]") and "31자" in p for p in guard.check_post(calc_post, cfg))


def test_30_char_hook_allowed(cfg, calc_post):
    hook = "240kWh " + "가" * 23
    assert len(hook) == 30
    _with_hook(calc_post, hook)
    assert "G7" not in codes(guard.check_post(calc_post, cfg))


@pytest.mark.parametrize("where", ["hook", "body"])
def test_banned_hook_phrase_blocked_anywhere(cfg, calc_post, where):
    if where == "hook":
        _with_hook(calc_post, "이거 모르면 손해, 한 달 240kWh")
    else:
        calc_post["text"] = calc_post["text"].replace("작은 방이라면", "충격적이게도 작은 방이라면")
    assert "G8" in codes(guard.check_post(calc_post, cfg))


def test_tip_hook_is_first_line_and_needs_no_number(cfg, tip_post):
    assert guard.hook_line(tip_post) == "겨울 전기요금이 갑자기 뛰는 건 누진 구간 때문입니다"
    assert guard.check_post(tip_post, cfg) == []
    tip_post["text"] = "가" * 31 + tip_post["text"][tip_post["text"].index("\n"):]
    assert "G7" in codes(guard.check_post(tip_post, cfg))


# ── 8장: 가드 실패 시 다른 훅 후보로 먼저 교체 ──────────────────────────────

class FakeLLM:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, system, user, temperature):
        self.calls.append(user)
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


def draft(hooks):
    return {"hook_candidates": hooks, "body": CALC_BODY, "reply_line": None, "topic_tag": None}


NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)


def test_guard_failure_swaps_hook_before_regenerating(cfg, tariff, products):
    # 기록이 없으면 question 을 먼저 쓴다. question 훅은 숫자가 없어 G7 → LLM 재호출 없이 result 로 교체
    llm = FakeLLM(draft([
        {"style": "question", "text": "전기히터 한 달 요금은 얼마일까요?"},
        {"style": "result", "text": CALC_HOOK},
        {"style": "compare", "text": "두 방식의 한 달 차이는?"},
    ]))
    post = generator.generate_post("calc", [products[0]], tariff, cfg, llm, now=NOW)
    assert post["status"] == "draft"
    assert post["hook_style"] == "result"
    assert post["text"].split("\n")[1] == CALC_HOOK
    assert len(llm.calls) == 1
    log = post["guard_log"]["attempts"]
    assert [a["hook_style"] for a in log] == ["question", "result"]
    assert any(p.startswith("[G7]") for p in log[0]["problems"]) and log[1]["problems"] == []


def test_all_hooks_fail_then_regenerates_with_guard_feedback(cfg, tariff, products):
    no_numbers = [{"style": s, "text": "전기히터 한 달 요금은?"} for s in ("question", "result", "compare")]
    good = [{"style": s, "text": CALC_HOOK} for s in ("question", "result", "compare")]
    llm = FakeLLM(draft(no_numbers), draft(good))
    post = generator.generate_post("calc", [products[0]], tariff, cfg, llm, now=NOW)
    assert post["status"] == "draft"
    assert len(llm.calls) == 2
    assert "[G7]" in llm.calls[1]                     # 가드 사유를 생성기에 돌려준다
    assert [a["hook_style"] for a in post["guard_log"]["attempts"]] == ["question", "result", "compare", "question"]


def test_guard_cannot_be_skipped_by_caller(cfg, tariff, products):
    # checks 를 비워도 코드 가드는 항상 돈다
    llm = FakeLLM(*[draft([{"style": s, "text": "최저가 확인하세요"} for s in ("question", "result", "compare")])] * 3)
    post = generator.generate_post("calc", [products[0]], tariff, cfg, llm, checks=(), now=NOW)
    assert post["status"] == "held"


# ── 그 밖의 규칙 ────────────────────────────────────────────────────────

def test_reply_with_link_needs_disclosure(cfg, calc_post):
    calc_post.update(variant="link_in_reply", reply_text=REPLY_LINE)
    problems = guard.check_post(calc_post, cfg)
    assert any("답글 첫 줄" in p for p in problems)


def test_link_in_reply_still_needs_disclosure_in_body(cfg, calc_post):
    calc_post.update(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n{REPLY_LINE}")
    calc_post["text"] = calc_post["text"].split("\n", 1)[1]
    assert any("본문 첫 줄" in p for p in guard.check_post(calc_post, cfg))


def test_url_in_tip_text_counts_as_link(cfg, tip_post):
    tip_post["text"] += "\nhttps://link.coupang.com/a/zzz"
    assert "G2" in codes(guard.check_post(tip_post, cfg))


def test_missing_assumption_blocked(cfg, calc_post, compare_post):
    for post in (calc_post, compare_post):
        post["text"] = post["text"].replace("누진 2단계 가정, 기본요금·감면 제외 추정", "대략 추정")
        assert "G6" in codes(guard.check_post(post, cfg))


def test_reply_is_checked_too(cfg, calc_post):
    calc_post.update(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n3일 안에 확인하세요 👍")
    problems = guard.check_post(calc_post, cfg)
    assert any(p.startswith("[G3] 답글") for p in problems)
    assert any(p.startswith("[G1] 답글") for p in problems)


@pytest.mark.parametrize("emoji", ["☀", "❤️", "👍", "⭐", "✔", "🇰🇷"])
def test_emoji_variants_blocked(cfg, calc_post, emoji):
    calc_post["text"] += emoji
    assert "G1" in codes(guard.check_post(calc_post, cfg))


def test_ordinary_symbols_not_emoji(cfg, calc_post):
    calc_post["text"] += "\n요금·사용량 → 월 기준 (추정)"
    assert "G1" not in codes(guard.check_post(calc_post, cfg))


def test_small_numbers_are_not_free(cfg, calc_post):
    # column_guard.py 는 0~12 를 무조건 허용했지만, 규칙 7 에 따라 여기서는 입력에 있어야 한다
    calc_post["text"] += "\n3일만 써도 차이가 납니다."
    assert any(p.startswith("[G3]") and ": 3 " in p + " " for p in guard.check_post(calc_post, cfg))


@pytest.mark.parametrize("phrase", ["58,600원", "약 5만 8,600원", "240kWh", "하루 4시간", "30일", "214.6원", "1,000W"])
def test_payload_number_formats_allowed(cfg, calc_post, phrase):
    calc_post["text"] += f"\n{phrase}"
    assert "G3" not in codes(guard.check_post(calc_post, cfg))


def test_empty_text_blocked(cfg, calc_post):
    calc_post["text"] = "  "
    assert guard.check_post(calc_post, cfg) == ["[G1] 본문이 비어 있습니다"]


def test_allowed_numbers_conversions():
    allowed = guard.allowed_numbers({"cost": 58600, "rate": 214.6, "date": "2026-10-01", "label": "5만 8,600원"})
    assert {"58600", "5", "6", "8600", "214.6", "214", "215", "2026", "10", "1"} <= allowed
    assert "3" not in allowed and "12" not in allowed
