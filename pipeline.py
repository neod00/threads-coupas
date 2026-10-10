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

USED_STATUSES = ("approved", "published")    # 주제 돌려 쓰기·반복 판정에 넣는 글
HOOK_HISTORY_DAYS = 7


def plan_day(posts, cfg: Config, day, has_compare_pairs: bool, has_picks: bool = False, has_calc: bool = True) -> list[str]:
    """그날 새로 만들 글 유형 목록. 빈 슬롯 수만큼, 링크 글은 링크 한도까지만."""
    occupied = [(t, link) for t, link in rules.occupied(posts) if rules.kst_date(t) == day]
    free = max(0, min(cfg.max_posts_per_day, len(cfg.slots_kst)) - len(occupied))
    link_quota = max(0, rules.max_link_posts_per_day(cfg) - sum(link for _, link in occupied))
    n_links = min(free, link_quota)
    calc_types = ["calc", "compare"] if has_compare_pairs else ["calc"]
    if not has_picks:
        # 날짜마다 calc/compare 시작 순서를 바꿔 두 유형이 고르게 쌓이게 한다
        start = day.toordinal() % len(calc_types)
        links = [calc_types[(start + i) % len(calc_types)] for i in range(n_links)]
    else:
        # 2026-10-10: 링크 글의 첫 자리는 항상 소개(pick), 둘째 자리는 날짜마다 pick → calc → compare 로 돈다
        second = (["pick"] + calc_types) if has_calc else ["pick"]
        cycle = ["pick", second[day.toordinal() % len(second)]]
        links = [cycle[i % len(cycle)] for i in range(n_links)]
    return links + ["tip"] * (free - n_links)


def _has_compare_pairs(products) -> bool:
    try:
        generator.pick_products(products, "compare", datetime.now(timezone.utc).date())
        return True
    except ValueError:
        return False


def run_generate(store, cfg: Config, tariff, products, *, complete, review_call, notifier, now: datetime,
                 env=None, variant: str = "link_in_reply", save: bool = True) -> dict:
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
    types = plan_day(occupying, cfg, tomorrow, _has_compare_pairs(products),
                     has_picks=any(not p.has_power for p in products), has_calc=any(p.has_power for p in products))
    if not types:
        return {"action": "no_free_slot", "posts": []}

    week_ago = (now - timedelta(days=HOOK_HISTORY_DAYS)).astimezone(timezone.utc).isoformat()
    history = store.list_posts(since=week_ago)
    # 실제로 쓰인 글(발행됨 + 발행 대기). 주제 돌려 쓰기와 반복 판정에 쓴다. 오래된 것 → 최신 순.
    used = store.list_posts(USED_STATUSES)
    rev = reviewer_mod.Reviewer(cfg, review_call)

    made = []
    for i, post_type in enumerate(types):
        # 가장 오래 안 쓴 (상품 × 사용시간) 조합·팁 주제를 고른다 (같은 숫자의 글이 되풀이되면 검수 R9 에 막힌다)
        subject = generator.pick_subject(products, post_type, tomorrow, used, offset=i)
        picked, topic = subject["picked"], subject["tip_topic"]
        if post_type == "tip":
            # tip 은 예시 계산에 쓸 상품 1개를 고른다 (링크는 붙이지 않음)
            picked = generator.pick_subject(products, "calc", tomorrow, used, offset=i)["picked"]
        rows = reviewer_mod.product_rows(picked) if post_type != "tip" else []
        newest_first = [p["text"] for p in reversed(used)]
        check = reviewer_mod.make_check(rev, rows, newest_first[:reviewer_mod.RECENT_POSTS_LIMIT])
        same_type = [p["text"] for p in reversed(used) if p["type"] == post_type]
        post = generator.generate_post(post_type, picked, tariff, cfg, complete, variant=variant, tip_topic=topic,
                                       checks=[check], history=history + made, now=now,
                                       hours=subject["hours"], recent_texts=same_type)
        if post["status"] == "approved":
            used.append(post)

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
    ap.add_argument("--variant", choices=generator.VARIANTS, default="link_in_reply")
    ap.add_argument("--out", help="생성된 글 전체를 이 JSON 파일로 저장 (검토용)")
    args = ap.parse_args(argv)

    if auto_publish_disabled():
        log.info("AUTO_PUBLISH_DISABLED 가 켜져 있어 생성하지 않습니다.")
        return 0
    try:
        cfg, secrets = load_config(), load_secrets()
        tariff, products = load_tariff(), load_products()
        review_call = reviewer_mod.make_call(cfg.reviewer_model, secrets)
        store = open_store(secrets)
        fb = cfg.generator_fallback_model
        complete = make_complete(cfg.generator_model, secrets.api_key_for(provider_of(cfg.generator_model)),
                                 fb, secrets.api_key_for(provider_of(fb)) if fb else None)
    except (ConfigError, LLMError, ValueError) as e:
        log.error(f"설정 오류: {e}")
        return 1
    result = run_generate(store, cfg, tariff, products, complete=complete,
                          review_call=review_call,
                          notifier=make_notifier(secrets), now=datetime.now(timezone.utc),
                          variant=args.variant, save=not args.dry_run)
    summary = [{"type": p["type"], "status": p["status"], "scheduled_at": p["scheduled_at"],
                "hook": guard.hook_line(p)} for p in result["posts"]]
    print(json.dumps({"action": result["action"], "posts": summary}, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result["posts"], f, ensure_ascii=False, indent=2)
        print(f"전체 글 저장: {args.out}")
    elif args.dry_run:
        print(json.dumps(result["posts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
