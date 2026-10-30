"""
AI 검수자 (PLAN v2 Step 4-B). 로옥션 column_guard.py 의 llm_factcheck(문장별 JSON 판정)를 확장했다.

- 생성 모델과 다른 회사의 모델(config.reviewer_model)을 쓴다. settings 가 제공사 분리를 이미 확인했다.
  2026-10-02 부터 임시로 OpenAI gpt-5.4-mini (reasoning_effort low): Anthropic 크레딧이 콘솔에는 보이는데 API 에서 거절돼서.
  (gpt-4.1 은 정상 글 10/10 이었지만 문제 글 P10 기간 오류를 놓쳤다.)
  해결되면 Claude Sonnet 5 로 되돌리고 재시험한다 (make_call 이 제공사에 맞춰 고른다).
- JSON 스키마(output_config.format)로 R1~R12 판정을 받는다. 형식이 틀리면 1회 재요청, 그래도 틀리면 held.
  검수 모델은 Claude Sonnet 5, 짧은 생각(adaptive + effort low) (2026-09-28 결정. Haiku 4.5 는 시험에서 문제 글 2개를
  놓쳤고, 생각을 끈 Sonnet 5 는 운영 중 오판이 잦았다).
  Sonnet 5 는 temperature 를 기본값 외로 보내면 거부하므로 보내지 않는다. Haiku 4.5 로 바꾸면 temperature 0 을 준다 (request_params).
- 서버 측 거부 대체(fallbacks)는 켜지 않는다. 다른 모델이 판정하면 시험지(Step 4-C)를 치르지 않은 모델의 판정이 되기 때문이다.
  거부(refusal)·출력 한도 초과는 형식 오류로 보고 같은 규칙(재요청 1회 → held)을 따른다.

판정 → 처리 (generator.generate_post 가 checks 로 받는다)
  전 항목 pass          → 통과 (approved 후보)
  fail 만 있고 훅에만 문제 → 다른 훅 후보로 교체 후 재검수
  fail (본문 문제)       → 사유를 생성기에 돌려주고 재생성 (최대 2회)
  uncertain 하나라도     → 즉시 held (재생성하지 않음)
  형식 오류 (재요청 후)   → 즉시 held. format_error 는 자동 정지(3회 연속) 판정용으로 기록한다.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from products import Product
from settings import PROJECT_ROOT, Config

log = logging.getLogger("threads_coupas")

PROMPT_PATH = PROJECT_ROOT / "prompts" / "reviewer.md"
APP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "reviewer_app.md"
PLAN_PATH = PROJECT_ROOT / "PLAN.md"
ITEM_CODES = tuple(f"R{i}" for i in range(1, 13))
VERDICTS = ("pass", "fail", "uncertain")
MAX_REQUESTS = 2            # 최초 1회 + 형식 오류 재요청 1회
RECENT_POSTS_LIMIT = 30
MAX_TOKENS = 8000           # 판정 JSON 은 짧지만 생각(thinking) 토큰도 이 한도에 포함된다
# temperature 를 받는 모델에는 PLAN 대로 0 을 준다. Opus 5·Sonnet 5 등 최신 모델은 temperature 를 보내면 400 이다.
TEMPERATURE_ZERO_MODELS = ("claude-haiku-4-5",)
# 짧게 생각하고 판정하는 모델 (adaptive thinking + effort low). 생각을 끈 Sonnet 5 는 적힌 지시를 놓치고
# 없는 문제를 만드는 오판(R1·R6 애매)이 잦아 운영 중 held 가 50% 를 넘었다 (2026-09-28).
LOW_EFFORT_THINKING_MODELS = ("claude-sonnet-5",)
OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def request_params(model: str) -> dict:
    """모델 외의 요청 설정. 합격 기록 해시에 들어가므로 바꾸면 재시험이 필요하다.
    anthropic SDK 1.x 는 temperature 인자를 없앴다. 아직 temperature 를 받는 모델(Haiku 4.5)에는 SDK 문서대로
    extra_body 로 넘긴다 (PLAN Step 4-B: temperature 0).
    OpenAI(임시 검수, 2026-10-02)는 temperature 0 + JSON 스키마 강제(strict)."""
    from settings import provider_of

    if provider_of(model) == "openai":
        params = {"response_format": {"type": "json_schema",
                                      "json_schema": {"name": "review", "strict": True, "schema": OUTPUT_SCHEMA}}}
        if model.startswith(OPENAI_REASONING_PREFIXES):
            # 추론 모델은 temperature 를 받지 않는다. 짧게 생각하고 판정한다 (gpt-4.1 은 시험에서 P10 기간 오류를 놓쳤다).
            params.update(max_completion_tokens=MAX_TOKENS, reasoning_effort="medium")
        else:
            params.update(max_completion_tokens=4000, temperature=0)
        return params
    params = {"max_tokens": MAX_TOKENS,
              "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}}
    if model.startswith(TEMPERATURE_ZERO_MODELS):
        params["extra_body"] = {"temperature": 0}
    if model.startswith(LOW_EFFORT_THINKING_MODELS):
        params["thinking"] = {"type": "adaptive"}
        params["output_config"]["effort"] = "low"
    return params

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": {"type": "string"},
        "hook_only": {"type": "boolean"},
    },
    "required": ["verdict", "reason", "hook_only"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {code: ITEM_SCHEMA for code in ITEM_CODES},
    "required": list(ITEM_CODES),
    "additionalProperties": False,
}

# (system, user) -> (stop_reason, 응답 텍스트)
CallFn = Callable[[str, str], tuple]


# ── 프롬프트 ─────────────────────────────────────────────────────────

def absolute_rules(plan_path=PLAN_PATH) -> str:
    """PLAN.md 1장 '절대 규칙' 전문. PLAN 이 바뀌면 프롬프트 해시도 바뀌어 시험지를 다시 봐야 한다."""
    text = plan_path.read_text(encoding="utf-8")
    m = re.search(r"^## 1\..*?(?=^---\s*$)", text, re.M | re.S)
    if not m:
        raise ValueError("PLAN.md 에서 1장 절대 규칙을 찾지 못했습니다")
    return m.group(0).strip()


# R11 말투 기준. polite 는 예전 reviewer.md 의 R11 문장과 글자 그대로 같아서 기존 계정의 프롬프트 해시(시험지)가 바뀌지 않는다.
TONE_RULES = {
    "polite": '말투가 존댓말(해요체 또는 합니다체)로 일관되고, 어색하거나 스팸 같은 문장이 없음. 반말·자음 표현(ㅋㅋ)·"지금 바로 클릭" 같은 문장은 fail. 해요체의 가벼운 말투, 짧은 줄바꿈, 독자에게 묻는 마지막 질문은 이 계정의 정해진 형식이므로 fail 이 아닙니다. R4 에서 허용한 결론 형식도 광고성 문장으로 보지 않습니다.',
    "casual": ("말투가 한 글 안에서 일관되고(반말·해요체·합니다체 모두 허용), 어색하거나 스팸 같은 문장이 없음. "
               "자음 표현(ㅋㅋ, ㅠㅠ)·\"지금 바로 클릭\" 같은 문장은 fail. "
               "이 계정은 친근한 반말이 정해진 형식이므로 반말, 짧은 줄바꿈, 독자에게 묻는 마지막 질문은 fail 이 아닙니다. "
               "R4 에서 허용한 결론 형식도 광고성 문장으로 보지 않습니다."),
}


def prompt_path_for(cfg: Config):
    """계정 종류에 맞는 검수 프롬프트. 앱 홍보 계정(content: app)은 앱 글 기준(reviewer_app.md, 2026-10-30)."""
    return APP_PROMPT_PATH if cfg.content == "app" else PROMPT_PATH


def system_prompt(cfg: Config, prompt_path=None, plan_path=PLAN_PATH) -> str:
    prompt_path = prompt_path or prompt_path_for(cfg)
    template = prompt_path.read_text(encoding="utf-8")
    # 앱 검수 프롬프트에는 쿠팡 계정의 1장 규칙 전문을 넣지 않는다 (앱 글의 규칙은 프롬프트에 직접 적었다)
    rules = absolute_rules(plan_path) if "{absolute_rules}" in template else ""
    return template.format(absolute_rules=rules, disclosure_text=cfg.disclosure_text, tone_rule=TONE_RULES[cfg.tone])


def prompt_hash(cfg: Config, prompt_path=None, plan_path=PLAN_PATH) -> str:
    """검수 프롬프트(규칙 전문 포함)·출력 스키마·요청 설정의 해시. 시험지 합격 기록과 대조한다."""
    blob = (system_prompt(cfg, prompt_path, plan_path) + json.dumps(OUTPUT_SCHEMA, sort_keys=True)
            + json.dumps(request_params(cfg.reviewer_model), sort_keys=True))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def product_rows(products: Iterable[Product]) -> list[dict]:
    return [{"name": p.name, "category": p.category, "power_w": p.power_w, "note": p.note, "link": p.link}
            for p in products]


def user_message(post: dict, products: Sequence[dict], recent_posts: Sequence[str]) -> str:
    return json.dumps({
        "post": {k: post.get(k) for k in ("type", "text", "reply_text", "variant", "has_link")},
        "calc_payload": post.get("calc_payload"),
        "products": list(products),
        "recent_posts": list(recent_posts)[:RECENT_POSTS_LIMIT],
    }, ensure_ascii=False, indent=1)


# ── 응답 검증 ─────────────────────────────────────────────────────────

def parse_review(stop_reason: str, text: str) -> tuple[dict | None, str | None]:
    """(판정 dict, 오류 설명). 스키마를 강제해도 거부·잘림·SDK 차이에 대비해 코드로 다시 확인한다."""
    if stop_reason != "end_turn":
        return None, f"stop_reason={stop_reason}"
    try:
        data = json.loads(text or "")
    except ValueError:
        return None, "JSON 이 아닙니다"
    if not isinstance(data, dict):
        return None, "최상위가 객체가 아닙니다"
    missing = [c for c in ITEM_CODES if c not in data]
    extra = [k for k in data if k not in ITEM_CODES]
    if missing or extra:
        return None, f"항목 불일치 (없음: {missing}, 모르는 키: {extra})"
    for code in ITEM_CODES:
        item = data[code]
        if (not isinstance(item, dict) or item.get("verdict") not in VERDICTS
                or not isinstance(item.get("reason"), str) or not isinstance(item.get("hook_only"), bool)):
            return None, f"{code} 형식 오류"
    return data, None


# ── 검수 ─────────────────────────────────────────────────────────────

@dataclass
class ReviewResult:
    status: str                     # approved | fail | uncertain | format_error
    items: dict | None
    model: str
    requests: int                   # 이번 검수에서 보낸 API 요청 수 (형식 오류 재요청 포함)
    errors: list = field(default_factory=list)

    def problems(self) -> list[str]:
        if self.status == "format_error":
            return [f"[검수] 응답 형식 오류 {self.requests}회: {'; '.join(self.errors)}"]
        return [f"[{code}] {item['verdict']}: {item['reason']}"
                for code, item in (self.items or {}).items() if item["verdict"] != "pass"]

    @property
    def hook_fixable(self) -> bool:
        """fail 항목이 전부 훅에만 있는 문제면 다른 훅 후보로 바꿔 재검수할 수 있다."""
        bad = [i for i in (self.items or {}).values() if i["verdict"] != "pass"]
        return self.status == "fail" and bool(bad) and all(i["hook_only"] for i in bad)

    def record(self) -> dict:
        return {"status": self.status, "items": self.items, "errors": self.errors}


CODE_CHECKED = {"R1": "G2", "R8": "G6"}     # 문자열이 똑같은지 보는 항목은 코드 가드 결과로 판정한다
REPEAT_SIMILARITY = 0.7     # R9: 최근 글과 글자 유사도가 이 값 이상이면 반복 (2026-10-09 까지 발행 글끼리 최대 0.46)


def _without_disclosure(text: str, cfg: Config) -> str:
    """모든 링크 글에 똑같이 붙는 대가성 문구는 유사도 계산에서 뺀다."""
    return "\n".join(line for line in (text or "").splitlines() if line.strip() != cfg.disclosure_text).strip()


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def repetition_check(post: dict, recent_posts: Sequence[str], cfg: Config) -> dict:
    """R9 를 코드로 판정한다. 전기요금 계산 한 주제만 다루는 계정이라 LLM 은 숫자가 다른 글도 '구조가 비슷해 애매하다'며
    전부 uncertain 으로 막았다 (2026-10-04 자동 정지, 10-09 시험 생성 4/4 보류). 반복은 다음 둘 중 하나다:
      - 훅(링크 글 둘째 줄, tip 첫 줄)이 최근 글의 훅과 같음 → 훅만 바꾸면 풀린다 (hook_only)
      - 대가성 문구를 뺀 본문이 최근 글과 REPEAT_SIMILARITY 이상 비슷함
    같은 숫자의 글이 되풀이되지 않게 하는 일은 pipeline 의 주제 돌려 쓰기(generator.pick_subject)가 맡는다."""
    import guard

    text = _without_disclosure(post.get("text"), cfg)
    hook = _squash(guard.hook_line(post))
    worst, same_hook = 0.0, False
    for recent in recent_posts:
        other = _without_disclosure(recent, cfg)
        if not other:
            continue
        worst = max(worst, difflib.SequenceMatcher(None, text, other).ratio())
        # 최근 글의 훅은 대가성 문구를 뺀 첫 줄이다
        same_hook = same_hook or (bool(hook) and hook == _squash(other.splitlines()[0]))
    if worst >= REPEAT_SIMILARITY:
        return {"verdict": "fail", "hook_only": False,
                "reason": f"(코드 확인) 최근 글과 {worst:.0%} 비슷합니다. 첫 문장·문단 순서·결론을 다르게 씁니다."}
    if same_hook:
        return {"verdict": "fail", "hook_only": True, "reason": "(코드 확인) 훅이 최근 글의 훅과 같습니다."}
    return {"verdict": "pass", "hook_only": False, "reason": f"(코드 확인) 최근 글과 최대 {worst:.0%} 비슷함"}


def apply_code_checks(post: dict, data: dict, cfg: Config, recent_posts: Sequence[str] = ()) -> dict:
    """R1(대가성 문구 위치)·R8(가정 문구 그대로 포함)은 기계적 문자열 검사라 LLM 이 자주 틀린다
    (2026-10-02 시험: 그대로 있는 문구를 '다르다'고 막고, 빠진 문구를 통과시킴). 코드 가드(G2·G6) 결과로 덮어쓴다."""
    import guard

    problems = guard.check_post(post, cfg)
    data = {k: dict(v) for k, v in data.items()}
    for item, code in CODE_CHECKED.items():
        hits = [p for p in problems if p.startswith(f"[{code}]")]
        if hits:
            data[item] = {"verdict": "fail", "reason": f"(코드 확인) {hits[0]}", "hook_only": False}
        else:
            not_applicable = item == "R8" and post.get("type") not in ("calc", "compare")
            data[item] = {"verdict": "pass", "hook_only": False,
                          "reason": "(코드 확인) 해당 없음" if not_applicable else "(코드 확인) 문구가 정확히 있음"}
    data["R9"] = repetition_check(post, recent_posts, cfg)
    return data


class Reviewer:
    def __init__(self, cfg: Config, call: CallFn):
        self.cfg = cfg
        self.call = call
        self.system = system_prompt(cfg)

    def review(self, post: dict, products: Sequence[dict] = (), recent_posts: Sequence[str] = ()) -> ReviewResult:
        user = user_message(post, products, recent_posts)
        errors = []
        for n in range(1, MAX_REQUESTS + 1):
            try:
                stop_reason, text = self.call(self.system, user)
                data, error = parse_review(stop_reason, text)
            except Exception as e:      # 네트워크 오류도 형식 오류와 같이 재요청 1회 후 held
                data, error = None, f"호출 실패: {e}"
            if data is not None:
                data = apply_code_checks(post, data, self.cfg, recent_posts)
                verdicts = {item["verdict"] for item in data.values()}
                status = "uncertain" if "uncertain" in verdicts else "fail" if "fail" in verdicts else "approved"
                return ReviewResult(status, data, self.cfg.reviewer_model, n, errors)
            errors.append(error)
            log.warning(f"[검수] 응답 형식 오류 ({n}/{MAX_REQUESTS}): {error}")
        return ReviewResult("format_error", None, self.cfg.reviewer_model, MAX_REQUESTS, errors)


class CheckProblems(list):
    """generator.generate_post 의 checks 가 돌려주는 문제 목록 + 처리 신호.
    hook_fixable: 다른 훅 후보로 바꿔 볼 가치가 있음 / final: 재생성하지 않고 바로 held / record: posts 행에 남길 검수 기록."""

    def __init__(self, problems=(), *, hook_fixable=True, final=False, record=None):
        super().__init__(problems)
        self.hook_fixable = hook_fixable
        self.final = final
        self.record = record


def make_check(reviewer: Reviewer, products: Sequence[dict] = (), recent_posts: Sequence[str] = ()):
    """생성기 checks 에 넣을 함수. 코드 가드를 통과한 게시물만 이 함수까지 온다."""
    def check(post: dict) -> CheckProblems:
        result = reviewer.review(post, products, recent_posts)
        return CheckProblems(
            result.problems(),
            hook_fixable=result.hook_fixable,
            final=result.status in ("uncertain", "format_error"),
            record={"review_result": result.record(), "review_model": result.model, "requests": result.requests},
        )
    return check


# ── Claude 호출 ──────────────────────────────────────────────────────

def make_anthropic_call(api_key: str, model: str) -> CallFn:
    """실제 Claude API 호출. 구조화 출력(output_config.format)으로 OUTPUT_SCHEMA 를 강제한다."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    def call(system: str, user: str) -> tuple:
        resp = client.messages.create(
            model=model,
            **request_params(model),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return resp.stop_reason, text

    return call


# ── OpenAI 호출 (임시 검수, 2026-10-02: Anthropic 크레딧이 API 에 반영되지 않는 동안) ──────────

def make_openai_call(api_key: str, model: str) -> CallFn:
    """OpenAI 검수 호출. stop_reason 은 Claude 와 같은 이름으로 맞춘다 (stop → end_turn, length → max_tokens)."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, timeout=120)
    reasons = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}

    def call(system: str, user: str) -> tuple:
        resp = client.chat.completions.create(
            model=model, **request_params(model),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            return "refusal", ""
        return reasons.get(choice.finish_reason, choice.finish_reason), choice.message.content or ""

    return call


def make_call(model: str, secrets) -> CallFn:
    """config.reviewer_model 의 제공사에 맞는 검수 호출 함수."""
    from settings import ConfigError, provider_of

    provider = provider_of(model)
    key = secrets.api_key_for(provider)
    if not key:
        raise ConfigError(f"검수 모델 {model} 의 API 키가 없습니다 ({provider})")
    if provider == "anthropic":
        return make_anthropic_call(key, model)
    if provider == "openai":
        return make_openai_call(key, model)
    raise ConfigError(f"검수 모델로 지원하지 않는 제공사입니다: {model}")
