"""
운영 알림 (2026-10-18). 일일 요약에 "상품 링크를 더 만들어야 할 때"와 "하루 발행 수를 늘려도 될 때"를 알려 준다.
알리기만 한다. 설정을 바꾸거나 링크를 만들지는 않는다 — 사람이 승인하고 Claude 에게 시킨다.

config.yaml 의 ops_advice: true 인 계정에만 나온다 (기본 꺼짐).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import rules
from settings import HARD_MAX_POSTS_PER_DAY, Config

LINK_RUNWAY_WARN_DAYS = 10        # 같은 상품이 다시 나가기까지 이 날수보다 짧으면 링크 보충을 알린다
HELD_WINDOW_DAYS = 7
MAX_HELD_RATIO = 0.2              # 최근 7일 보류 비율이 이보다 높으면 발행 수를 늘리자고 하지 않는다
# (하루 발행 수, 첫 발행 뒤 최소 일수, 소개 글에 쓸 수 있는 상품 최소 개수) — 신규 계정이 천천히 늘리도록
RAMP = ((4, 7, 20), (6, 14, 30))


def _first_published(store) -> datetime | None:
    times = [rules.as_dt(p["published_at"]) for p in store.list_posts(["published"]) if p.get("published_at")]
    return min(times) if times else None


def _held_ratio(store, now: datetime) -> float:
    since = now - timedelta(days=HELD_WINDOW_DAYS)
    posts = [p for p in store.list_posts(["held", "approved", "published"])
             if p.get("created_at") and rules.as_dt(p["created_at"]) >= since]
    return sum(p["status"] == "held" for p in posts) / len(posts) if posts else 0.0


def advice(store, cfg: Config, products, now: datetime, *, stopped: bool = False) -> list[str]:
    """알려 줄 문장 목록 (없으면 빈 목록)."""
    out = []
    pickable = [p for p in products if not p.has_power]
    n = len(pickable)
    links = rules.max_link_posts_per_day(cfg)

    if links and "pick" in cfg.post_types and n / links < LINK_RUNWAY_WARN_DAYS:
        out.append(f"[상품 링크 보충 필요] 소개 글에 쓸 상품이 {n}개예요. 하루 링크 글이 {links}개라 약 {n / links:.0f}일마다 "
                   "같은 상품이 다시 나가요. Claude 에게 \"상품 10개 추가해줘\"라고 요청해 주세요.")

    first = _first_published(store)
    target = next((r for r in RAMP if r[0] > cfg.max_posts_per_day and r[0] <= HARD_MAX_POSTS_PER_DAY), None)
    if first is None or target is None or stopped:
        return out
    posts, min_days, min_products = target
    days = (now - first).days
    if days < min_days:
        return out
    if _held_ratio(store, now) > MAX_HELD_RATIO:
        return out
    if n < min_products:
        out.append(f"[발행 수 늘리기 준비] 첫 발행 후 {days}일이 지나 하루 {posts}개로 늘릴 수 있어요. "
                   f"다만 상품이 {min_products}개는 있어야 해요 (지금 {n}개). 상품을 먼저 {min_products - n}개 이상 추가해 주세요.")
    else:
        out.append(f"[발행 수 늘려도 돼요] 첫 발행 후 {days}일 동안 보류 비율이 낮고 지금 자동 정지도 없어요. 하루 {cfg.max_posts_per_day}개 → "
                   f"{posts}개로 늘려도 돼요 (상품 {n}개). Claude 에게 \"발행 수 {posts}개로 올려줘\"라고 요청해 주세요 (발행 시각도 같이 늘려요).")
    return out
