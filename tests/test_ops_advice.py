"""운영 알림: 상품 링크 보충·발행 수 증가를 알려 준다 (알리기만 한다)."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

import digest
import ops_advice
import settings
from notify import CollectingNotifier
from store import MemoryStore

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class P:
    def __init__(self, i, power=None):
        self.product_id, self.power_w = i, power

    @property
    def has_power(self):
        return self.power_w is not None


def products(n):
    return [P(f"s{i}") for i in range(n)] + [P("heater", 900)]


@pytest.fixture
def cfg():
    return dataclasses.replace(settings.load_config(), post_types=("pick", "talk"), max_posts_per_day=3,
                               max_link_post_ratio=0.7, ops_advice=True)


def add(store, make_post, status, days_ago, **kw):
    t = (NOW - timedelta(days=days_ago)).isoformat()
    return store.insert_post(make_post(status=status, created_at=t, published_at=t if status == "published" else None, **kw))


def test_link_refill_warning_when_runway_short(cfg):
    notes = ops_advice.advice(MemoryStore(), cfg, products(12), NOW)          # 12개 / 하루 링크 2개 = 6일
    assert len(notes) == 1 and "상품 링크 보충 필요" in notes[0] and "12개" in notes[0] and "6일" in notes[0]
    assert "상품 10개 추가해줘" in notes[0]
    assert ops_advice.advice(MemoryStore(), cfg, products(20), NOW) == []     # 20개 / 2 = 10일 → 충분


def test_heater_with_power_is_not_counted(cfg):
    assert "12개" in ops_advice.advice(MemoryStore(), cfg, products(12), NOW)[0]


def test_no_link_warning_when_pick_is_off(cfg):
    cfg = dataclasses.replace(cfg, post_types=("talk",))
    assert ops_advice.advice(MemoryStore(), cfg, products(3), NOW) == []


def test_raise_posts_suggested_after_a_stable_week(cfg, make_post):
    s = MemoryStore()
    add(s, make_post, "published", 8)
    add(s, make_post, "published", 1)
    notes = ops_advice.advice(s, cfg, products(22), NOW)
    assert len(notes) == 1 and "발행 수 늘려도 돼요" in notes[0] and "3개 → 4개" in notes[0] and "4개로 올려줘" in notes[0]


def test_not_suggested_before_a_week_or_when_stopped_or_held_too_often(cfg, make_post):
    s = MemoryStore()
    add(s, make_post, "published", 5)
    assert ops_advice.advice(s, cfg, products(22), NOW) == []                           # 5일 < 7일
    s = MemoryStore()
    add(s, make_post, "published", 9)
    assert ops_advice.advice(s, cfg, products(22), NOW, stopped=True) == []             # 자동 정지 중
    for _ in range(3):
        add(s, make_post, "held", 2)
    assert ops_advice.advice(s, cfg, products(22), NOW) == []                           # 보류 3/4 > 20%


def test_raise_needs_enough_products_first(cfg, make_post):
    s = MemoryStore()
    add(s, make_post, "published", 9)
    notes = ops_advice.advice(s, cfg, products(18), NOW)
    assert any("상품이 20개는 있어야 해요" in n and "지금 18개" in n for n in notes)
    assert not any("늘려도 돼요" in n for n in notes)


def test_second_step_to_six_after_two_weeks_and_never_above_hard_max(cfg, make_post):
    four = dataclasses.replace(cfg, max_posts_per_day=4)
    s = MemoryStore()
    add(s, make_post, "published", 10)
    assert ops_advice.advice(s, four, products(32), NOW) == []                          # 10일 < 14일
    s = MemoryStore()
    add(s, make_post, "published", 15)
    notes = ops_advice.advice(s, four, products(32), NOW)
    assert len(notes) == 1 and "4개 → 6개" in notes[0]
    six = dataclasses.replace(cfg, max_posts_per_day=6)
    assert ops_advice.advice(s, six, products(44), NOW) == []                           # 더 올릴 단계 없음 (44개/링크 4개 = 11일)


def test_digest_shows_advice_only_when_enabled(cfg, make_post):
    s = MemoryStore()
    text = digest.build_digest(s, cfg, NOW, dry_run=True, products=products(12))
    assert "상품 링크 보충 필요" in text
    off = dataclasses.replace(cfg, ops_advice=False)
    assert "상품 링크 보충 필요" not in digest.build_digest(s, off, NOW, dry_run=True, products=products(12))
    assert "상품 링크 보충 필요" not in digest.build_digest(s, cfg, NOW, dry_run=True)        # 상품 목록이 없으면 건너뜀


def test_ops_advice_config_flag():
    import yaml
    raw = yaml.safe_load(settings.PROJECT_ROOT.joinpath("config.yaml").read_text(encoding="utf-8"))
    assert settings.parse_config(raw).ops_advice is False
    assert settings.parse_config(raw | {"ops_advice": True}).ops_advice is True
    with pytest.raises(settings.ConfigError):
        settings.parse_config(raw | {"ops_advice": "yes"})
