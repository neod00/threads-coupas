"""
Step 4-B AI 검수자 테스트. Claude 는 가짜 호출로 바꿔 네트워크 없이 검증한다.

8장 체크리스트 (AI 검수자)
  - 검수자 응답 형식 오류 → 재요청 후 held
  - uncertain 판정 → held (일일 요약 표시는 Step 5 에서 digest 로 증명; 여기서는 표시에 쓸 사유가 남는지 확인)
그 밖에: 전 항목 pass → approved, 훅 문제 → 훅 교체 후 재검수, 본문 문제 → 재생성, 요청 형식
"""
import json
import types as pytypes
from datetime import datetime, timezone

import pytest

import generator
import reviewer

CALC_HOOK = "2,000W 히터 하루 4시간이면 한 달 240kWh"
CALC_BODY = (
    "누진 2단계 가정, 기본요금·감면 제외 추정으로 약 5만 8,600원이 추가로 나옵니다.\n\n"
    "같은 시간 1,000W 저전력 모드로 돌리면 사용량이 줄어듭니다.\n"
    "작은 방이라면 저전력 모드부터 써보는 게 낫습니다."
)
NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
HOOKS = [
    {"style": "question", "text": "2,000W 히터 한 달 전기료는 얼마일까요?"},
    {"style": "result", "text": CALC_HOOK},
    {"style": "compare", "text": "2,000W 히터, 한 달 240kWh 차이"},
]


def verdicts(**overrides):
    """R1~R12 전부 pass 인 판정에 일부 항목만 바꾼다. 예: verdicts(R2=("fail", False))"""
    data = {c: {"verdict": "pass", "reason": "문제 없음", "hook_only": False} for c in reviewer.ITEM_CODES}
    for code, (verdict, hook_only) in overrides.items():
        data[code] = {"verdict": verdict, "reason": f"{code} 사유", "hook_only": hook_only}
    return data


