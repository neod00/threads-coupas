"""
Step 2 계산기 테스트.

8장 체크리스트: 계산기 단위 테스트 3케이스 통과 (손계산)

요금표는 테스트용 값이다 (실제 한전 요금이 아님). 실제 값은 사람이 data/tariff.yaml 에 입력한다.
  1단계 ~200kWh 120.0원, 2단계 ~400kWh 214.6원, 3단계 307.3원, 배수 1.137, 가정 구간 2단계
"""
import copy
from datetime import date

import pytest

import calculator
from settings import ConfigError

RAW = {
    "effective_date": "2026-10-01",
    "season": "winter",
    "tiers": [
        {"upto_kwh": 200, "rate_per_kwh": 120.0},
        {"upto_kwh": 400, "rate_per_kwh": 214.6},
        {"upto_kwh": None, "rate_per_kwh": 307.3},
    ],
    "surcharge_multiplier": 1.137,
    "assumed_tier_for_posts": 2,
}


@pytest.fixture
def tariff():
    return calculator.parse_tariff(copy.deepcopy(RAW))


# ── 8장: 손계산 3케이스 ──────────────────────────────────────────────────

def test_case1_heater_2000w_4h(tariff):
    # 2000 × 4 × 30 / 1000 = 240 kWh
    # 240 × 214.6 = 51,504 → × 1.137 = 58,560.048 → 100원 반올림 58,600
    r = calculator.monthly_cost(2000, 4, tariff)
    assert r["monthly_kwh"] == 240
    assert r["est_added_cost_won"] == 58_600
    assert r["est_added_cost_label"] == "5만 8,600원"
    assert r["assumption_text"] == "누진 2단계 가정, 기본요금·감면 제외 추정"
    assert r["tariff_effective_date"] == "2026-10-01"


def test_case2_fan_heater_1500w_3_5h(tariff):
    # 1500 × 3.5 × 30 / 1000 = 157.5 kWh
    # 157.5 × 214.6 = 33,799.5 → × 1.137 = 38,430.0315 → 38,400
    r = calculator.monthly_cost(1500, 3.5, tariff)
    assert r["monthly_kwh"] == 157.5
    assert r["hours_per_day"] == 3.5
    assert r["est_added_cost_won"] == 38_400
    assert r["est_added_cost_label"] == "3만 8,400원"


def test_case3_electric_blanket_55w_8h_31d_tier1(tariff):
    # 55 × 8 × 31 / 1000 = 13.64 kWh → 표시 13.6
    # 요금은 반올림 전 kWh 로: 13.64 × 120.0 = 1,636.8 → × 1.137 = 1,861.0416 → 1,900
    r = calculator.monthly_cost(55, 8, tariff, days_per_month=31, tier=1)
    assert r["monthly_kwh"] == 13.6
    assert r["assumed_tier"] == 1
    assert r["rate_per_kwh"] == 120
    assert r["est_added_cost_won"] == 1_900
    assert r["est_added_cost_label"] == "1,900원"
    assert r["assumption_text"] == "누진 1단계 가정, 기본요금·감면 제외 추정"


# ── 비교 함수 ────────────────────────────────────────────────────────────

def test_compare_two_modes(tariff):
    # a: 58,600원 (케이스 1). b: 120 kWh × 214.6 × 1.137 = 29,280.024 → 29,300원
    r = calculator.compare(
        {"name": "고출력", "power_w": 2000, "hours_per_day": 4},
        {"name": "저전력 모드", "power_w": 1000, "hours_per_day": 4},
        tariff,
    )
    assert r["a"]["est_added_cost_won"] == 58_600
    assert r["b"]["est_added_cost_won"] == 29_300
    assert r["kwh_diff"] == 120
    # 차이는 반올림된 두 요금의 차: 글에 적힌 두 값을 빼면 같은 값
    assert r["cost_diff_won"] == 29_300 == r["a"]["est_added_cost_won"] - r["b"]["est_added_cost_won"]
    assert r["cost_diff_label"] == "2만 9,300원"
    assert (r["higher_cost"], r["higher_cost_name"]) == ("a", "고출력")


