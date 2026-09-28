"""
게시물 생성기 (PLAN v2 Step 3).

흐름: 상품 선택 → 계산기(calc_payload) → LLM 은 그 숫자로 훅 후보 3개와 본문만 작성
      → 훅 하나를 골라 조립 → 검사(코드 가드는 항상, Step 4-B 부터 검수자도)
      → 걸리면 남은 훅 후보로 먼저 교체 → 그래도 안 되면 사유를 돌려주고 재생성 (최초 1회 + 재생성 2회)
      → 끝내 실패하면 status=held, 시도 이력은 guard_log 에 남긴다.
      재생성 루프는 로옥션 column_guard.py 의 generate_with_guard 방식이다.

훅 선택 (사람 선택 없음): 최근 7일간 본문에 가장 적게 쓰인 hook_style 을 먼저 쓴다.
  동률이면 question → result → compare 순서. 유형별 데이터를 고르게 쌓아 주간 리포트에서 비교하기 위해서다.

조립 (대가성 문구·링크·훅 위치는 LLM 에 맡기지 않는다, 규칙 3)
  - 링크 글: 1줄 disclosure_text / 2줄 훅 / 빈 줄 / 본문
      link_in_body : 링크는 link_url 에 두고 발행 때 link_attachment 로 붙인다 (본문 텍스트에 URL 없음)
      link_in_reply: 답글 = disclosure_text + 빈 줄 + LLM 이 쓴 안내 한 문장, 링크는 답글에 붙인다
  - tip: 1줄 훅 / 빈 줄 / 본문 (링크·문구 없음)

실행 (LLM 비용 발생, DB 저장 없이 초안 JSON 만 출력):
  python generator.py --type calc [--variant link_in_reply] [--count 2]
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

import calculator
import guard
from calculator import Tariff
from products import Product
from settings import PROJECT_ROOT, Config

log = logging.getLogger("threads_coupas")

PROMPTS_DIR = PROJECT_ROOT / "prompts"
POST_TYPES = ("calc", "compare", "tip")
VARIANTS = ("link_in_body", "link_in_reply")
HOOK_STYLES = ("question", "result", "compare")     # 동률일 때 이 순서
HOOK_WINDOW_DAYS = 7
USED_STATUSES = ("approved", "published")           # 본문에 실제로 쓰인 훅으로 세는 상태
MAX_ATTEMPTS = 3            # 최초 1회 + 재생성 2회 (PLAN Step 4)
TOPIC_TAG_MAX = 50

TIP_TOPICS = (
    "누진 구간 때문에 겨울 전기요금이 갑자기 오르는 이유",
    "소비전력(W)으로 한 달 사용량(kWh)을 가늠하는 법",
    "난방가전을 들이기 전에 지난달 고지서 사용량부터 확인하기",
    "같은 난방가전도 사용시간을 줄이면 요금이 어떻게 달라지는지",
    "전기요·온수매트의 온도 단계와 사용시간 관리",
)

CompleteFn = Callable[[str, str, float], str]
CheckFn = Callable[[dict], list]     # 조립된 게시물 → 문제 목록 (빈 목록 = 통과)


# ── 프롬프트 ─────────────────────────────────────────────────────────

def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def system_prompt(cfg: Config, post_type: str, variant: str | None) -> str:
    reply_spec = (
        '"링크 답글에 넣을 안내 한 문장 (예: 소비전력 표기는 상품 페이지에서 확인할 수 있습니다.)"'
        if variant == "link_in_reply" else "null"
    )
    number_rule = (
        "calc·compare 훅에는 계산 데이터의 숫자를 1개 이상 넣습니다 (월 kWh, 추정 요금, 두 제품의 요금 차이 등)."
        if post_type != "tip" else "tip 훅에는 숫자가 없어도 됩니다. 숫자를 쓰면 계산 데이터의 값만 씁니다."
    )
    return _read_prompt("threads_base.md").format(
        banned_words=", ".join(cfg.banned_words),
        banned_hook_phrases=", ".join(cfg.banned_hook_phrases),
        max_body_chars=cfg.limits.max_body_chars,
        hook_number_rule=number_rule,
        reply_line_spec=reply_spec,
    )


def user_message(post_type: str, payload: dict, feedback: list | None = None, previous: dict | None = None) -> str:
    msg = _read_prompt(f"tpl_{post_type}.md") + "\n\n계산 데이터(JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    if feedback:
        msg += "\n\n이전 초안의 문제점:\n" + "\n".join(f"- {p}" for p in feedback)
        if previous:
            msg += "\n\n이전 초안:\n" + json.dumps(previous, ensure_ascii=False, indent=1)
        msg += (
            "\n\n위 문제점을 모두 고쳐 훅 후보 3개와 본문을 새로 작성하세요. 이전 초안을 되풀이하지 마세요. "
            "지적된 숫자·표현은 빼거나 계산 데이터의 값으로 바꿉니다. 반드시 JSON 으로만 답합니다."
        )
    return msg


# ── 상품·주제·훅 선택 ────────────────────────────────────────────────

def _rotate(items: list, day: date, offset: int = 0):
    return items[(day.toordinal() + offset) % len(items)]


def pick_products(products: Sequence[Product], post_type: str, day: date,
                  recent_ids: Sequence[str] = (), offset: int = 0) -> list[Product]:
    """최근에 쓴 상품을 빼고 날짜 기준으로 돌아가며 고른다 (티스토리 climateSeeds.ts selectSeedsForRun 방식).
    compare 는 같은 카테고리의 두 상품을 고르고, 소비전력이 큰 쪽을 a 로 둔다."""
    recent = set(recent_ids)
    ordered = sorted(products, key=lambda p: p.product_id)
    if post_type == "compare":
        pairs = [
            tuple(sorted(pair, key=lambda p: (-p.power_w, p.product_id)))
            for pair in itertools.combinations(ordered, 2)
            if pair[0].category == pair[1].category and pair[0].power_w != pair[1].power_w
        ]
        if not pairs:
            raise ValueError("compare 에 쓸 같은 카테고리·다른 소비전력의 상품 2개가 없습니다.")
        fresh = [p for p in pairs if not {p[0].product_id, p[1].product_id} <= recent]
        return list(_rotate(fresh or pairs, day, offset))
    if not ordered:
        raise ValueError("상품이 없습니다.")
    fresh = [p for p in ordered if p.product_id not in recent]
    return [_rotate(fresh or ordered, day, offset)]


def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        dt = datetime.fromisoformat(value)
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hook_style_order(history: Iterable[dict], now: datetime) -> list[str]:
    """최근 7일간 본문에 쓰인(approved·published) hook_style 사용 횟수가 적은 순서.
    history 는 posts 행(dict: hook_style, status, created_at). 동률이면 HOOK_STYLES 순서."""
    since = now - timedelta(days=HOOK_WINDOW_DAYS)
    counts = {s: 0 for s in HOOK_STYLES}
    for post in history:
        created = _as_utc(post.get("created_at"))
        if (post.get("status") in USED_STATUSES and post.get("hook_style") in counts
                and created is not None and since <= created <= now):
            counts[post["hook_style"]] += 1
    return sorted(HOOK_STYLES, key=lambda s: (counts[s], HOOK_STYLES.index(s)))


def build_payload(post_type: str, picked: Sequence[Product], tariff: Tariff, tip_topic: str | None = None) -> dict:
    """LLM·가드·검수자가 함께 보는 calc_payload. 게시물의 모든 숫자는 여기에 있어야 한다."""
    if post_type == "calc":
        p = picked[0]
        return {"product": {"name": p.name, "note": p.note}, **calculator.monthly_cost(p.power_w, p.default_hours, tariff)}
    if post_type == "compare":
        a, b = picked
        # 같은 조건 비교: 두 상품 모두 a 의 기본 사용시간을 쓴다.
        result = calculator.compare(
            {"name": a.name, "power_w": a.power_w, "hours_per_day": a.default_hours},
            {"name": b.name, "power_w": b.power_w, "hours_per_day": a.default_hours},
            tariff,
        )
        result["a"]["note"], result["b"]["note"] = a.note, b.note
        return result
    if post_type == "tip":
        # tip 은 숫자 가벼운 신뢰 쌓기 글이다 (2026-09-28). 요금 추정치(원)와 "누진 N단계 가정"은 넣지 않는다:
        # 넣으면 생성기가 요금을 섞어 쓰고 검수자가 가정 문구 누락(R8)·구간 모순(R6)으로 막아 held 가 쌓였다.
        payload = {
            "tip_topic": tip_topic or TIP_TOPICS[0],
            "tariff": {
                "tariff_effective_date": tariff.effective_date.isoformat(),
                "season": tariff.season,
                "tiers": [
                    {"tier": i, "upto_kwh": calculator.plain(t.upto_kwh) if t.upto_kwh is not None else None,
                     "rate_per_kwh": calculator.plain(t.rate_per_kwh)}
                    for i, t in enumerate(tariff.tiers, start=1)
                ],
            },
        }
        if picked:
            p = picked[0]
            usage = calculator.monthly_cost(p.power_w, p.default_hours, tariff)
            # 사용량 예시만 남긴다 (소비전력·시간·일수·kWh). 요금·가정 구간은 빼 둔다.
            payload["example"] = {"category": p.category,
                                  **{k: usage[k] for k in ("power_w", "hours_per_day", "days_per_month", "monthly_kwh")}}
        return payload
    raise ValueError(f"알 수 없는 유형: {post_type}")


# ── LLM 응답 → 게시물 ────────────────────────────────────────────────

def parse_draft(content: str) -> dict:
    """로옥션 _parse_json_object 와 같은 방식: 코드펜스를 벗기고, 실패하면 가장 바깥 {...} 를 찾는다."""
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content)
    try:
        data = json.loads(content)
    except ValueError:
        m = re.search(r"\{.*\}", content, re.S)
        try:
            data = json.loads(m.group(0)) if m else {}
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _clean(text) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\\n", "\n")
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def _topic_tag(value) -> str | None:
    tag = str(value or "").strip().lstrip("#")
    if not tag or tag.lower() == "null" or len(tag) > TOPIC_TAG_MAX or "." in tag or "&" in tag or "\n" in tag:
        return None     # 선택 항목이라 잘못된 태그는 버린다 (재생성 사유로 삼지 않음)
    return tag


def hook_candidates(draft: dict) -> list[dict]:
    """LLM 응답의 훅 후보를 [{text, style}] 로 정리한다. 스타일별 첫 번째 것만, HOOK_STYLES 순서로."""
    found = {}
    for item in draft.get("hook_candidates") or []:
        if not isinstance(item, dict):
            continue
        style = str(item.get("style") or "").strip().lower()
        text = " ".join(_clean(item.get("text")).split())      # 훅은 한 줄
        if style in HOOK_STYLES and text and style not in found:
            found[style] = {"text": text, "style": style}
    return [found[s] for s in HOOK_STYLES if s in found]


def draft_problems(draft: dict, variant: str | None) -> list[str]:
    problems = []
    styles = {h["style"] for h in hook_candidates(draft)}
    missing = [s for s in HOOK_STYLES if s not in styles]
    if missing:
        problems.append(f"훅 후보가 부족합니다. question·result·compare 를 1개씩 써야 합니다 (없음: {', '.join(missing)})")
    if not _clean(draft.get("body")):
        problems.append('응답에 "body" 가 없거나 비어 있습니다')
    if variant == "link_in_reply" and not _clean(draft.get("reply_line")):
        problems.append('link_in_reply 인데 "reply_line" 이 비어 있습니다')
    return problems


def assemble(post_type: str, variant: str | None, draft: dict, hook: dict, cfg: Config, link: str | None) -> dict:
    """LLM 초안 + 고른 훅에 대가성 문구·링크를 코드로 붙여 posts 행의 본문 필드를 만든다."""
    body = _clean(draft.get("body"))
    fields = {"text": f"{hook['text']}\n\n{body}", "reply_text": None, "has_link": False, "link_url": None,
              "variant": None, "hook_style": hook["style"], "topic_tag": _topic_tag(draft.get("topic_tag"))}
    if post_type == "tip":
        return fields
    fields.update(text=f"{cfg.disclosure_text}\n{hook['text']}\n\n{body}", has_link=True, link_url=link, variant=variant)
    if variant == "link_in_reply":
        fields["reply_text"] = f"{cfg.disclosure_text}\n\n{_clean(draft.get('reply_line'))}"
    return fields


def _run_checks(post: dict, checks: Sequence[CheckFn]) -> tuple[list, list[dict]]:
    """(첫 번째로 실패한 검사의 결과, 검사들이 남긴 기록). 결과 객체의 hook_fixable·final 신호를 그대로 넘긴다
    (reviewer.CheckProblems). 가드처럼 평범한 list 를 돌려주는 검사는 hook_fixable=True, final=False 로 본다."""
    records = []
    for check in checks:
        result = check(post)
        if getattr(result, "record", None):
            records.append(result.record)
        if result:
            return result, records
    return [], records


# ── 생성 + 훅 교체 + 재생성 루프 ──────────────────────────────────────

def generate_post(post_type: str, picked: Sequence[Product], tariff: Tariff, cfg: Config, complete: CompleteFn, *,
                  variant: str = "link_in_body", tip_topic: str | None = None,
                  checks: Sequence[CheckFn] = (), history: Iterable[dict] = (),
                  max_attempts: int = MAX_ATTEMPTS, now: datetime | None = None) -> dict:
    """posts 행 하나(dict)를 돌려준다. status 는 approved(가드+검수 통과), draft(가드만 통과, 검수 없음) 또는 held.
    코드 가드(guard.check_post)는 항상 먼저 실행된다 (빼거나 끌 수 없음).
    checks: 가드 다음에 실행할 검사 함수들 (Step 4-B 검수자). history: 훅 선택용 최근 posts 행."""
    checks = [guard.make_checker(cfg), *checks]
    if post_type not in POST_TYPES:
        raise ValueError(f"알 수 없는 유형: {post_type}")
    if post_type != "tip" and variant not in VARIANTS:
        raise ValueError(f"알 수 없는 variant: {variant}")
    now = now or datetime.now(timezone.utc)
    variant = None if post_type == "tip" else variant
    link = None if post_type == "tip" else picked[0].link
    style_order = hook_style_order(history, now)

    payload = build_payload(post_type, picked, tariff, tip_topic)
    system = system_prompt(cfg, post_type, variant)
    attempts, feedback, previous = [], None, None
    fields, candidates, passed, final = None, [], False, False
    review = {"review_result": None, "review_model": None, "review_attempts": 0}

    for attempt in range(1, max_attempts + 1):
        # 재시도는 같은 글을 되풀이하지 않도록 온도를 올린다 (로옥션 방식)
        temperature = 0.3 if attempt == 1 else 0.7
        try:
            draft = parse_draft(complete(system, user_message(post_type, payload, feedback, previous), temperature))
            problems = draft_problems(draft, variant)
        except Exception as e:      # 네트워크·API 오류도 이력에 남기고 다음 시도로 넘어간다
            draft, problems = {}, [f"LLM 호출 실패: {e}"]
        if problems:
            attempts.append({"attempt": attempt, "hook_style": None, "problems": problems})
        else:
            candidates = hook_candidates(draft)
            by_style = {h["style"]: h for h in candidates}
            # 최근 7일 최소 사용 유형부터, 걸리면 남은 후보로 교체
            all_problems = []
            for style in style_order:
                fields = assemble(post_type, variant, draft, by_style[style], cfg, link)
                result, records = _run_checks(dict(fields, type=post_type, calc_payload=payload), checks)
                for rec in records:
                    review.update(review_result=rec["review_result"], review_model=rec["review_model"])
                    review["review_attempts"] += rec["requests"]
                attempts.append({"attempt": attempt, "hook_style": style, "problems": list(result)})
                if not result:
                    passed = True
                    break
                all_problems += [p for p in result if p not in all_problems]
                if getattr(result, "final", False):         # uncertain·검수 형식 오류 → 재생성 없이 held
                    final = True
                    break
                if not getattr(result, "hook_fixable", True):   # 본문 문제는 훅을 바꿔도 안 풀린다 → 재생성
                    break
            problems = all_problems
        log.info(f"[생성 {post_type}] 시도 {attempt}/{max_attempts}: " + ("통과" if passed else f"문제 {len(problems)}건"))
        if passed or final:
            break
        if draft:
            feedback, previous = problems, draft
        else:
            feedback = problems

    if fields is None:
        fields = {"text": "", "reply_text": None, "has_link": post_type != "tip", "link_url": link,
                  "variant": variant, "hook_style": None, "topic_tag": None}
    reviewed_ok = passed and (review["review_result"] or {}).get("status") == "approved"
    return {
        "id": str(uuid.uuid4()),
        "type": post_type,
        "product_ids": [p.product_id for p in picked] if post_type != "tip" else [],
        **fields,
        "hook_candidates": candidates,
        "calc_payload": payload,
        # 규칙 4: approved 는 코드 가드와 AI 검수자를 모두 통과한 글에만. 검수 없이 가드만 통과하면 draft 로 둔다.
        "status": ("approved" if reviewed_ok else "draft") if passed else "held",
        "guard_log": {"attempts": attempts},
        **review,
        "scheduled_at": None,
        "published_at": None,
        "threads_media_id": None,
        "threads_reply_id": None,
        "created_at": now.isoformat(),
    }


def main() -> int:
    from calculator import load_tariff
    from llm_client import LLMError, make_complete
    from products import load_products
    from settings import ConfigError, load_config, load_secrets, provider_of, today_kst

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description="초안 생성 (DB 저장 없이 JSON 출력)")
    ap.add_argument("--type", choices=POST_TYPES, required=True)
    ap.add_argument("--variant", choices=VARIANTS, default="link_in_body")
    ap.add_argument("--count", type=int, default=1)
    args = ap.parse_args()
    try:
        cfg, tariff, products = load_config(), load_tariff(), load_products()
        secrets = load_secrets()
        fb = cfg.generator_fallback_model
        complete = make_complete(
            cfg.generator_model, secrets.api_key_for(provider_of(cfg.generator_model)),
            fb, secrets.api_key_for(provider_of(fb)) if fb else None,
        )
    except (ConfigError, LLMError, ValueError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    day, drafts = today_kst(), []
    for i in range(args.count):
        # tip 은 예시 계산에 쓸 상품 1개를 고른다 (링크는 붙이지 않음)
        picked = pick_products(products, "calc" if args.type == "tip" else args.type, day, offset=i)
        topic = TIP_TOPICS[(day.toordinal() + i) % len(TIP_TOPICS)] if args.type == "tip" else None
        drafts.append(generate_post(args.type, picked, tariff, cfg, complete, variant=args.variant, tip_topic=topic))
    print(json.dumps(drafts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
