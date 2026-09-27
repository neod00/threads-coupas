"""
products.csv 로더 (PLAN 2-3).

컬럼: product_id, name, category, power_w, default_hours, link, note
- power_w 는 상품 페이지에서 직접 확인한 소비전력(W)
- link 는 쿠팡 파트너스 간편링크 (https://link.coupang.com/...)
- note 는 본인이 쓴 특징 1~2줄. 상품 설명·후기 복사 금지 (규칙 2), 안전·건강 주장 금지 — 해당 표현이 있으면 거부한다.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from settings import PROJECT_ROOT, ConfigError

PRODUCTS_PATH = PROJECT_ROOT / "data" / "products.csv"
COLUMNS = ("product_id", "name", "category", "power_w", "default_hours", "link", "note")
CATEGORIES = ("heater", "fan_heater", "mat", "blanket", "humidifier", "dehumidifier", "dryer", "other")
LINK_HOSTS = ("link.coupang.com", "coupa.ng")     # 파트너스 간편링크 도메인
MAX_NOTE_CHARS = 120
# 후기·상품 설명을 옮겨 온 흔적, 사용 경험 표현 (규칙 2)
REVIEW_COPY_PATTERN = re.compile(r"구매자|리뷰|후기|별점|평점|상품평|써보니|써 보니|써봤|써 봤|사용해보니|사용해 보니|우리 집")
# 안전·건강·효능 주장 (PLAN 2-3: note 에 안전·건강 주장 금지)
SAFETY_CLAIM_PATTERN = re.compile(r"안전|화재|무해|전자파|숙면|건강|걱정 없|아이 방")


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    category: str
    power_w: float
    default_hours: float
    link: str
    note: str


def _number(value: str, name: str, row: int, hi: float | None = None) -> float:
    try:
        n = float(value)
    except ValueError:
        raise ConfigError(f"products.csv {row}행 {name} 은 숫자여야 합니다: {value!r}") from None
    if n <= 0 or (hi is not None and n > hi):
        raise ConfigError(f"products.csv {row}행 {name} 값이 범위를 벗어났습니다: {value}")
    return n


def parse_products(rows: list[dict]) -> list[Product]:
    products, seen = [], set()
    for i, r in enumerate(rows, start=2):        # 1행은 머리글
        get = lambda k: (r.get(k) or "").strip()
        pid = get("product_id")
        if not pid:
            raise ConfigError(f"products.csv {i}행 product_id 가 비어 있습니다.")
        if pid in seen:
            raise ConfigError(f"products.csv product_id 가 중복됩니다: {pid}")
        seen.add(pid)
        for k in ("name", "category", "link"):
            if not get(k):
                raise ConfigError(f"products.csv {i}행 {k} 가 비어 있습니다.")
        if get("category") not in CATEGORIES:
            raise ConfigError(f"products.csv {i}행 category 는 {', '.join(CATEGORIES)} 중 하나여야 합니다: {get('category')}")
        url = urlparse(get("link"))
        if url.scheme != "https" or url.hostname not in LINK_HOSTS:
            raise ConfigError(f"products.csv {i}행 link 는 쿠팡 파트너스 간편링크(https://link.coupang.com/... 또는 https://coupa.ng/...)여야 합니다.")
        note = get("note")
        if len(note) > MAX_NOTE_CHARS:
            raise ConfigError(f"products.csv {i}행 note 가 깁니다 ({len(note)}자, 최대 {MAX_NOTE_CHARS}자). 본인이 쓴 1~2줄만 넣으세요.")
        if REVIEW_COPY_PATTERN.search(note):
            raise ConfigError(f"products.csv {i}행 note 에 후기 인용·사용 경험 표현이 있습니다 (규칙 2): {note}")
        if SAFETY_CLAIM_PATTERN.search(note):
            raise ConfigError(f"products.csv {i}행 note 에 안전·건강 주장이 있습니다: {note}")
        products.append(Product(
            product_id=pid,
            name=get("name"),
            category=get("category"),
            power_w=_number(get("power_w"), "power_w", i, hi=10_000),
            default_hours=_number(get("default_hours"), "default_hours", i, hi=24),
            link=get("link"),
            note=note,
        ))
    if not products:
        raise ConfigError("products.csv 에 상품이 없습니다. 상품 20~30개를 입력하세요.")
    return products


def load_products(path: Path = PRODUCTS_PATH) -> list[Product]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:   # 엑셀에서 저장한 BOM 도 읽는다
            reader = csv.DictReader(f)
            header = tuple(h.strip() for h in (reader.fieldnames or ()))
            if header != COLUMNS:
                raise ConfigError(f"products.csv 머리글은 {','.join(COLUMNS)} 이어야 합니다: {','.join(header)}")
            return parse_products(list(reader))
    except FileNotFoundError:
        raise ConfigError(f"상품 파일이 없습니다: {path}") from None
