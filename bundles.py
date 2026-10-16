"""
시즌 모음(bundle) 글 테마 (PLAN v4, 2026-10-16). data/bundles.yaml 을 읽는다.

테마 하나 = 상품 3개 + 쿠팡 파트너스 "검색 결과 공유" 링크 1개 (글 하나에 링크 1개 규칙 그대로).
상품 메모는 products.csv 와 같은 검사를 받는다 (후기 인용·경험 표현·안전·건강 주장 금지).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from products import LINK_HOSTS, MAX_NOTE_CHARS, REVIEW_COPY_PATTERN, SAFETY_CLAIM_PATTERN
from settings import PROJECT_ROOT, ConfigError

BUNDLES_PATH = PROJECT_ROOT / "data" / "bundles.yaml"
ITEMS_PER_BUNDLE = 3
MMDD = re.compile(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")


@dataclass(frozen=True)
class BundleItem:
    name: str
    note: str


@dataclass(frozen=True)
class Bundle:
    bundle_id: str
    theme: str
    keyword: str
    link: str
    season: tuple[str, str] | None      # ("10-25", "11-11"). None 이면 언제나
    items: tuple[BundleItem, ...]

    def in_season(self, day: date) -> bool:
        if self.season is None:
            return True
        start, end = self.season
        today = f"{day:%m-%d}"
        # 해를 넘기는 기간 (예: 10-15 ~ 02-28)
        return start <= today <= end if start <= end else today >= start or today <= end


def _real_day(mmdd: str) -> bool:
    """11-31 같은 없는 날짜를 거른다 (윤년 기준이라 02-29 는 허용)."""
    try:
        date.fromisoformat(f"2024-{mmdd}")
        return True
    except ValueError:
        return False


def _check_note(note: str, where: str) -> None:
    if not note or len(note) > MAX_NOTE_CHARS:
        raise ConfigError(f"{where} note 는 1~{MAX_NOTE_CHARS}자여야 합니다.")
    if REVIEW_COPY_PATTERN.search(note):
        raise ConfigError(f"{where} note 에 후기 인용·사용 경험 표현이 있습니다 (규칙 2): {note}")
    if SAFETY_CLAIM_PATTERN.search(note):
        raise ConfigError(f"{where} note 에 안전·건강 주장이 있습니다: {note}")


def parse_bundles(raw) -> list[Bundle]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("bundles.yaml 은 테마 목록이어야 합니다.")
    out, seen = [], set()
    for i, b in enumerate(raw, start=1):
        where = f"bundles.yaml {i}번째 테마"
        if not isinstance(b, dict) or set(b) != {"id", "theme", "keyword", "link", "season", "items"}:
            raise ConfigError(f"{where} 는 id, theme, keyword, link, season, items 키를 가져야 합니다.")
        bid = str(b["id"]).strip()
        if not bid or bid in seen:
            raise ConfigError(f"{where} id 가 비었거나 중복입니다: {bid!r}")
        seen.add(bid)
        url = urlparse(str(b["link"]).strip())
        if url.scheme != "https" or url.hostname not in LINK_HOSTS or not url.path.strip("/"):
            raise ConfigError(f"{where} link 는 쿠팡 파트너스 간편링크여야 합니다.")
        season = b["season"]
        if season is not None:
            if (not isinstance(season, list) or len(season) != 2
                    or not all(isinstance(s, str) and MMDD.fullmatch(s) and _real_day(s) for s in season)):
                raise ConfigError(f'{where} season 은 ["MM-DD", "MM-DD"] 또는 null 이어야 합니다: {season!r}')
            season = (season[0], season[1])
        items = b["items"]
        if not isinstance(items, list) or len(items) != ITEMS_PER_BUNDLE:
            raise ConfigError(f"{where} items 는 상품 {ITEMS_PER_BUNDLE}개여야 합니다.")
        parsed = []
        for j, it in enumerate(items, start=1):
            if not isinstance(it, dict) or set(it) != {"name", "note"} or not str(it["name"]).strip():
                raise ConfigError(f"{where} items {j}번째는 name, note 를 가져야 합니다.")
            _check_note(str(it["note"]).strip(), f"{where} items {j}번째")
            parsed.append(BundleItem(str(it["name"]).strip(), str(it["note"]).strip()))
        out.append(Bundle(bid, str(b["theme"]).strip(), str(b["keyword"]).strip(), str(b["link"]).strip(),
                          season, tuple(parsed)))
    return out


def load_bundles(path: Path = BUNDLES_PATH) -> list[Bundle]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except yaml.YAMLError as e:
        raise ConfigError(f"bundles.yaml 문법 오류: {e}") from None
    return parse_bundles(raw)
