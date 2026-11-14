"""
쿠팡 계정의 선물지니 글(gift) 글감 (2026-11-14 사용자 결정, PLAN 1-4).

선물지니는 사용자가 직접 만든 선물 추천 웹앱이고, 앱 안의 상품 링크가 사용자의 쿠팡 파트너스로 연결된다.
sabol.kka 는 수·토요일에 링크 글 한 자리를 이 앱 소개 글로 쓴다 (주 2회).

- 글감은 계정 폴더의 data/gift_app.yaml 뿐이다. 다른 계정 파일로 대신하지 않는다 (없으면 gift 글을 만들지 않는다).
- 링크 답글: 첫 줄 대가성 문구(cfg.disclosure_text), 둘째 줄 OWNER_LINE("내가 만든 앱"), 빈 줄, 안내 문장, 링크.
  앱에서 쿠팡으로 넘어가 구매하면 수수료를 받으므로 쿠팡 대가성 문구를 그대로 둔다 (가드 G2 가 두 줄을 검사).
- 링크는 gift_app.yaml 의 url 호스트만 통과한다 (발행 직전 link_checker).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from settings import ACCOUNT_DIR, ConfigError

GIFT_PATH = ACCOUNT_DIR / "data" / "gift_app.yaml"
OWNER_LINE = "선물지니는 내가 만든 선물 추천 앱이야."
GIFT_WEEKDAYS = (2, 5)            # 수요일·토요일 (date.weekday()). 주 2회
MAX_FACT_CHARS = 120


@dataclass(frozen=True)
class Occasion:
    theme: str
    season: tuple[tuple[int, int], tuple[int, int]] | None = None    # ((월, 일), (월, 일)), 양 끝 포함

    def in_season(self, day: date) -> bool:
        if self.season is None:
            return True
        (m1, d1), (m2, d2) = self.season
        start, end, today = (m1, d1), (m2, d2), (day.month, day.day)
        return start <= today <= end if start <= end else (today >= start or today <= end)   # 연말을 넘는 기간


@dataclass(frozen=True)
class GiftApp:
    name: str
    url: str
    one_liner: str
    facts: tuple[str, ...]
    occasions: tuple[Occasion, ...]

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower()


def _mmdd(value, where: str) -> tuple[int, int]:
    try:
        m, d = (int(x) for x in str(value).split("-"))
        date(2024, m, d)            # 윤년이라 02-29 도 받는다
        return m, d
    except (ValueError, TypeError):
        raise ConfigError(f"gift_app.yaml {where} 의 날짜는 'MM-DD' 형식이어야 합니다: {value!r}") from None


def parse_gift_app(raw: dict) -> GiftApp:
    app = (raw or {}).get("app") or {}
    name, url, one_liner = (str(app.get(k) or "").strip() for k in ("name", "url", "one_liner"))
    parsed = urlparse(url)
    if not name or not one_liner:
        raise ConfigError("gift_app.yaml 의 app.name·one_liner 가 비어 있습니다")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError(f"gift_app.yaml 의 app.url 은 https 주소여야 합니다: {url!r}")
    facts = tuple(str(f).strip() for f in app.get("facts") or [] if str(f).strip())
    if not facts or any(len(f) > MAX_FACT_CHARS for f in facts):
        raise ConfigError(f"gift_app.yaml 의 app.facts 는 {MAX_FACT_CHARS}자 이하 문장이 하나 이상 있어야 합니다")
    occasions = []
    for i, item in enumerate((raw or {}).get("occasions") or [], start=1):
        theme = str((item or {}).get("theme") or "").strip()
        if not theme or len(theme) > 40:
            raise ConfigError(f"gift_app.yaml occasions {i}번째 theme 은 40자 이하로 적어야 합니다")
        season = item.get("season")
        if season is not None:
            if not isinstance(season, list) or len(season) != 2:
                raise ConfigError(f"gift_app.yaml occasions {i}번째 season 은 [시작, 끝] 두 날짜여야 합니다")
            season = (_mmdd(season[0], f"occasions {i}"), _mmdd(season[1], f"occasions {i}"))
        occasions.append(Occasion(theme, season))
    if not any(o.season is None for o in occasions):
        raise ConfigError("gift_app.yaml occasions 에 season 이 없는(언제나 쓰는) 주제가 하나 이상 있어야 합니다")
    if len({o.theme for o in occasions}) != len(occasions):
        raise ConfigError("gift_app.yaml occasions 의 theme 이 겹칩니다")
    return GiftApp(name, url, one_liner, facts, tuple(occasions))


def load_gift_app(path: Path | None = None) -> GiftApp:
    path = path or GIFT_PATH
    if not Path(path).is_file():
        raise ConfigError(f"gift 글을 쓰려면 {path} 가 있어야 합니다")
    return parse_gift_app(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def is_gift_day(day: date) -> bool:
    return day.weekday() in GIFT_WEEKDAYS


def themes_for(app: GiftApp, day: date | None) -> list[str]:
    """그날 쓸 수 있는 주제: 시즌 중인 주제 + 상시 주제 (파일 순서)."""
    return [o.theme for o in app.occasions if day is None or o.in_season(day)]


def build_payload(app: GiftApp, theme: str) -> dict:
    """생성기·가드·검수자가 함께 보는 calc_payload. 글의 사실은 app.facts 에 있는 것만 쓴다."""
    return {"app": {"name": app.name, "one_liner": app.one_liner, "facts": list(app.facts),
                    "maker": "이 계정 주인이 직접 만든 앱"},
            "occasion": theme}


def review_rows(app: GiftApp) -> list[dict]:
    """검수자에게 넘길 '연결된 상품 행' 자리. 앱 자체가 링크 대상이다."""
    return [{"name": app.name, "category": "app", "power_w": None, "note": app.one_liner, "link": app.url}]


def link_problem(url: str, app: GiftApp) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https":
        return f"링크가 https 가 아닙니다: {url}"
    if (parsed.hostname or "").lower() != app.host:
        return f"링크가 gift_app.yaml 에 등록한 내 앱 주소({app.host})가 아닙니다: {parsed.hostname}"
    return None
