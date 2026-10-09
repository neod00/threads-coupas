"""
Step 3 생성기 테스트. LLM 은 가짜 함수로 바꿔 네트워크 없이 흐름을 검증한다.

8장 체크리스트 (생성·훅)
  - 생성기가 유형별 훅 후보 3개를 hook_candidates 에 저장
  - 훅 선택이 최근 7일 최소 사용 유형을 따름 (7일치 가짜 데이터로 테스트)
그 밖에: 규칙 3 (대가성 문구·링크·훅 위치는 코드가 조립), 훅 교체 → 재생성 → held 흐름
"""
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

import generator
from products import CATEGORIES

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)      # KST 20:00
BODY = "누진 2단계 가정, 기본요금·감면 제외 추정으로 약 5만 8,600원이 추가로 나옵니다.\n\n작은 방이라면 저전력 모드부터 써보는 게 낫습니다."
HOOKS = [
    {"style": "question", "text": "2,000W 히터 한 달 전기료는 얼마일까요?"},
    {"style": "result", "text": "2,000W 히터 하루 4시간이면 한 달 240kWh"},
    {"style": "compare", "text": "히터 2,000W와 800W, 한 달 차이"},
]


class FakeLLM:
    """(system, user, temperature) 호출을 기록하고 준비된 응답을 차례로 돌려준다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, system, user, temperature):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def ok(body=BODY, hooks=HOOKS, reply_line=None, topic_tag="전기요금"):
    return {"hook_candidates": hooks, "body": body, "reply_line": reply_line, "topic_tag": topic_tag}


def gen(cfg, tariff, products, llm, **kw):
    kw.setdefault("now", NOW)
    return generator.generate_post(kw.pop("post_type", "calc"), kw.pop("picked", [products[0]]), tariff, cfg, llm, **kw)


def history(counts: dict, days_ago: float = 1, status="published"):
    """hook_style 별 개수만큼 가짜 posts 행을 만든다."""
    return [{"hook_style": style, "status": status, "created_at": (NOW - timedelta(days=days_ago)).isoformat()}
            for style, n in counts.items() for _ in range(n)]


# ── 8장: 훅 후보 3개 저장 ─────────────────────────────────────────────────

@pytest.mark.parametrize("post_type", ["calc", "compare", "tip"])
def test_three_hook_candidates_saved(cfg, tariff, products, post_type):
    picked = generator.pick_products(products, "compare", NOW.date()) if post_type == "compare" else [products[0]]
    draft = ok(body="누진 구간은 한 달 총 사용량이 기준입니다.") if post_type == "tip" else ok()
    post = gen(cfg, tariff, products, FakeLLM(draft), post_type=post_type, picked=picked)
    assert post["status"] == "draft"
    assert [h["style"] for h in post["hook_candidates"]] == ["question", "result", "compare"]
    assert all(h["text"] for h in post["hook_candidates"])
    assert post["hook_style"] in generator.HOOK_STYLES


def test_missing_hook_style_triggers_regeneration(cfg, tariff, products):
    llm = FakeLLM(ok(hooks=HOOKS[:2]), ok())
    post = gen(cfg, tariff, products, llm)
    assert post["status"] == "draft" and len(llm.calls) == 2
    assert "없음: compare" in llm.calls[1]["user"]
    assert len(post["hook_candidates"]) == 3


# ── 8장: 최근 7일 최소 사용 유형 ────────────────────────────────────────────

def test_hook_follows_least_used_style_in_last_7_days(cfg, tariff, products):
    # 최근 7일: question 3, result 1, compare 2 → result
    # 8~10일 전 result 10개는 7일 밖이라 세지 않는다
    past = (history({"question": 3, "result": 1}, days_ago=2)
            + history({"compare": 2}, days_ago=6.9)
            + history({"result": 10}, days_ago=8)
            + history({"result": 5}, days_ago=10))
    post = gen(cfg, tariff, products, FakeLLM(ok()), history=past)
    assert post["hook_style"] == "result"
    assert "\n2,000W 히터 하루 4시간이면 한 달 240kWh\n" in post["text"]


def test_hook_style_order_over_seven_fake_days():
    # 7일 동안 하루 하나씩 쓰인 기록: question 3, result 2, compare 2 → 동률(result, compare)은 정해진 순서
    past = []
    for d, style in enumerate(["question", "result", "compare", "question", "result", "compare", "question"]):
        past += history({style: 1}, days_ago=d + 0.5)
    assert generator.hook_style_order(past, NOW) == ["result", "compare", "question"]


def test_only_used_posts_count():
    # held·draft·cancelled 글의 훅은 본문에 쓰이지 않았으므로 세지 않는다
    past = history({"question": 5}, status="held") + history({"result": 1}) + history({"compare": 1}, status="approved")
    assert generator.hook_style_order(past, NOW) == ["question", "result", "compare"]


def test_empty_history_uses_fixed_order():
    assert generator.hook_style_order([], NOW) == ["question", "result", "compare"]


# ── 훅 교체 → 재생성 → held ────────────────────────────────────────────────

def test_other_hook_tried_before_regenerating(cfg, tariff, products):
    # 첫 번째로 고른 question 훅이 가드에 걸리면 LLM 을 다시 부르지 않고 result 훅으로 교체
    def guard(post):
        return ["훅에 계산 숫자가 없습니다"] if post["hook_style"] == "question" else []

    llm = FakeLLM(ok())
    post = gen(cfg, tariff, products, llm, checks=[guard])
    assert post["status"] == "draft"
    assert post["hook_style"] == "result"
    assert len(llm.calls) == 1
    assert [(a["hook_style"], bool(a["problems"])) for a in post["guard_log"]["attempts"]] == [
        ("question", True), ("result", False)]


def test_regenerates_with_feedback_when_all_hooks_fail(cfg, tariff, products):
    # 1차: question·result 훅은 가짜 검수에서, compare 훅(800W 는 이 상품 데이터에 없음)은 가드에서 걸림 → 재생성
    verdicts = iter([["검수: 단정 표현"], ["검수: 단정 표현"], []])
    llm = FakeLLM(ok(), ok())
    post = gen(cfg, tariff, products, llm, checks=[lambda p: next(verdicts)])
    assert post["status"] == "draft"
    assert [c["temperature"] for c in llm.calls] == [0.3, 0.7]
    feedback = llm.calls[1]["user"]
    assert "검수: 단정 표현" in feedback and "[G3]" in feedback      # 검수·가드 사유를 모두 돌려준다
    assert "이전 초안" in feedback and "240kWh" in feedback


def test_held_after_three_generations(cfg, tariff, products):
    llm = FakeLLM(ok(), ok(), ok())
    post = gen(cfg, tariff, products, llm, checks=[lambda p: ["501자 초과"]])
    assert post["status"] == "held"
    assert len(llm.calls) == 3
    assert len(post["guard_log"]["attempts"]) == 9          # 3회 생성 × 훅 3개
    assert post["text"].startswith(cfg.disclosure_text)     # 마지막 초안을 일일 요약·모니터링용으로 남긴다


def test_checks_run_in_order_and_stop_at_first_failure(cfg, tariff, products):
    seen = []
    first = lambda p: seen.append("guard") or []
    second = lambda p: seen.append("reviewer") or []
    gen(cfg, tariff, products, FakeLLM(ok()), checks=[first, second])
    assert seen == ["guard", "reviewer"]


def test_llm_error_is_logged_and_retried(cfg, tariff, products):
    post = gen(cfg, tariff, products, FakeLLM(RuntimeError("503 UNAVAILABLE"), ok()))
    assert post["status"] == "draft"
    assert "LLM 호출 실패: 503 UNAVAILABLE" in post["guard_log"]["attempts"][0]["problems"]


def test_all_llm_errors_give_held_post(cfg, tariff, products):
    post = gen(cfg, tariff, products, FakeLLM(RuntimeError("x"), RuntimeError("y"), RuntimeError("z")))
    assert post["status"] == "held"
    assert post["text"] == "" and post["hook_candidates"] == []


# ── 규칙 3: 조립 (문구·훅 위치·링크) ────────────────────────────────────────

def test_link_post_layout(cfg, tariff, products):
    post = gen(cfg, tariff, products, FakeLLM(ok()))
    lines = post["text"].split("\n")
    assert lines[0] == cfg.disclosure_text                  # 1줄: 대가성 문구
    assert lines[1] == HOOKS[0]["text"]                     # 2줄: 훅 (기록 없으면 question)
    assert lines[2] == ""
    assert post["text"].endswith(BODY)
    assert post["link_url"] == products[0].link and "http" not in post["text"]
    assert (post["variant"], post["reply_text"], post["has_link"]) == ("link_in_body", None, True)
    assert post["calc_payload"]["est_added_cost_won"] == 58_600
    assert (post["review_result"], post["review_model"], post["review_attempts"]) == (None, None, 0)


def test_link_in_reply_puts_disclosure_in_both(cfg, tariff, products):
    llm = FakeLLM(ok(reply_line="소비전력 표기는 상품 페이지에서 확인할 수 있습니다."))
    post = gen(cfg, tariff, products, llm, variant="link_in_reply")
    assert post["text"].split("\n")[0] == cfg.disclosure_text
    assert post["reply_text"].split("\n")[0] == cfg.disclosure_text
    assert post["reply_text"].endswith("확인할 수 있습니다.")
    assert '"링크 답글에 넣을 안내 한 문장' in llm.calls[0]["system"]


def test_missing_reply_line_triggers_regeneration(cfg, tariff, products):
    llm = FakeLLM(ok(reply_line=None), ok(reply_line="상품 페이지에서 확인할 수 있습니다."))
    post = gen(cfg, tariff, products, llm, variant="link_in_reply")
    assert post["status"] == "draft" and len(llm.calls) == 2


def test_tip_layout_hook_on_first_line(cfg, tariff, products):
    post = gen(cfg, tariff, products, FakeLLM(ok(body="같은 사용량도 누진 구간에 따라 요금이 다릅니다.")),
               post_type="tip", tip_topic=generator.TIP_TOPICS[0])
    assert post["text"].split("\n")[0] == HOOKS[0]["text"]
    assert cfg.disclosure_text not in post["text"]
    assert (post["has_link"], post["link_url"], post["variant"], post["product_ids"]) == (False, None, None, [])
    assert post["calc_payload"]["tariff"]["tiers"][1] == {"tier": 2, "upto_kwh": 400, "rate_per_kwh": 214.6}


def test_tip_payload_has_usage_but_no_cost_or_assumed_tier(tariff, products):
    # tip 은 숫자 가벼운 글: 요금 추정치·"누진 N단계 가정"은 데이터에 없다 (2026-09-28)
    p = generator.build_payload("tip", [products[0]], tariff, generator.TIP_TOPICS[0])
    assert p["example"] == {"category": "heater", "power_w": 2000, "hours_per_day": 4, "days_per_month": 30,
                            "monthly_kwh": 240}
    assert "assumed_tier" not in p["tariff"]
    flat = json.dumps(p, ensure_ascii=False)
    assert "est_added_cost" not in flat and "assumption_text" not in flat


def test_tip_with_cost_estimate_is_blocked_by_guard(cfg, tariff, products):
    # 팁 글에 요금 추정("약 5만 8,600원")을 쓰면 데이터에 없는 숫자라 가드가 막는다
    llm = FakeLLM(*[ok(body="2,000W 히터를 하루 4시간 쓰면 약 5만 8,600원이 추가됩니다.")] * 3)
    post = gen(cfg, tariff, products, llm, post_type="tip", tip_topic=generator.TIP_TOPICS[0])
    assert post["status"] == "held"
    assert any("[G3]" in p for a in post["guard_log"]["attempts"] for p in a["problems"])


def test_compare_uses_same_hours_for_both(cfg, tariff, products):
    picked = generator.pick_products(products, "compare", NOW.date())
    p = gen(cfg, tariff, products, FakeLLM(ok()), post_type="compare", picked=picked)["calc_payload"]
    assert (p["a"]["name"], p["b"]["name"]) == ("A사 전기히터", "B사 미니히터")
    assert p["a"]["hours_per_day"] == p["b"]["hours_per_day"] == 4


# ── 프롬프트 ─────────────────────────────────────────────────────────────

def test_prompt_carries_rules_and_data(cfg, tariff, products):
    llm = FakeLLM(ok())
    gen(cfg, tariff, products, llm)
    system, user = llm.calls[0]["system"], llm.calls[0]["user"]
    assert "최저가" in system and "전자파" in system                 # 금지어 (가격·안전)
    assert "이거 모르면 손해" in system                              # 금지 훅 문구
    assert "30자 이내" in system and "숫자를 1개 이상" in system       # 훅 규칙
    assert str(cfg.limits.max_body_chars) in system
    assert "누진 2단계 가정, 기본요금·감면 제외 추정" in user


def test_tip_prompt_does_not_require_numbers(cfg, tariff, products):
    llm = FakeLLM(ok())
    gen(cfg, tariff, products, llm, post_type="tip")
    assert "숫자가 없어도 됩니다" in llm.calls[0]["system"]


def test_invalid_type_or_variant(cfg, tariff, products):
    with pytest.raises(ValueError):
        gen(cfg, tariff, products, FakeLLM(), post_type="review")
    with pytest.raises(ValueError):
        gen(cfg, tariff, products, FakeLLM(), variant="link_everywhere")


# ── 응답 파싱 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    '{"body": "본문"}',
    '```json\n{"body": "본문"}\n```',
    '다음과 같습니다.\n{"body": "본문"}\n끝',
])
def test_parse_draft_variants(content):
    assert generator.parse_draft(content)["body"] == "본문"


def test_parse_draft_garbage_gives_empty():
    assert generator.parse_draft("not json") == {}


def test_hook_candidates_normalized():
    draft = {"hook_candidates": [
        {"style": "Result", "text": " 한 달\n240kWh "}, {"style": "result", "text": "중복"},
        {"style": "teaser", "text": "모르는 유형"}, "문자열", {"style": "question", "text": ""},
    ]}
    assert generator.hook_candidates(draft) == [{"text": "한 달 240kWh", "style": "result"}]


@pytest.mark.parametrize("tag, expected", [
    ("전기요금", "전기요금"), ("#전기요금", "전기요금"), ("전기.요금", None), ("A&B", None),
    ("가" * 51, None), (None, None), ("null", None),
])
def test_topic_tag_cleanup(tag, expected):
    assert generator._topic_tag(tag) == expected


# ── 상품 선택 ────────────────────────────────────────────────────────────

def test_pick_skips_recent_products(products):
    for offset in range(5):
        assert generator.pick_products(products, "calc", NOW.date(), recent_ids=["h1", "h2"], offset=offset)[0].product_id == "m1"


def test_pick_rotates_by_day(products):
    picks = {generator.pick_products(products, "calc", (NOW + timedelta(days=d)).date())[0].product_id for d in range(3)}
    assert picks == {"h1", "h2", "m1"}


def test_compare_needs_same_category_pair(products):
    with pytest.raises(ValueError, match="같은 카테고리"):
        generator.pick_products([products[0], products[2]], "compare", NOW.date())


# ── 2026-10-03 일반 독자용 개선: 요금 앞세우기·생활 장면 ─────────────────────

def test_calc_payload_has_daily_cost_and_scene(cfg, tariff, products):
    heater = next(p for p in products if p.category == "heater")
    payload = generator.build_payload("calc", [heater], tariff, day=date(2026, 10, 4))
    assert payload["est_daily_cost_label"].endswith("원")
    assert payload["situation_hint"] in generator.SCENE_HINTS["heater"]
    # 장면이 날짜마다 돌아가며 나온다
    hints = {generator.build_payload("calc", [heater], tariff, day=date(2026, 10, d))["situation_hint"] for d in range(1, 7)}
    assert len(hints) == len(generator.SCENE_HINTS["heater"])


def test_scene_hints_are_safe_for_guard_and_reviewer(cfg):
    banned = list(cfg.banned_words) + ["아이", "노인", "강아지", "반려", "숙면", "건강", "써봤", "사용해보니"]
    for category, scenes in generator.SCENE_HINTS.items():
        assert category in CATEGORIES
        for text in scenes:
            assert not re.search(r"\d", text), text           # 숫자는 계산 데이터에서만 (규칙 7)
            assert text.endswith("경우"), text                  # 경험이 아니라 가정형
            assert not [w for w in banned if w in text], text


def test_tip_payload_stays_free_of_costs(cfg, tariff, products):
    payload = generator.build_payload("tip", [products[0]], tariff, generator.TIP_TOPICS[0], day=date(2026, 10, 4))
    assert "situation_hint" not in payload
    assert "est_daily_cost_won" not in json.dumps(payload)


def test_system_prompt_puts_money_before_kwh(cfg):
    calc = generator.system_prompt(cfg, "calc", "link_in_body")
    assert "전기요금(원)을 앞세웁니다" in calc
    assert "situation_hint" in calc and "가정형" in calc
    assert "경험" in calc                                       # 사용 경험 금지는 그대로 (PLAN 규칙)
    tip = generator.system_prompt(cfg, "tip", None)
    assert "숫자가 없어도 됩니다" in tip


def test_tip_topics_are_varied_and_safe(cfg):
    topics = generator.TIP_TOPICS
    assert len(topics) >= 9 and len(set(topics)) == len(topics)   # 최근 글과 겹쳐 R9 로 막히는 일을 줄인다
    for t in topics:
        assert not re.search(r"\d", t), t                          # 주제에 숫자를 넣지 않는다 (규칙 7)
        assert not [w for w in cfg.banned_words if w in t], t
    # 하루 두 개씩 연속으로 뽑아도 같은 주제가 이틀 연속 나오지 않는다
    for ordinal in range(100, 140):
        today = {topics[(ordinal + i) % len(topics)] for i in range(2)}
        tomorrow = {topics[(ordinal + 1 + i) % len(topics)] for i in range(2)}
        assert len(today) == 2 and len(tomorrow) == 2


# ── 2026-10-09 주제 돌려 쓰기: 같은 숫자의 글이 되풀이되어 R9 로 자동 정지된 문제 ──────────

def test_hour_options():
    assert generator.hour_options(4) == [4, 2, 6]
    assert generator.hour_options(8) == [8, 4, 12]
    assert generator.hour_options(1) == [1, 2]            # 절반은 1시간 아래로 내려가지 않는다
    assert generator.hour_options(20) == [20, 10, 24]     # 24시간을 넘지 않는다


def _used(post_type, picked, tariff, hours=None, topic=None):
    return {"type": post_type, "product_ids": [p.product_id for p in picked] if post_type != "tip" else [],
            "calc_payload": generator.build_payload(post_type, picked, tariff, topic, hours=hours)}


def test_pick_subject_never_repeats_until_all_used(tariff, products):
    day, used, keys = date(2026, 10, 10), [], []
    total = len(generator.subjects(products, "calc"))
    for i in range(total):
        s = generator.pick_subject(products, "calc", day + timedelta(days=i), used)
        keys.append(s["key"])
        used.append(_used("calc", s["picked"], tariff, hours=s["hours"]))
        assert generator.subject_key(used[-1]) == s["key"]
    assert len(set(keys)) == total                         # 모든 (상품, 사용시간) 조합을 한 번씩
    # 연달아 같은 상품이 나오지 않는다 (상품이 2개 이상일 때)
    assert all(a.split("|")[1] != b.split("|")[1] for a, b in zip(keys, keys[1:]))
    # 다 쓴 뒤에는 가장 오래전에 쓴 조합으로 돌아간다
    assert generator.pick_subject(products, "calc", day, used)["key"] == keys[0]


def test_pick_subject_changes_numbers_for_same_product(tariff, products):
    heater = next(p for p in products if p.category == "heater")
    a = generator.build_payload("calc", [heater], tariff, hours=2)
    b = generator.build_payload("calc", [heater], tariff, hours=6)
    assert a["hours_per_day"] == 2 and b["hours_per_day"] == 6
    assert a["est_added_cost_won"] < b["est_added_cost_won"]


def test_pick_subject_tip_topics_rotate(tariff, products):
    used, topics = [], []
    for i in range(len(generator.TIP_TOPICS)):
        s = generator.pick_subject(products, "tip", date(2026, 10, 10), used, offset=i)
        topics.append(s["tip_topic"])
        used.append(_used("tip", [products[0]], tariff, topic=s["tip_topic"]))
    assert sorted(topics) == sorted(generator.TIP_TOPICS)


def test_user_message_shows_recent_posts_to_avoid(cfg, tariff, products):
    payload = generator.build_payload("calc", [products[0]], tariff)
    msg = generator.user_message("calc", payload, recent_texts=[f"글{i}" for i in range(9)])
    assert "최근에 올린 글" in msg and "겹치지 않게" in msg
    assert msg.count("[최근 글 ") == generator.RECENT_TEXTS_FOR_GENERATOR
    assert "최근에 올린 글" not in generator.user_message("calc", payload)
