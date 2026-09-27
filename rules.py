"""
발행 일정 규칙 (PLAN v2 규칙 5, Step 4-B 슬롯 배정). 슬롯 배정과 발행기(Step 6)가 같은 기준을 쓴다.

- 하루 = KST 달력일
- 하루 최대 개수: config.max_posts_per_day (코드 상한 5)
- 게시 간격: 이미 예약·발행된 모든 글과 min_gap_hours 이상
- 링크 글 비율: 하루 링크 글 수 ≤ floor(max_link_post_ratio × max_posts_per_day)  (4개 × 0.6 → 하루 링크 글 2개까지)
- link_in_reply 답글은 본문 게시물의 일부로 보고 따로 세지 않는다
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from settings import HARD_MAX_POSTS_PER_DAY, KST, Config

OCCUPYING = ("approved", "published")      # 슬롯을 차지하는 상태 (cancelled·held·failed 는 비운다)


def as_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"시간대 없는 시각은 쓰지 않습니다: {value!r}")
    return dt


def kst_date(dt: datetime) -> date:
    return dt.astimezone(KST).date()


def post_time(post: dict) -> datetime | None:
    """글이 차지하는 시각: 발행됐으면 published_at, 아니면 scheduled_at."""
    return as_dt(post.get("published_at")) or as_dt(post.get("scheduled_at"))


def occupied(posts) -> list[tuple[datetime, bool]]:
    """(시각, 링크 글 여부) 목록. 예약·발행된 글만."""
    out = []
    for p in posts:
        t = post_time(p)
        if p.get("status") in OCCUPYING and t is not None:
            out.append((t, bool(p.get("has_link"))))
    return out


def max_link_posts_per_day(cfg: Config) -> int:
    # 부동소수 오차(0.6 × 4 = 2.4000000000000004) 때문에 작은 여유를 두고 내린다
    return math.floor(cfg.max_link_post_ratio * cfg.max_posts_per_day + 1e-9)


def slot_problems(when: datetime, has_link: bool, posts, cfg: Config) -> list[str]:
    """이 시각에 이 글을 두면 어기는 규칙 목록 (빈 목록 = 가능). 발행기도 발행 직전에 쓴다."""
    problems = []
    taken = occupied(posts)
    day = kst_date(when)
    same_day = [(t, link) for t, link in taken if kst_date(t) == day]
    daily_cap = min(cfg.max_posts_per_day, HARD_MAX_POSTS_PER_DAY)
    if len(same_day) >= daily_cap:
        problems.append(f"규칙 5: {day} 에 이미 {len(same_day)}개가 예약·발행됨 (하루 최대 {daily_cap}개)")
    if has_link and sum(link for _, link in same_day) >= max_link_posts_per_day(cfg):
        problems.append(f"{day} 링크 글이 이미 {max_link_posts_per_day(cfg)}개 (링크 글 비율 {cfg.max_link_post_ratio:.0%} 이하)")
    gap = timedelta(hours=cfg.min_gap_hours)
    close = [t for t, _ in taken if abs(t - when) < gap]
    if close:
        nearest = min(close, key=lambda t: abs(t - when)).astimezone(KST)
        problems.append(f"규칙 5: {nearest:%m-%d %H:%M} KST 글과 간격이 {cfg.min_gap_hours:g}시간 미만")
    return problems


def next_slot(posts, has_link: bool, cfg: Config, after: datetime, horizon_days: int = 7) -> datetime | None:
    """after 이후 가장 이른 빈 슬롯 (KST slots_kst 중). horizon_days 안에 없으면 None."""
    start = kst_date(after)
    for d in range(horizon_days + 1):
        day = start + timedelta(days=d)
        for slot in cfg.slots_kst:
            when = datetime.combine(day, slot, tzinfo=KST)
            if when > after and not slot_problems(when, has_link, posts, cfg):
                return when
    return None
