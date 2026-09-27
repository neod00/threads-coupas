"""
AI 검수자 (PLAN v2 Step 4-B). 로옥션 column_guard.py 의 llm_factcheck(문장별 JSON 판정)를 확장했다.

- 생성 모델과 다른 회사의 모델(config.reviewer_model, Claude)을 쓴다. settings 가 제공사 분리를 이미 확인했다.
- JSON 스키마(output_config.format)로 R1~R12 판정을 받는다. 형식이 틀리면 1회 재요청, 그래도 틀리면 held.
  Claude Opus 5 는 temperature 를 받지 않으므로(보내면 400) PLAN 의 "temperature 0" 대신 스키마와 고정 프롬프트로 판정을 고정한다.
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
PLAN_PATH = PROJECT_ROOT / "PLAN.md"
ITEM_CODES = tuple(f"R{i}" for i in range(1, 13))
VERDICTS = ("pass", "fail", "uncertain")
MAX_REQUESTS = 2            # 최초 1회 + 형식 오류 재요청 1회
RECENT_POSTS_LIMIT = 30
MAX_TOKENS = 16000

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


def system_prompt(cfg: Config, prompt_path=PROMPT_PATH, plan_path=PLAN_PATH) -> str:
    return prompt_path.read_text(encoding="utf-8").format(
        absolute_rules=absolute_rules(plan_path), disclosure_text=cfg.disclosure_text,
    )


def prompt_hash(cfg: Config, prompt_path=PROMPT_PATH, plan_path=PLAN_PATH) -> str:
    """검수 프롬프트(규칙 전문 포함)와 출력 스키마의 해시. 시험지 합격 기록과 대조한다."""
    blob = system_prompt(cfg, prompt_path, plan_path) + json.dumps(OUTPUT_SCHEMA, sort_keys=True)
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
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return resp.stop_reason, text

    return call
