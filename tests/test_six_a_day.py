"""2026-10-17: 하루 6개 (규칙 5 개정). 간격 2시간·링크 글 60% 이하는 그대로."""
import dataclasses
from datetime import date, datetime, timedelta

import pipeline
import publisher
import rules
import settings
from conftest import coupang_config, coupang_raw
from settings import KST


def test_six_slots_keep_two_hour_gap(cfg):
    six = coupang_config()
    minutes = [t.hour * 60 + t.minute for t in six.slots_kst]
    assert len(minutes) == 6
    assert all(b - a >= 120 for a, b in zip(minutes, minutes[1:] + [minutes[0] + 1440]))
    assert rules.max_link_posts_per_day(six) == 3             # 링크 글 3개, 링크 없는 글 3개


def test_code_cap_is_six_and_cannot_be_exceeded():
    assert settings.HARD_MAX_POSTS_PER_DAY == 6
    raw = {"max_posts_per_day": 7}
    import yaml
    base = coupang_raw()
    try:
        settings.parse_config({**base, **raw})
    except settings.ConfigError as e:
        assert "규칙 5" in str(e)
    else:
        raise AssertionError("7개가 허용됨")


def test_plan_day_six_posts_is_three_links_and_three_talks():
    six = coupang_config()
    day = date(2026, 10, 20)
    plan = pipeline.plan_day([], six, day, True, has_picks=True, has_calc=True, has_bundle=True)
    assert plan == ["bundle", "pick", "pick", "talk", "talk", "talk"]
    assert pipeline.plan_day([], six, day, True, has_picks=True, has_calc=True, has_bundle=False) == \
        ["pick", "pick", "pick", "talk", "talk", "talk"]


def test_next_slots_fill_in_order_and_respect_link_limit():
    six = coupang_config()
    day = datetime(2026, 10, 20, tzinfo=KST)
    made, got = [], []
    for has_link in (True, False, True, False, True, False):
        slot = rules.next_slot(made, has_link, six, after=day, horizon_days=0)
        assert slot is not None
        made.append({"scheduled_at": slot.isoformat(), "has_link": has_link, "status": "approved"})
        got.append(slot.astimezone(KST).strftime("%H:%M"))
    assert got == ["08:10", "10:30", "13:00", "16:00", "19:00", "21:30"]
    # 7번째 글은 둘 곳이 없다
    assert rules.next_slot(made, False, six, after=day, horizon_days=0) is None
    # 링크 글 4번째는 비율(60%) 때문에 둘 수 없다
    assert rules.next_slot(made[:5], True, six, after=day, horizon_days=0) is None


def test_publisher_daily_cap_follows_config():
    six = coupang_config()
    now = datetime(2026, 10, 20, 22, 0, tzinfo=KST)
    published = [{"id": f"p{i}", "published_at": (now - timedelta(hours=3 + i * 2)).isoformat()} for i in range(5)]
    post = {"id": "new"}
    # 오늘 5개 발행 → 6번째는 가능, 7번째는 거부
    assert not [p for p in publisher.publish_time_problems(post, published, six, now) if "오늘 이미" in p]
    six_done = published + [{"id": "p6", "published_at": (now - timedelta(hours=2, minutes=30)).isoformat()}]
    assert any("오늘 이미 6개" in p for p in publisher.publish_time_problems(post, six_done, six, now))
