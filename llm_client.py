"""
생성용 LLM 호출 (PLAN Step 3). 티스토리 앱 api/_lib/aiProviders.ts·geminiErrors.ts 를 Python 으로 옮겼다.

가져온 것
  - 기본 모델이 사용량 한도(quota·spend cap·429 exceeded)에 걸리면 대체 모델로 넘어간다 (대체 키가 있을 때만)
  - 서버 혼잡(503) 같은 일시 오류가 재시도 뒤에도 이어져도 대체 모델로 넘어간다
  - 503·unavailable·timeout 같은 일시 오류만 지수 백오프로 재시도
  - 출력 한도에서 끊긴 응답은 실패로 처리 (잘린 글이 초안이 되지 않게)
바꾼 것
  - Google 검색 도구를 쓰지 않는다. 게시물 숫자의 출처는 계산기 결과(calc_payload)뿐이어야 한다 (규칙 7).
  - JSON 응답을 강제하고, 추론(thinking)을 끄고, 출력 토큰을 짧게 잡는다.
  - 모델 이름은 config.yaml(generator_model, generator_fallback_model)에서 받는다.
    검수 모델과 회사가 다른지는 settings.parse_config 가 이미 확인했다.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable

from settings import provider_of

log = logging.getLogger("threads_coupas")

MAX_OUTPUT_TOKENS = 1024
MAX_RETRIES = 1             # 대체 모델이 있을 때 일시 오류 재시도 횟수 (없으면 3)
MAX_RATE_WAITS = 2          # 분당 한도에 걸렸을 때 기다렸다 재시도하는 횟수
GEMINI_TIMEOUT_MS = 90_000  # 응답 없이 멈춘 요청을 끊는 시간 (제한이 없으면 40분 작업 제한에 걸려 취소됨, 2026-10-23)


class LLMError(RuntimeError):
    pass


# ── 오류 분류 (geminiErrors.ts) ────────────────────────────────────────

def _error_text(error: BaseException) -> str:
    parts = [str(error)] + [str(getattr(error, a, "") or "") for a in ("status", "code", "message")]
    return "\n".join(p for p in parts if p).lower()


def is_usage_limit_error(error: BaseException) -> bool:
    text = _error_text(error)
    return (
        "resource_exhausted" in text
        or "monthly spending cap" in text
        or "spend cap" in text
        or "quota" in text
        or ("429" in text and "exceeded" in text)
    )


def per_minute_retry_delay(error: BaseException) -> float | None:
    """분당 요청 한도(무료 요금제 5회/분 등)에 걸렸으면 기다릴 초. 하루 한도·지출 한도면 None (기다려도 안 풀림)."""
    text = _error_text(error)
    if "perminute" not in text.replace("_", "").replace(" ", ""):
        return None
    m = re.search(r"retry in ([\d.]+)s", text) or re.search(r"retrydelay'?\"?:\s*'?\"?(\d+)s", text.replace(" ", ""))
    delay = float(m.group(1)) if m else 30.0
    return min(delay + 1, 65.0)


def is_retryable_error(error: BaseException) -> bool:
    if is_usage_limit_error(error):
        return False
    text = _error_text(error)
    return any(k in text for k in ("503", "unavailable", "timeout", "timed out", "econnreset", "etimedout"))


# ── 제공사별 호출 ─────────────────────────────────────────────────────

def _gemini_complete(api_key: str, model: str, system: str, user: str, temperature: float) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    finish = str(getattr((resp.candidates or [None])[0], "finish_reason", "") or "")
    if "MAX_TOKENS" in finish:
        raise LLMError("[Gemini] 출력 한도에서 응답이 끊겼습니다")
    if not resp.text:
        raise LLMError(f"[Gemini] 빈 응답 (finish_reason={finish or '없음'})")
    return resp.text


def _openai_complete(api_key: str, model: str, system: str, user: str, temperature: float) -> str:
    from openai import OpenAI

    resp = OpenAI(api_key=api_key, timeout=60).chat.completions.create(
        model=model,
        temperature=temperature,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise LLMError("[OpenAI] 출력 한도에서 응답이 끊겼습니다")
    if not choice.message.content:
        raise LLMError("[OpenAI] 빈 응답")
    return choice.message.content


_PROVIDERS = {"google": "_gemini_complete", "openai": "_openai_complete"}


def _call(provider: str, api_key: str, model: str, system: str, user: str, temperature: float) -> str:
    # 모듈 속성으로 찾아야 테스트에서 가짜 함수로 바꿀 수 있다.
    return globals()[_PROVIDERS[provider]](api_key, model, system, user, temperature)


# ── 기본 모델 + 대체 모델 (generateContentWithAiFallback) ─────────────────

CompleteFn = Callable[[str, str, float], str]   # (system, user, temperature) -> JSON 문자열


def make_complete(model: str, api_key: str | None, fallback_model: str | None = None,
                  fallback_api_key: str | None = None,
                  sleep: Callable[[float], None] = time.sleep) -> CompleteFn:
    provider = provider_of(model)
    if provider not in _PROVIDERS:
        raise LLMError(f"생성 모델로 지원하지 않는 제공사입니다: {model}")
    if not api_key:
        raise LLMError(f"{model} 의 API 키가 없습니다")
    fallback = None
    if fallback_model and fallback_api_key:
        fb_provider = provider_of(fallback_model)
        if fb_provider not in _PROVIDERS:
            raise LLMError(f"대체 모델로 지원하지 않는 제공사입니다: {fallback_model}")
        fallback = (fb_provider, fallback_api_key, fallback_model)

    # 대체 모델이 없으면 일시 오류(503)를 더 끈기 있게 재시도한다 (4·8·16초)
    max_retries = MAX_RETRIES if fallback else 3

    def complete(system: str, user: str, temperature: float) -> str:
        attempt, rate_waits = 0, 0
        while True:
            try:
                return _call(provider, api_key, model, system, user, temperature)
            except LLMError:
                raise
            except Exception as e:  # SDK 오류 형식이 버전마다 달라 문자열로 분류한다
                delay = per_minute_retry_delay(e)
                if delay is not None and rate_waits < MAX_RATE_WAITS:
                    # 분당 한도는 잠깐 기다리면 풀린다 (2026-10-02: 무료 요금제 5회/분으로 held 발생)
                    rate_waits += 1
                    log.warning(f"[LLM] {model} 분당 요청 한도, {delay:.0f}초 기다렸다 재시도 ({rate_waits}/{MAX_RATE_WAITS})")
                    sleep(delay)
                    continue
                if is_usage_limit_error(e) and fallback:
                    log.warning(f"[LLM] {model} 사용량 한도 → {fallback[2]} 로 대체")
                    return _call(fallback[0], fallback[1], fallback[2], system, user, temperature)
                attempt += 1
                if attempt > max_retries and is_retryable_error(e) and fallback:
                    # 서버 혼잡(503)이 재시도 뒤에도 이어지면 대체 모델로 넘어간다 (2026-09-28: Gemini 503 로 held 발생)
                    log.warning(f"[LLM] {model} 일시 오류가 계속됨 → {fallback[2]} 로 대체")
                    return _call(fallback[0], fallback[1], fallback[2], system, user, temperature)
                if attempt > max_retries or not is_retryable_error(e):
                    raise
                wait = 2 ** attempt * 2
                log.warning(f"[LLM] {model} 일시 오류, {wait}초 후 재시도 ({attempt}/{max_retries}): {str(e)[:120]}")
                sleep(wait)

    return complete