def test_compare_same_cost_has_no_higher_side(tariff):
    r = calculator.compare({"name": "A", "power_w": 1000, "hours_per_day": 2},
                           {"name": "B", "power_w": 2000, "hours_per_day": 1}, tariff)
    assert r["cost_diff_won"] == 0 and r["higher_cost"] is None


def test_compare_requires_names(tariff):
    with pytest.raises(ValueError, match="name"):
        calculator.compare({"power_w": 1, "hours_per_day": 1}, {"name": "B", "power_w": 1, "hours_per_day": 1}, tariff)


# ── 반올림 규칙 ──────────────────────────────────────────────────────────

def test_cost_rounds_half_up_to_100_won():
    raw = copy.deepcopy(RAW)
    raw.update(surcharge_multiplier=1, tiers=[{"upto_kwh": None, "rate_per_kwh": 100}], assumed_tier_for_posts=1)
    t = calculator.parse_tariff(raw)
    assert calculator.monthly_cost(50, 1, t, days_per_month=10)["est_added_cost_won"] == 100   # 정확히 50원 → 100
    assert calculator.monthly_cost(49, 1, t, days_per_month=10)["est_added_cost_won"] == 0     # 49원 → 0
    assert calculator.monthly_cost(25, 1, t, days_per_month=10)["monthly_kwh"] == 0.3          # 0.25 kWh → 0.3


def test_payload_numbers_are_plain(tariff):
    r = calculator.monthly_cost(2000.0, 4.0, tariff)
    assert isinstance(r["power_w"], int) and isinstance(r["monthly_kwh"], int)   # 240.0 이 아니라 240


# ── 입력 검증 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"power_w": 0, "hours_per_day": 4},
    {"power_w": -100, "hours_per_day": 4},
    {"power_w": 2000, "hours_per_day": 25},
    {"power_w": 2000, "hours_per_day": 0},
    {"power_w": "2000", "hours_per_day": 4},
    {"power_w": 2000, "hours_per_day": 4, "days_per_month": 32},
    {"power_w": 2000, "hours_per_day": 4, "tier": 4},
])
def test_invalid_inputs_rejected(tariff, kwargs):
    with pytest.raises(ValueError):
        calculator.monthly_cost(tariff=tariff, **kwargs)


# ── 요금표 검증 ──────────────────────────────────────────────────────────

def test_blank_template_is_rejected():
    # 사람이 값을 넣기 전의 템플릿 모양
    raw = {**RAW, "effective_date": None, "surcharge_multiplier": None,
           "tiers": [{"upto_kwh": None, "rate_per_kwh": None}] * 3}
    with pytest.raises(ConfigError, match="비어 있습니다"):
        calculator.parse_tariff(raw)


@pytest.mark.parametrize("changes, message", [
    ({"surcharge_multiplier": None}, "surcharge_multiplier"),
    ({"surcharge_multiplier": 0.9}, "surcharge_multiplier"),
    ({"assumed_tier_for_posts": 4}, "assumed_tier_for_posts"),
    ({"tiers": [{"upto_kwh": 200, "rate_per_kwh": 120}, {"upto_kwh": 100, "rate_per_kwh": 214.6},
                {"upto_kwh": None, "rate_per_kwh": 307.3}]}, "커져야"),
    ({"tiers": [{"upto_kwh": 200, "rate_per_kwh": 300}, {"upto_kwh": None, "rate_per_kwh": 120}]}, "작아질 수 없습니다"),
    ({"tiers": [{"upto_kwh": 200, "rate_per_kwh": 120}, {"upto_kwh": 400, "rate_per_kwh": 214.6}]}, "마지막 구간"),
    ({"effective_date": "2026/10/01"}, "YYYY-MM-DD"),
    ({"extra": 1}, "모르는 키"),
])
def test_invalid_tariff_rejected(changes, message):
    raw = {**copy.deepcopy(RAW), **changes}
    with pytest.raises(ConfigError, match=message):
        calculator.parse_tariff(raw)


def test_float_rates_kept_exact(tariff):
    assert str(tariff.tiers[1].rate_per_kwh) == "214.6"
    assert tariff.effective_date == date(2026, 10, 1)
