"""슬롯 배정 규칙 테스트 (규칙 5: 하루 최대 개수·최소 간격, 링크 글 비율). 발행기도 같은 slot_problems 를 쓴다."""
from datetime import datetime

import pytest

import rules
from settings import KST


def kst(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=KST)


def at(post_maker, day, hm, has_link=True, status="approved", published=False):
    field = "published_at" if published else "scheduled_at"
    return post_maker(status=status, has_link=has_link, **{field: kst(day, hm).isoformat()})


def test_first_free_slot_after_now(cfg):
    assert rules.next_slot([], True, cfg, after=kst(2, "20:00")) == kst(2, "21:30")
    assert rules.next_slot([], True, cfg, after=kst(2, "21:30")) == kst(3, "08:10")    # 같은 시각은 지난 것으로 본다


def test_daily_cap_moves_to_next_day(cfg, make_post):
    posts = [at(make_post, 3, hm, has_link=False) for hm in ("08:10", "12:30", "18:40", "21:30")]
    assert rules.next_slot(posts, False, cfg, after=kst(2, "22:00")) == kst(4, "08:10")


def test_link_ratio_per_day(cfg, make_post):
    # 하루 4개 × 0.6 → 링크 글은 하루 2개까지
    assert rules.max_link_posts_per_day(cfg) == 2
    posts = [at(make_post, 3, "08:10"), at(make_post, 3, "12:30")]
    assert rules.next_slot(posts, True, cfg, after=kst(2, "22:00")) == kst(4, "08:10")
    assert rules.next_slot(posts, False, cfg, after=kst(2, "22:00")) == kst(3, "18:40")


def test_min_gap_from_published_post(cfg, make_post):
    posts = [at(make_post, 3, "17:30", published=True, status="published")]
    # 18:40 은 17:30 발행 글과 1시간 10분 차이 → 건너뜀
    assert rules.next_slot(posts, False, cfg, after=kst(3, "13:00")) == kst(3, "21:30")


@pytest.mark.parametrize("status", ["cancelled", "held", "failed", "draft"])
def test_non_occupying_statuses_free_the_slot(cfg, make_post, status):
    posts = [at(make_post, 2, "21:30", status=status)]
    assert rules.next_slot(posts, True, cfg, after=kst(2, "20:00")) == kst(2, "21:30")


def test_slot_problems_explain_rules(cfg, make_post):
    posts = [at(make_post, 3, "08:10"), at(make_post, 3, "12:30"), at(make_post, 3, "18:40", has_link=False),
             at(make_post, 3, "21:30", has_link=False)]
    problems = rules.slot_problems(kst(3, "22:30"), True, posts, cfg)
    assert any("하루 최대 4개" in p for p in problems)
    assert any("링크 글이 이미 2개" in p for p in problems)
    assert any("간격이 2시간 미만" in p for p in problems)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        rules.as_dt("2026-11-03T08:10:00")
