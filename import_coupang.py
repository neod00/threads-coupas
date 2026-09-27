"""
쿠팡 파트너스 일별 리포트 적재 (PLAN v2 Step 8). 파트너스 API 는 최종 승인 전이라 쓰지 않는다.
주 1회 파트너스 사이트의 일별 리포트를 CSV 로 받아 넣는다 (5분). 같은 날짜는 덮어쓴다.

CSV 머리글 (영문 또는 한글 둘 다 됨)
  date  | 날짜, 일자
  clicks | 클릭수, 클릭
  orders | 주문수, 주문, 구매수
  commission | 수수료, 수익, 커미션   ("12,345원" 같은 표기도 읽는다)

실행:
  python import_coupang.py data/coupang_2026-11-02.csv [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

from settings import ConfigError, _parse_date

ALIASES = {
    "date": ("date", "날짜", "일자"),
    "clicks": ("clicks", "클릭수", "클릭"),
    "orders": ("orders", "주문수", "주문", "구매수"),
    "commission": ("commission", "수수료", "수익", "커미션"),
}


def _number(value: str, name: str, row: int) -> int:
    text = re.sub(r"[,\s원]", "", value or "")
    if not re.fullmatch(r"\d+", text):
        raise ConfigError(f"{row}행 {name} 은 0 이상의 정수여야 합니다: {value!r}")
    return int(text)


def parse_rows(rows: list[dict], today: date | None = None) -> list[dict]:
    today = today or date.today()
    if not rows:
        raise ConfigError("CSV 에 행이 없습니다")
    headers = {h.strip(): h for h in rows[0].keys() if h}
    columns = {}
    for field, names in ALIASES.items():
        found = next((headers[n] for n in names if n in headers), None)
        if not found:
            raise ConfigError(f"CSV 에 '{field}' 열이 없습니다 (가능한 이름: {', '.join(names)})")
        columns[field] = found
    out, seen = [], set()
    for i, r in enumerate(rows, start=2):
        day = _parse_date((r[columns["date"]] or "").strip().replace(".", "-").replace("/", "-"), f"{i}행 날짜")
        if day in seen:
            raise ConfigError(f"같은 날짜가 두 번 있습니다: {day}")
        seen.add(day)
        if day > today:
            raise ConfigError(f"{i}행 날짜가 미래입니다: {day}")
        out.append({"date": day.isoformat(),
                    "clicks": _number(r[columns["clicks"]], "clicks", i),
                    "orders": _number(r[columns["orders"]], "orders", i),
                    "commission": _number(r[columns["commission"]], "commission", i)})
    return out


def load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return parse_rows(list(csv.DictReader(f)))


def main(argv=None) -> int:
    from settings import load_secrets
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="쿠팡 파트너스 일별 리포트 CSV 적재")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="검사만 하고 저장하지 않음")
    args = ap.parse_args(argv)
    try:
        rows = load_csv(args.csv)
        if not args.dry_run:
            open_store(load_secrets()).upsert_coupang(rows)
    except (ConfigError, FileNotFoundError) as e:
        print(f"실패: {e}")
        return 1
    total = {k: sum(r[k] for r in rows) for k in ("clicks", "orders", "commission")}
    print(f"{'검사만 함' if args.dry_run else '저장'}: {len(rows)}일 ({rows[0]['date']} ~ {rows[-1]['date']}) "
          f"클릭 {total['clicks']}, 주문 {total['orders']}, 수수료 {total['commission']:,}원")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