class FakeClaude:
    """(system, user) → (stop_reason, text). 응답은 dict(→JSON), 문자열, (stop_reason, text), 예외 중 하나."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, system, user):
        self.calls.append({"system": system, "user": json.loads(user)})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return r
        return "end_turn", r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


class FakeLLM:
    def __init__(self, n=3):
        self.calls = 0
        self.n = n

    def __call__(self, system, user, temperature):
        self.calls += 1
        return json.dumps({"hook_candidates": HOOKS, "body": CALC_BODY, "reply_line": None, "topic_tag": None},
                          ensure_ascii=False)


def generate(cfg, tariff, products, claude, llm=None):
    rev = reviewer.Reviewer(cfg, claude)
    check = reviewer.make_check(rev, reviewer.product_rows([products[0]]), ["최근 글 1"])
    llm = llm or FakeLLM()
    post = generator.generate_post("calc", [products[0]], tariff, cfg, llm, checks=[check], now=NOW, variant="link_in_body")
    return post, llm


# ── 8장: 형식 오류 → 재요청 후 held ─────────────────────────────────────────

def test_format_error_retried_once_then_held(cfg, tariff, products):
    claude = FakeClaude("형식이 틀린 답", '{"R1": "pass"}')
    post, llm = generate(cfg, tariff, products, claude)
    assert len(claude.calls) == 2                        # 최초 1회 + 재요청 1회
    assert post["status"] == "held"
    assert llm.calls == 1                                # 재생성하지 않고 바로 held
    assert post["review_result"]["status"] == "format_error"
    assert post["review_attempts"] == 2
    assert post["review_model"] == cfg.reviewer_model


def test_format_error_then_valid_answer_continues(cfg, tariff, products):
    claude = FakeClaude("{잘린 JSON", verdicts())
    post, _ = generate(cfg, tariff, products, claude)
    assert post["status"] == "approved"
    assert post["review_attempts"] == 2


@pytest.mark.parametrize("bad", [
    ("refusal", ""),                                            # 거부
    ("max_tokens", '{"R1": {"verdict": "pass"'),                # 잘림
    {k: v for k, v in verdicts().items() if k != "R12"},        # 항목 누락
    {**verdicts(), "R13": {"verdict": "pass", "reason": "", "hook_only": False}},     # 모르는 항목
    verdicts(R3=("maybe", False)),                              # 없는 판정 값
    RuntimeError("connection reset"),                           # 네트워크 오류
])
def test_each_format_error_kind_is_detected(cfg, bad):
    result = reviewer.Reviewer(cfg, FakeClaude(bad, bad)).review({"type": "calc", "text": "x"})
    assert result.status == "format_error"
    assert result.requests == 2 and len(result.errors) == 2


# ── 8장: uncertain → held ───────────────────────────────────────────────────

def test_uncertain_goes_to_held_without_regeneration(cfg, tariff, products):
    claude = FakeClaude(verdicts(R3=("uncertain", False)))
    post, llm = generate(cfg, tariff, products, claude)
    assert post["status"] == "held"
    assert llm.calls == 1 and len(claude.calls) == 1     # 훅 교체·재생성 없이 바로 held
    # 일일 요약에 보여 줄 사유가 남는다
    assert post["review_result"]["status"] == "uncertain"
    assert post["review_result"]["items"]["R3"] == {"verdict": "uncertain", "reason": "R3 사유", "hook_only": False}
    assert "[R3] uncertain: R3 사유" in post["guard_log"]["attempts"][-1]["problems"]


def test_uncertain_even_with_fails_is_held(cfg, tariff, products):
    post, _ = generate(cfg, tariff, products, FakeClaude(verdicts(R2=("fail", False), R4=("uncertain", False))))
    assert post["status"] == "held"


# ── 처리 흐름 ────────────────────────────────────────────────────────────────

def test_all_pass_becomes_approved(cfg, tariff, products):
    claude = FakeClaude(verdicts())
    post, _ = generate(cfg, tariff, products, claude)
    assert post["status"] == "approved"
    assert post["review_result"]["status"] == "approved"
    assert post["review_attempts"] == 1
    # 검수자가 받은 입력: 본문, calc_payload, 상품 행, 최근 글
    sent = claude.calls[0]["user"]
    assert sent["post"]["text"] == post["text"]
    assert sent["calc_payload"]["est_added_cost_won"] == 58_600
    assert sent["products"][0]["name"] == "A사 전기히터"
    assert sent["recent_posts"] == ["최근 글 1"]


def test_guard_only_gives_draft_not_approved(cfg, tariff, products):
    # 규칙 4: 검수자를 거치지 않은 글은 approved 가 되지 않는다
    post = generator.generate_post("calc", [products[0]], tariff, cfg, FakeLLM(), now=NOW, variant="link_in_body")
    assert post["status"] == "draft"


def test_hook_only_fail_swaps_hook_and_rereviews(cfg, tariff, products):
    claude = FakeClaude(verdicts(R12=("fail", True)), verdicts())
    post, llm = generate(cfg, tariff, products, claude)
    assert post["status"] == "approved"
    assert llm.calls == 1 and len(claude.calls) == 2
    assert post["hook_style"] == "result"                 # question 훅이 걸려 result 로 교체
    assert post["review_attempts"] == 2


def test_body_fail_regenerates_without_trying_other_hooks(cfg, tariff, products):
    claude = FakeClaude(verdicts(R2=("fail", False)), verdicts())
    post, llm = generate(cfg, tariff, products, claude)
    assert post["status"] == "approved"
    assert llm.calls == 2 and len(claude.calls) == 2      # 본문 문제는 훅을 바꿔도 안 풀리므로 바로 재생성
    assert [a["hook_style"] for a in post["guard_log"]["attempts"]] == ["question", "question"]


def test_fail_after_two_regenerations_is_held(cfg, tariff, products):
    claude = FakeClaude(*[verdicts(R4=("fail", False))] * 3)
    post, llm = generate(cfg, tariff, products, claude)
    assert post["status"] == "held"
    assert llm.calls == 3 and post["review_attempts"] == 3


def test_reviewer_only_sees_guard_passed_posts(cfg, tariff, products):
    # 가드에 걸리는 훅(compare: 이 상품 데이터에 없는 숫자 없음 → 여기서는 숫자 없는 훅)은 검수 요청을 보내지 않는다
    hooks = [{"style": s, "text": "전기히터를 하루 종일 켜 두면 한 달 요금은 과연 얼마나 나올까요?"} for s in ("question", "result", "compare")]

    class NoNumberLLM(FakeLLM):
        def __call__(self, system, user, temperature):
            self.calls += 1
            return json.dumps({"hook_candidates": hooks, "body": CALC_BODY}, ensure_ascii=False)

    claude = FakeClaude()
    post, _ = generate(cfg, tariff, products, claude, NoNumberLLM())
    assert post["status"] == "held" and claude.calls == []


# ── 프롬프트·요청 ────────────────────────────────────────────────────────────

def test_system_prompt_has_rules_and_disclosure(cfg):
    system = reviewer.system_prompt(cfg)
    assert "10. **자동 정지.**" in system and "1. **한 번의 실행은 계정 1개.**" in system     # PLAN 1장 전문
    assert cfg.disclosure_text in system
    assert "문제를 찾는 것이 임무" in system
    assert "{" not in system.replace("{{", "")                              # 채워지지 않은 자리표시자 없음


def test_prompt_hash_changes_with_prompt_or_plan(cfg, tmp_path):
    base = reviewer.prompt_hash(cfg)
    prompt = tmp_path / "reviewer.md"
    prompt.write_text(reviewer.PROMPT_PATH.read_text(encoding="utf-8") + "\n추가 지시", encoding="utf-8")
    assert reviewer.prompt_hash(cfg, prompt_path=prompt) != base
    plan = tmp_path / "PLAN.md"
    plan.write_text(reviewer.PLAN_PATH.read_text(encoding="utf-8").replace("한 번의 실행은 계정 1개.", "한 번의 실행은 계정 2개."), encoding="utf-8")
    assert reviewer.prompt_hash(cfg, plan_path=plan) != base


def test_anthropic_request_shape(monkeypatch):
    import anthropic

    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return pytypes.SimpleNamespace(stop_reason="end_turn",
                                           content=[pytypes.SimpleNamespace(type="text", text="{}")])

    class FakeClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    call = reviewer.make_anthropic_call("k", "claude-opus-5")
    assert call("sys", "user") == ("end_turn", "{}")
    assert captured["model"] == "claude-opus-5"
    assert captured["system"] == "sys"
    assert captured["output_config"]["format"] == {"type": "json_schema", "schema": reviewer.OUTPUT_SCHEMA}
    assert "temperature" not in captured                  # Opus 5 는 temperature 를 받지 않는다
    assert "fallbacks" not in captured                    # 시험을 치르지 않은 모델로 넘어가지 않게

    captured.clear()
    reviewer.make_anthropic_call("k", "claude-haiku-4-5")("sys", "user")
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["extra_body"] == {"temperature": 0}   # PLAN Step 4-B: temperature 0 (SDK 1.x 는 extra_body 로)
    assert "temperature" not in captured                  # SDK 1.x 는 temperature 인자가 없다 (TypeError)

    captured.clear()
    reviewer.make_anthropic_call("k", "claude-sonnet-5")("sys", "user")
    assert captured["thinking"] == {"type": "adaptive"}   # 짧게 생각하고 판정 (오판 줄이기)
    assert captured["output_config"] == {"format": {"type": "json_schema", "schema": reviewer.OUTPUT_SCHEMA},
                                         "effort": "low"}
    assert "extra_body" not in captured                   # Sonnet 5 는 temperature 를 보내지 않는다


def test_request_params_change_invalidates_prompt_hash(cfg, monkeypatch):
    import dataclasses

    claude = dataclasses.replace(cfg, reviewer_model="claude-sonnet-5")
    base = reviewer.prompt_hash(claude)
    monkeypatch.setattr(reviewer, "MAX_TOKENS", 1234)
    assert reviewer.prompt_hash(claude) != base
    # 검수 제공사가 바뀌면(요청 설정이 바뀌므로) 해시도 바뀐다
    assert reviewer.prompt_hash(dataclasses.replace(cfg, reviewer_model="gpt-4.1")) != reviewer.prompt_hash(claude)


def test_openai_review_request_shape(monkeypatch):
    import openai

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = pytypes.SimpleNamespace(content="{}", refusal=None)
            return pytypes.SimpleNamespace(choices=[pytypes.SimpleNamespace(message=msg, finish_reason="stop")])

    class FakeOpenAI:
        def __init__(self, api_key, timeout=None):
            self.chat = pytypes.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    assert reviewer.make_openai_call("k", "gpt-4.1")("sys", "user") == ("end_turn", "{}")
    assert captured["temperature"] == 0                                    # PLAN Step 4-B
    assert captured["response_format"]["json_schema"] == {"name": "review", "strict": True,
                                                          "schema": reviewer.OUTPUT_SCHEMA}
    assert captured["messages"][0] == {"role": "system", "content": "sys"}

    captured.clear()
    reviewer.make_openai_call("k", "gpt-5.4-mini")("sys", "user")
    assert captured["reasoning_effort"] == "medium"                        # 추론 모델: low 는 시험에서 문제 글을 가끔 놓쳐 medium (2026-10-10)
    assert "temperature" not in captured                                   # 추론 모델은 temperature 를 받지 않는다


def test_openai_refusal_and_truncation_are_format_errors(monkeypatch, cfg):
    import openai

    def fake(finish, refusal=None):
        class C:
            def create(self, **kw):
                msg = pytypes.SimpleNamespace(content="{\"R1\"", refusal=refusal)
                return pytypes.SimpleNamespace(choices=[pytypes.SimpleNamespace(message=msg, finish_reason=finish)])

        return lambda api_key, timeout=None: pytypes.SimpleNamespace(chat=pytypes.SimpleNamespace(completions=C()))

    for finish, refusal in (("length", None), ("stop", "I can't help")):
        monkeypatch.setattr(openai, "OpenAI", fake(finish, refusal))
        result = reviewer.Reviewer(cfg, reviewer.make_openai_call("k", "gpt-4.1")).review({"type": "calc", "text": "x"})
        assert result.status == "format_error"


def test_r1_r8_decided_by_code(cfg, tariff, products):
    # LLM 이 "가정 문구가 다르다"고 잘못 막아도, 코드가 문구를 확인하면 R8 은 pass 가 된다
    import generator

    payload = generator.build_payload("calc", [products[0]], tariff)
    post = {"type": "calc", "has_link": True, "link_url": products[0].link, "variant": "link_in_body",
            "text": f"{cfg.disclosure_text}\n{CALC_HOOK}\n\n{CALC_BODY}", "calc_payload": payload}
    wrong = verdicts(R8=("fail", False), R1=("uncertain", False))
    result = reviewer.Reviewer(cfg, FakeClaude(wrong)).review(post)
    assert result.status == "approved"
    assert result.items["R8"]["reason"].startswith("(코드 확인)")
    # 반대로 가정 문구가 빠졌는데 LLM 이 통과시켜도 코드가 막는다
    missing = dict(post, text=post["text"].replace("누진 2단계 가정, 기본요금·감면 제외 추정으로 ", ""))
    result = reviewer.Reviewer(cfg, FakeClaude(verdicts())).review(missing)
    assert result.status == "fail" and result.items["R8"]["verdict"] == "fail"
    # 대가성 문구가 빠지면 R1 fail
    no_disclosure = dict(post, text=post["text"].split("\n", 1)[1])
    assert reviewer.Reviewer(cfg, FakeClaude(verdicts())).review(no_disclosure).items["R1"]["verdict"] == "fail"


def test_make_call_picks_provider_and_needs_key(cfg):
    from settings import ConfigError, Secrets

    with pytest.raises(ConfigError, match="API 키가 없습니다"):
        reviewer.make_call("gpt-4.1", Secrets())
    assert callable(reviewer.make_call("gpt-4.1", Secrets(openai_api_key="k")))
    assert callable(reviewer.make_call("claude-sonnet-5", Secrets(anthropic_api_key="k")))


# ── 2026-10-09: R9(반복)도 코드가 판정한다 ───────────────────────────────────

def _calc_post(cfg, tariff, products, hook=CALC_HOOK, body=CALC_BODY):
    import generator

    return {"type": "calc", "has_link": True, "link_url": products[0].link, "variant": "link_in_body",
            "text": f"{cfg.disclosure_text}\n{hook}\n\n{body}",
            "calc_payload": generator.build_payload("calc", [products[0]], tariff)}


def test_r9_llm_opinion_is_ignored(cfg, tariff, products):
    # LLM 이 "비슷해 애매함"이라고 해도, 최근 글과 다르면 코드가 pass 로 둔다 (2026-10-04 자동 정지 원인)
    post = _calc_post(cfg, tariff, products)
    recent = [f"{cfg.disclosure_text}\n가습기 저녁마다 켜면 한 달 얼마일까요?\n\n전혀 다른 내용의 글입니다. 거실에서 켜 두는 경우입니다."]
    result = reviewer.Reviewer(cfg, FakeClaude(verdicts(R9=("uncertain", False)))).review(post, recent_posts=recent)
    assert result.status == "approved"
    assert result.items["R9"]["reason"].startswith("(코드 확인)")


def test_r9_near_duplicate_is_blocked_even_if_llm_passes(cfg, tariff, products):
    post = _calc_post(cfg, tariff, products)
    almost = post["text"].replace("낫습니다", "좋습니다")
    result = reviewer.Reviewer(cfg, FakeClaude(verdicts())).review(post, recent_posts=["다른 글", almost])
    assert result.status == "fail"
    assert result.items["R9"]["verdict"] == "fail" and result.items["R9"]["hook_only"] is False
    assert not result.hook_fixable


def test_r9_same_hook_is_hook_only(cfg, tariff, products):
    post = _calc_post(cfg, tariff, products)
    recent = [f"{cfg.disclosure_text}\n{CALC_HOOK}\n\n본문은 완전히 다른 이야기입니다. 욕실에서 잠깐 켜는 경우를 가정했습니다."]
    result = reviewer.Reviewer(cfg, FakeClaude(verdicts())).review(post, recent_posts=recent)
    assert result.items["R9"] == {"verdict": "fail", "hook_only": True, "reason": "(코드 확인) 훅이 최근 글의 훅과 같습니다."}
    assert result.hook_fixable                      # 생성기가 다른 훅 후보로 바꿔 본다


def test_r9_shared_disclosure_does_not_count_as_similarity(cfg):
    a = {"type": "calc", "text": f"{cfg.disclosure_text}\n가\n\n나"}
    r = reviewer.repetition_check(a, [f"{cfg.disclosure_text}\n다\n\n라"], cfg)
    assert r["verdict"] == "pass" and "0%" in r["reason"]


def test_reviewer_prompt_tells_llm_r9_is_code_checked(cfg):
    assert "R9 최근 글과의 반복 여부는 코드가" in reviewer.system_prompt(cfg)
