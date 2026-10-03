"""
전기요금 계산기 (PLAN Step 2).

숫자는 코드가 계산하고, LLM 은 이 결과(calc_payload)의 값만 옮겨 쓴다.
가드(Step 4)는 게시물의 숫자가 이 결과나 입력 데이터에 있는지 검사한다.

계산식
  monthly_kwh        = power_w × hours_per_day × days_per_month / 1000
  est_added_cost_won = monthly_kwh × 가정 누진 구간 단가 × surcharge_multiplier
  est_daily_cost_won = est_added_cost_won ÷ days_per_month (일반 독자는 W·kWh 보다 "하루 얼마"를 먼저 본다)

반올림 규칙 (모두 ROUND_HALF_UP, 계산은 Decimal 로 정확하게 한다)
  - 요금은 반올림하지 않은 kWh 로 계산한 뒤 100원 단위로 반올림한다 ("약 ○만 ○,○00원" 추정치).
  - 하루 요금은 반올림된 월 요금 ÷ 월 사용일수를 10원 단위로 반올림한다 ("하루 약 ○,○○0원" 환산). 글에 적힌 월 요금과 맞아떨어진다.
  - 표시용 kWh 는 소수 첫째 자리까지 반올림한다 (정수면 정수로 둔다).
  - 비교의 요금 차이는 반올림된 두 요금의 차다. 글에 적힌 두 요금을 빼면 같은 값이 나온다.

누진 구간 가정: 추가 사용량 전체에 가정 구간 단가 하나를 곱한다. 기본요금과 감면은 넣지 않는다.

점검:
  python calculator.py --power 2000 --hours 4 [--days 30]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from settings import PROJECT_ROOT, ConfigError, _parse_date

TARIFF_PATH = PROJECT_ROOT / "data" / "tariff.yaml"
DEFAULT_DAYS_PER_MONTH = 30
COST_ROUND_UNIT = Decimal(100)
DAILY_COST_ROUND_UNIT = Decimal(10)
KWH_ROUND_UNIT = Decimal("0.1")

_TARIFF_KEYS = {"effective_date", "season", "tiers", "surcharge_multiplier", "assumed_tier_for_posts"}


@dataclass(frozen=True)
class Tier:
    upto_kwh: Decimal | None        # 마지막 구간은 None
    rate_per_kwh: Decimal


@dataclass(frozen=True)
class Tariff:
    effective_date: date
    season: str
    tiers: tuple[Tier, ...]
    surcharge_multiplier: Decimal
    assumed_tier_for_posts: int     # 1부터 센다


# ── 요금표 로딩 ───────────────────────────────────────────────────────

def _decimal(value, name: str) -> Decimal:
    if value is None:
        raise ConfigError(f"tariff.yaml 의 {name} 이 비어 있습니다. 한전 사이버지점에서 확인한 값을 입력하세요.")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"tariff.yaml 의 {name} 은 숫자여야 합니다: {value!r}")
    try:
        # float 는 str 을 거쳐야 214.6 이 214.599999… 로 바뀌지 않는다.
        return Decimal(str(value))
    except ArithmeticError:
        raise ConfigError(f"tariff.yaml 의 {name} 은 숫자여야 합니다: {value!r}") from None


def load_tariff(path: Path = TARIFF_PATH) -> Tariff:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"요금표 파일이 없습니다: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"tariff.yaml 문법 오류: {e}") from None
    return parse_tariff(raw)


def parse_tariff(raw) -> Tariff:
    if not isinstance(raw, dict):
        raise ConfigError("tariff.yaml 최상위는 '키: 값' 목록이어야 합니다.")
    unknown = sorted(set(raw) - _TARIFF_KEYS)
    missing = sorted(_TARIFF_KEYS - set(raw))
    if unknown:
        raise ConfigError("tariff.yaml 에 모르는 키가 있습니다: " + ", ".join(map(str, unknown)))
    if missing:
        raise ConfigError("tariff.yaml 에 빠진 키가 있습니다: " + ", ".join(missing))

    if raw["effective_date"] is None:
        raise ConfigError("tariff.yaml 의 effective_date 가 비어 있습니다. 확인한 요금표 기준일을 입력하세요.")
    effective = _parse_date(raw["effective_date"], "tariff.yaml effective_date")

    season = raw["season"]
    if not isinstance(season, str) or not season.strip():
        raise ConfigError("tariff.yaml 의 season 이 비어 있습니다.")

    raw_tiers = raw["tiers"]
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise ConfigError("tariff.yaml 의 tiers 는 구간 목록이어야 합니다.")
    tiers = []
    for i, t in enumerate(raw_tiers, start=1):
        if not isinstance(t, dict) or set(t) != {"upto_kwh", "rate_per_kwh"}:
            raise ConfigError(f"tariff.yaml tiers {i}단계는 upto_kwh, rate_per_kwh 두 키만 가져야 합니다.")
        rate = _decimal(t["rate_per_kwh"], f"tiers {i}단계 rate_per_kwh")
        last = i == len(raw_tiers)
        if last:
            if t["upto_kwh"] is not None:
                raise ConfigError("tariff.yaml 마지막 구간의 upto_kwh 는 null 이어야 합니다.")
            upto = None
        else:
            upto = _decimal(t["upto_kwh"], f"tiers {i}단계 upto_kwh")
        if rate <= 0 or (upto is not None and upto <= 0):
            raise ConfigError(f"tariff.yaml tiers {i}단계 값은 0보다 커야 합니다.")
        tiers.append(Tier(upto, rate))

    for prev, cur in zip(tiers, tiers[1:]):
        if cur.upto_kwh is not None and cur.upto_kwh <= prev.upto_kwh:
            raise ConfigError("tariff.yaml 누진 구간 upto_kwh 는 단계마다 커져야 합니다.")
        if cur.rate_per_kwh < prev.rate_per_kwh:
            raise ConfigError("tariff.yaml 누진 구간 단가는 단계가 올라갈수록 작아질 수 없습니다.")

    multiplier = _decimal(raw["surcharge_multiplier"], "surcharge_multiplier")
    if not Decimal(1) <= multiplier < Decimal(2):
        raise ConfigError(f"tariff.yaml 의 surcharge_multiplier 는 1 이상 2 미만이어야 합니다: {multiplier}")

    tier = raw["assumed_tier_for_posts"]
    if isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= len(tiers):
        raise ConfigError(f"tariff.yaml 의 assumed_tier_for_posts 는 1~{len(tiers)} 이어야 합니다: {tier!r}")

    return Tariff(effective, season.strip(), tuple(tiers), multiplier, tier)


# ── 계산 ─────────────────────────────────────────────────────────────

def won_label(n) -> str:
    """4228161 → '422만 8,161원', 1700000000 → '17억 원', 787500 → '78만 7,500원'.
    로옥션 column_guard.py 의 won_label 과 같은 규칙."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    eok, rest = divmod(n, 100_000_000)
    man, won = divmod(rest, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    if won:
        parts.append(f"{won:,}")
    if not parts:
        return f"{n:,}원"
    return f"{' '.join(parts)}원" if won else f"{' '.join(parts)} 원"


def plain(d: Decimal) -> int | float:
    """JSON 에 넣을 숫자. 정수면 int (240.0 이 아니라 240)."""
    return int(d) if d == d.to_integral_value() else float(d)


def _positive(value, name: str, hi: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} 은 숫자여야 합니다: {value!r}")
    d = Decimal(str(value))
    if d <= 0 or (hi is not None and d > hi):
        raise ValueError(f"{name} 은 0보다 크고 {hi} 이하여야 합니다: {value}" if hi else f"{name} 은 0보다 커야 합니다: {value}")
    return d


def assumption_text(tier: int) -> str:
    return f"누진 {tier}단계 가정, 기본요금·감면 제외 추정"


def monthly_cost(power_w, hours_per_day, tariff: Tariff,
                 days_per_month: int = DEFAULT_DAYS_PER_MONTH, tier: int | None = None) -> dict:
    """한 제품의 월 사용량과 추가 전기요금 추정치 (calc_payload)."""
    power = _positive(power_w, "power_w")
    hours = _positive(hours_per_day, "hours_per_day", Decimal(24))
    if isinstance(days_per_month, bool) or not isinstance(days_per_month, int) or not 1 <= days_per_month <= 31:
        raise ValueError(f"days_per_month 는 1~31 정수여야 합니다: {days_per_month!r}")
    tier = tariff.assumed_tier_for_posts if tier is None else tier
    if not 1 <= tier <= len(tariff.tiers):
        raise ValueError(f"tier 는 1~{len(tariff.tiers)} 이어야 합니다: {tier}")

    rate = tariff.tiers[tier - 1].rate_per_kwh
    kwh = power * hours * days_per_month / 1000
    exact_cost = kwh * rate * tariff.surcharge_multiplier
    cost = int((exact_cost / COST_ROUND_UNIT).quantize(Decimal(1), ROUND_HALF_UP) * COST_ROUND_UNIT)
    daily = int((Decimal(cost) / days_per_month / DAILY_COST_ROUND_UNIT).quantize(Decimal(1), ROUND_HALF_UP)
                * DAILY_COST_ROUND_UNIT)

    return {
        "power_w": plain(power),
        "hours_per_day": plain(hours),
        "days_per_month": days_per_month,
        "monthly_kwh": plain(kwh.quantize(KWH_ROUND_UNIT, ROUND_HALF_UP)),
        "assumed_tier": tier,
        "rate_per_kwh": plain(rate),
        "surcharge_multiplier": plain(tariff.surcharge_multiplier),
        "est_added_cost_won": cost,
        "est_added_cost_label": won_label(cost),
        "est_daily_cost_won": daily,
        "est_daily_cost_label": won_label(daily),
        "assumption_text": assumption_text(tier),
        "tariff_effective_date": tariff.effective_date.isoformat(),
    }


def compare(a: dict, b: dict, tariff: Tariff,
            days_per_month: int = DEFAULT_DAYS_PER_MONTH, tier: int | None = None) -> dict:
    """두 상품(또는 같은 상품의 두 방식)의 월 kWh·추정 요금 차이.
    a, b = {"name": ..., "power_w": ..., "hours_per_day": ...}. 사용일수·누진 구간은 두 쪽이 같다."""
    items = {}
    for key, item in (("a", a), ("b", b)):
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"compare 의 {key} 에 name 이 없습니다.")
        items[key] = {"name": name, **monthly_cost(item["power_w"], item["hours_per_day"], tariff, days_per_month, tier)}

    ca, cb = items["a"], items["b"]
    kwh_diff = abs(Decimal(str(ca["monthly_kwh"])) - Decimal(str(cb["monthly_kwh"])))
    cost_diff = abs(ca["est_added_cost_won"] - cb["est_added_cost_won"])
    if ca["est_added_cost_won"] == cb["est_added_cost_won"]:
        higher = None
    else:
        higher = "a" if ca["est_added_cost_won"] > cb["est_added_cost_won"] else "b"
    return {
        "a": ca,
        "b": cb,
        "kwh_diff": plain(kwh_diff),
        "cost_diff_won": cost_diff,
        "cost_diff_label": won_label(cost_diff),
        "higher_cost": higher,              # 요금이 더 나오는 쪽 ("a" / "b" / None)
        "higher_cost_name": items[higher]["name"] if higher else None,
        "assumption_text": ca["assumption_text"],
        "tariff_effective_date": ca["tariff_effective_date"],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="월 추가 전기요금 추정 (data/tariff.yaml 사용)")
    ap.add_argument("--power", type=float, required=True, help="소비전력 W")
    ap.add_argument("--hours", type=float, required=True, help="하루 사용시간")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS_PER_MONTH, help="월 사용일수")
    args = ap.parse_args()
    try:
        tariff = load_tariff()
        result = monthly_cost(args.power, args.hours, tariff, args.days)
    except (ConfigError, ValueError) as e:
        print(f"오류: {e}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
