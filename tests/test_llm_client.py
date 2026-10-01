"""Step 3 생성용 LLM 클라이언트 테스트 (티스토리 aiProviders.ts 이식분). 실제 API 는 부르지 않는다."""
import types as pytypes

import pytest

import llm_client
from llm_client import LLMError

G, O = "gemini-2.5-flash", "gpt-4.1-mini"


@pytest.fixture
def calls(monkeypatch):
    """_gemini_complete / _openai_complete 를 가짜로 바꾸고 (제공사, 모델) 호출 순서를 기록한다."""
    log, plan = [], {"gemini": [], "openai": []}

    def fake(provider):
        def f(api_key, model, system, user, temperature):
            log.append((provider, model))
            r = plan[provider].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return f

    monkeypatch.setattr(llm_client, "_gemini_complete", fake("gemini"))
    monkeypatch.setattr(llm_client, "_openai_complete", fake("openai"))
    return pytypes.SimpleNamespace(log=log, plan=plan)


def test_configured_model_is_used(calls):
    calls.plan["gemini"] = ['{"body": "g"}']
    assert llm_client.make_complete(G, "g-key", O, "o-key")("s", "u", 0.3) == '{"body": "g"}'
    assert calls.log == [("gemini", G)]


def test_usage_limit_falls_back(calls):
    calls.plan["gemini"] = [Exception("429 RESOURCE_EXHAUSTED: quota exceeded")]
    calls.plan["openai"] = ['{"body": "o"}']
    assert llm_client.make_complete(G, "g-key", O, "o-key")("s", "u", 0.3) == '{"body": "o"}'
    assert calls.log == [("gemini", G), ("openai", O)]


def test_usage_limit_without_fallback_key_raises(calls):
    calls.plan["gemini"] = [Exception("monthly spending cap reached")]
    with pytest.raises(Exception, match="spending cap"):
        llm_client.make_complete(G, "g-key", O, None)("s", "u", 0.3)
    assert calls.log == [("gemini", G)]


def test_temporary_error_retried_once(calls):
    waits = []
    calls.plan["gemini"] = [Exception("503 UNAVAILABLE"), '{"body": "g"}']
    assert llm_client.make_complete(G, "g-key", sleep=waits.append)("s", "u", 0.3) == '{"body": "g"}'
    assert len(calls.log) == 2 and waits == [4]


def test_temporary_error_gives_up_after_retry(calls):
    # 대체 모델이 있으면 1회 재시도 후 대체로, 없으면 3회까지 재시도 후 포기
    calls.plan["gemini"] = [Exception("503")] * 4
    waits = []
    with pytest.raises(Exception, match="503"):
        llm_client.make_complete(G, "g-key", sleep=waits.append)("s", "u", 0.3)
    assert len(calls.log) == 4 and waits == [4, 8, 16]


PER_MINUTE = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content_free_tier_requests, "
              "quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Please retry in 21.18s.")
PER_DAY = ("429 RESOURCE_EXHAUSTED. Quota exceeded, quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier. "
           "Please retry in 41.6s.")


def test_per_minute_limit_waits_and_retries(calls):
    calls.plan["gemini"] = [Exception(PER_MINUTE), '{"body": "g"}']
    waits = []
    assert llm_client.make_complete(G, "g-key", sleep=waits.append)("s", "u", 0.3) == '{"body": "g"}'
    assert waits == [pytest.approx(22.18)] and len(calls.log) == 2


def test_per_minute_limit_gives_up_after_two_waits(calls):
    calls.plan["gemini"] = [Exception(PER_MINUTE)] * 3
    with pytest.raises(Exception, match="RESOURCE_EXHAUSTED"):
        llm_client.make_complete(G, "g-key", sleep=lambda s: None)("s", "u", 0.3)
    assert len(calls.log) == 3


def test_daily_limit_does_not_wait(calls):
    # 하루 한도는 기다려도 풀리지 않으므로 바로 포기 (대체 모델이 있으면 대체)
    calls.plan["gemini"] = [Exception(PER_DAY)]
    waits = []
    with pytest.raises(Exception):
        llm_client.make_complete(G, "g-key", sleep=waits.append)("s", "u", 0.3)
    assert waits == []


def test_per_minute_delay_parsing():
    assert llm_client.per_minute_retry_delay(Exception(PER_MINUTE)) == pytest.approx(22.18)
    assert llm_client.per_minute_retry_delay(Exception(PER_DAY)) is None
    assert llm_client.per_minute_retry_delay(Exception("GenerateRequestsPerMinute, retry in 300s")) == 65.0


def test_persistent_overload_falls_back(calls):
    calls.plan["gemini"] = [Exception("503 UNAVAILABLE high demand"), Exception("503 UNAVAILABLE high demand")]
    calls.plan["openai"] = ['{"body": "o"}']
    assert llm_client.make_complete(G, "g-key", O, "o-key", sleep=lambda s: None)("s", "u", 0.3) == '{"body": "o"}'
    assert calls.log == [("gemini", G), ("gemini", G), ("openai", O)]


def test_other_error_not_retried(calls):
    calls.plan["gemini"] = [Exception("400 INVALID_ARGUMENT")]
    with pytest.raises(Exception, match="400"):
        llm_client.make_complete(G, "g-key", O, "o-key")("s", "u", 0.3)
    assert len(calls.log) == 1


def test_truncated_response_not_retried(calls):
    calls.plan["gemini"] = [LLMError("[Gemini] 출력 한도에서 응답이 끊겼습니다")]
    with pytest.raises(LLMError):
        llm_client.make_complete(G, "g-key", O, "o-key")("s", "u", 0.3)
    assert len(calls.log) == 1


def test_openai_as_primary(calls):
    calls.plan["openai"] = ["{}"]
    llm_client.make_complete(O, "o-key")("s", "u", 0.3)
    assert calls.log == [("openai", O)]


def test_missing_key_or_unsupported_generator_rejected():
    with pytest.raises(LLMError, match="API 키"):
        llm_client.make_complete(G, None)
    with pytest.raises(LLMError, match="지원하지 않는"):
        llm_client.make_complete("claude-opus-5", "k")     # Claude 는 검수 전용


def test_gemini_request_has_no_search_tool_and_forces_json(monkeypatch):
    """규칙 7: 외부 숫자가 들어오지 않게 Google 검색 도구를 쓰지 않는다."""
    from google import genai

    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            cand = pytypes.SimpleNamespace(finish_reason="STOP")
            return pytypes.SimpleNamespace(text='{"body": "x"}', candidates=[cand])

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(genai, "Client", FakeClient)
    assert llm_client._gemini_complete("k", G, "sys", "user", 0.3) == '{"body": "x"}'
    config = captured["config"]
    assert captured["model"] == G
    assert not config.tools
    assert config.response_mime_type == "application/json"
    assert config.thinking_config.thinking_budget == 0
    assert config.system_instruction == "sys"


def test_gemini_truncation_detected(monkeypatch):
    from google import genai

    class FakeClient:
        def __init__(self, api_key):
            self.models = pytypes.SimpleNamespace(generate_content=lambda **kw: pytypes.SimpleNamespace(
                text='{"body": "잘린', candidates=[pytypes.SimpleNamespace(finish_reason="FinishReason.MAX_TOKENS")]))

    monkeypatch.setattr(genai, "Client", FakeClient)
    with pytest.raises(LLMError, match="끊겼습니다"):
        llm_client._gemini_complete("k", G, "s", "u", 0.3)
