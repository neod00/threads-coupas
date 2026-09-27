"""
생성 파이프라인 (PLAN v2 Step 7 generate.yml). 매일 20:00 KST 에 다음 날 분량을 만든다.

  다음 날 빈 슬롯 수만큼 → 유형 배분(링크 글은 하루 링크 한도까지, 나머지는 tip) → 상품·훅 선택
  → 생성 → 코드 가드 → AI 검수자 → approved 면 다음 날 빈 슬롯 배정 / 아니면 held → 저장
  → 자동 정지 판정: 검수자 형식 오류 3회 연속, 하루 생성분 held 50% 초과 (규칙 10)

킬 스위치·자동 정지 상태면 생성하지 않는다 (쌓이는 초안과 LLM 비용을 막기 위해).

실행:
  python pipeline.py generate              # 생성해서 DB 에 저장 (LLM 비용 발생)
  python pipeline.py generate --dry-run    # 생성·검증만 하고 저장하지 않음 (LLM 비용은 발생)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import autostop
import generator
import guard
import reviewer as reviewer_mod
import rules
from settings import KST, Config

log = logging.getLogger("threads_coupas")

RECENT_PRODUCT_DAYS = 3         # 최근 며칠 동안 쓴 상품은 피한다
HOOK_HISTORY_DAYS = 7


def plan_day(posts, cfg: Config, day, has_compare_pairs: bool) -> list[str]:
    """그날 새로 만들 글 유형 목록. 빈 슬롯 수만큼, 링크 글은 링크 한도까지만."""
    occupied = [(t, link) for t, link in rules.occupied(posts) if rules.kst_date(t) == day]
    free = max(0, min(cfg.max_posts_per_day, len(cfg.slots_kst)) - len(occupied))
    link_quota = max(0, rules.max_link_posts_per_day(cfg) - sum(link for _, link in occupied))
    n_links = min(free, link_quota)
    link_types = ["calc", "compare"] if has_compare_pairs else ["calc"]
    # 날짜마다 calc/compare 시작 순서를 바꿔 두 유형이 고르게 쌓이게 한다
    start = day.toordinal() % len(link_types)
    links = [link_types[(start + i) % len(link_types)] for i in range(n_links)]
    return links + ["tip"] * (free - n_links)


def _has_compare_pairs(products) -> bool:
    try:
        generator.pick_products(products, "compare", datetime.now(timezone.utc).date())
        return True
    except ValueError:
        return False


def run_generate(store, cfg: Config, tariff, products, *, complete, review_call, notifier, now: datetime,
                 env=None, variant: str = "link_in_body", save: bool = True) -> dict:
    from settings import auto_publish_disabled

    if auto_publish_disabled(env):
        log.info("AUTO_PUBLISH_DISABLED 가 켜져 있어 생성하지 않습니다.")
        return {"action": "kill_switch", "posts": []}
    if autostop.is_stopped(store):
        log.warning("자동 정지 상태라 생성하지 않습니다.")
        return {"action": "auto_stopped", "posts": []}

    tomorrow = rules.kst_date(now) + timedelta(days=1)
    day_start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=KST)
    occupying = store.list_posts(rules.OCCUPYING)
    types = plan_day(occupying, cfg, tomorrow, _has_compare_pairs(products))
    if not types:
        return {"action": "no_free_slot", "posts": []}

    week_ago = (now - timedelta(days=HOOK_HISTORY_DAYS)).astimezone(timezone.utc).isoformat()
    history = store.list_posts(since=week_ago)
    recent_cut = (now - timedelta(days=RECENT_PRODUCT_DAYS)).astimezone(timezone.utc).isoformat()
    recent_ids = {pid for p in history if p["created_at"] >= recent_cut for pid in p.get("product_ids") or []}
    recent_texts = [p["text"] for p in reversed(store.list_posts(["published"]))][:reviewer_mod.RECENT_POSTS_LIMIT]
    rev = reviewer_mod.Reviewer(cfg, review_call)

    made = []
    for i, post_type in enumerate(types):
        pick_type = "calc" if post_type == "tip" else post_type
        picked = generator.pick_products(products, pick_type, tomorrow, recent_ids=sorted(recent_ids), offset=i)
        recent_ids.update(p.product_id for p in picked)
        topic = generator.TIP_TOPICS[(tomorrow.toordinal() + i) % len(generator.TIP_TOPICS)] if post_type == "tip" else None
        rows = reviewer_mod.product_rows(picked) if post_type != "tip" else []
        check = reviewer_mod.make_check(rev, rows, recent_texts)
        post = generator.generate_post(post_type, picked, tariff, cfg, complete, variant=variant, tip_topic=topic,
                                       checks=[check], history=history + made, now=now)

        if post["status"] == "approved":
            # 다음 날 슬롯만 본다 (20:00 에 돌아도 오늘 21:30 슬롯에 넣지 않는다)
            slot = rules.next_slot(occupying + made, post["has_link"], cfg, after=day_start, horizon_days=0)
            if slot is None:
                post["status"] = "held"
                post["guard_log"]["attempts"].append({"attempt": "schedule", "hook_style": post["hook_style"],
                                                      "problems": [f"{tomorrow} 에 규칙에 맞는 빈 슬롯이 없습니다"]})
            else:
                post["scheduled_at"] = slot.astimezone(timezone.utc).isoformat()
        made.append(post)
        if save:
            store.insert_post(post)
            autostop.record_review_status(store, notifier, (post.get("review_result") or {}).get("status"))
        log.info(f"[생성] {post_type} → {post['status']} {post.get('scheduled_at') or ''}")

    if save:
        autostop.check_held_ratio(store, notifier, made)
    return {"action": "generated", "posts": made}


def main(argv=None) -> int:
    from calculator import load_tariff
    from llm_client import LLMError, make_complete
    from notify import make_notifier
    from products import load_products
    from settings import ConfigError, auto_publish_disabled, load_config, load_secrets, provider_of
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="다음 날 분량 생성 → 가드 → 검수 → 슬롯 배정")
    ap.add_argument("command", choices=["generate"])
    ap.add_argument("--dry-run", action="store_true", help="생성·검증만 하고 저장하지 않음")
    ap.add_argument("--variant", choices=generator.VARIANTS, default="link_in_body")
    args = ap.parse_args(argv)

    if auto_publish_disabled():
        log.info("AUTO_PUBLISH_DISABLED 가 켜져 있어 생성하지 않습니다.")
        return 0
    try:
        cfg, secrets = load_config(), load_secrets()
        tariff, products = load_tariff(), load_products()
        secrets.require("anthropic_api_key")
        store = open_store(secrets)
        fb = cfg.generator_fallback_model
        complete = make_complete(cfg.generator_model, secrets.api_key_for(provider_of(cfg.generator_model)),
                                 fb, secrets.api_key_for(provider_of(fb)) if fb else None)
    except (ConfigError, LLMError, ValueError) as e:
        log.error(f"설정 오류: {e}")
        return 1
    result = run_generate(store, cfg, tariff, products, complete=complete,
                          review_call=reviewer_mod.make_anthropic_call(secrets.anthropic_api_key, cfg.reviewer_model),
                          notifier=make_notifier(secrets), now=datetime.now(timezone.utc),
                          variant=args.variant, save=not args.dry_run)
    summary = [{"type": p["type"], "status": p["status"], "scheduled_at": p["scheduled_at"],
                "hook": guard.hook_line(p)} for p in result["posts"]]
    print(json.dumps({"action": result["action"], "posts": summary}, ensure_ascii=False, indent=2))
    if args.dry_run:
        print(json.dumps(result["posts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
