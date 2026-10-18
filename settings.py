"""
설정·시크릿 로딩 (PLAN Step 1).

- .env: 토큰·API 키. 이미 설정된 환경변수(GitHub Secrets 등)가 .env 보다 우선한다.
- config.yaml: 발행 규칙·슬롯·금지어·대가성 문구·판정 기준.

여기서 강제하는 PLAN(v2) 규칙
  규칙 1  THREADS_USER_ID 는 숫자 ID 하나만 받는다. 여러 계정을 넣으면 로드 실패.
  규칙 3  대가성 문구가 비었거나 필수 문장이 빠지면 로드 실패.
  규칙 5  하루 최대 5개·간격 최소 2시간은 코드 상수다. config 가 이보다 느슨하면 로드 실패.
  규칙 7  필수 금지어·금지 훅 문구는 코드에 고정돼 있어 config 에서 빼도 적용된다.
  규칙 8  AUTO_PUBLISH_DISABLED 해석 (auto_publish_disabled).
  규칙 9  DRY_RUN 은 값이 정확히 false 일 때만 꺼진다 (dry_run). 시험지 합격 확인은 발행기가 한다.
  Step 1  생성 모델(대체 모델 포함)과 검수 모델의 제공사가 같으면 로드 실패.

설정 점검 (시크릿 값은 출력하지 않음):
  python settings.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import MISSING, dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """설정·시크릿 값이 규칙을 어긴다. 발행 작업은 이 예외가 나면 시작하지 않는다."""


# ── 계정 (PLAN 규칙 1, 2026-10-18 개정) ─────────────────────────────────
# 한 번의 실행은 계정 하나만 다룬다. ACCOUNT 가 비어 있으면 기존 계정(프로젝트 루트의 config.yaml·data·prompts)이다.
# ACCOUNT=sabolkka 면 accounts/sabolkka/config.yaml 이 반드시 있어야 하고, data·prompts 는 같은 폴더에 있는 파일만
# 기존 것 대신 쓴다. ACCOUNT 는 .env 가 아니라 실제 환경변수로만 받는다 (설정 파일을 고르는 값이라 .env 보다 먼저 필요하다).
ACCOUNT_NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,30}")


def _account_from_env() -> str:
    name = (os.environ.get("ACCOUNT") or "").strip()
    if name and not ACCOUNT_NAME_RE.fullmatch(name):
        raise ConfigError(f"ACCOUNT 이름 형식 오류: 소문자·숫자·밑줄만, 소문자로 시작 ({name!r})")
    return name


ACCOUNT = _account_from_env()
ACCOUNT_DIR = PROJECT_ROOT / "accounts" / ACCOUNT if ACCOUNT else PROJECT_ROOT


def account_path(*parts: str) -> Path:
    """계정 폴더에 같은 이름의 파일이 있으면 그 파일, 없으면 프로젝트 루트의 파일."""
    if ACCOUNT and (ACCOUNT_DIR.joinpath(*parts)).exists():
        return ACCOUNT_DIR.joinpath(*parts)
    return PROJECT_ROOT.joinpath(*parts)


def account_label() -> str:
    """알림 맨 앞에 붙일 표시. 기존 계정은 비어 있어 메시지가 그대로다."""
    return f"[{ACCOUNT}] " if ACCOUNT else ""


# config.yaml 은 계정 간 폴백 없이 계정마다 따로 둔다 (다른 계정 설정으로 조용히 도는 일을 막는다).
CONFIG_PATH = ACCOUNT_DIR / "config.yaml"
# 계정 전용 .env.<계정> 이 있으면 그것을 먼저, 이어서 공용 .env 를 읽는다 (이미 있는 값은 덮어쓰지 않는다).
ENV_PATH = PROJECT_ROOT / (f".env.{ACCOUNT}" if ACCOUNT else ".env")
SHARED_ENV_PATH = PROJECT_ROOT / ".env"

# 한국은 서머타임이 없어 고정 오프셋으로 충분하다 (Windows 에는 IANA 시간대 DB가 기본으로 없다).
KST = timezone(timedelta(hours=9), "KST")

# ── 코드에 고정한 한도 (config 로 완화할 수 없음) ───────────────────────
HARD_MAX_POSTS_PER_DAY = 6      # 규칙 5 (2026-10-17 사용자 결정으로 5 → 6)
HARD_MIN_GAP_HOURS = 2          # 규칙 5
TONES = ("polite", "casual")
MAX_REPLY_DELAY_HOURS = 12      # 답글이 너무 늦으면 글과 링크가 따로 논다 (재시도 주기 30분 포함)
THREADS_MAX_CHARS = 500         # Threads API 텍스트 한도
THREADS_MAX_LINKS = 5           # Threads API 글당 고유 링크 한도
TOKEN_LIFETIME_DAYS = 60        # 장기 토큰 유효기간 (THREADS_API_NOTES.md)
TOKEN_WARN_DAYS = 10            # 만료 10일 전부터 경고

# 규칙 7과 PLAN Step 4 의 금지어. config 의 banned_words 와 합쳐 쓴다.
REQUIRED_BANNED_WORDS = (
    "판매가", "최저가", "할인", "특가", "역대", "무조건", "100%", "보장", "최고", "1위", "품절 임박",
    # 사용 경험 표현
    "써봤", "사용해보니", "써보니", "우리 집",
    # 안전·건강 표현
    "안전", "화재", "무해", "전자파", "숙면", "건강에",
)
# PLAN Step 3 금지 훅 문구. config 의 banned_hook_phrases 와 합쳐 쓴다.
REQUIRED_BANNED_HOOK_PHRASES = (
    "나만 알고", "100명 중", "저장 안 하면", "절대 안 함", "이거 모르면 손해", "충격", "실화", "역대급",
)
# 모델 이름 앞부분 → 제공사. 생성·검수 모델의 회사가 같은지 판단하는 데 쓴다.
MODEL_PROVIDERS = (
    ("gemini", "google"),
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
)
# 글 유형 이름 (generator.POST_TYPES 와 같아야 한다)
POST_TYPE_NAMES = ("calc", "compare", "pick", "bundle", "talk", "tip")
# 쿠팡 파트너스 대가성 문구에서 빠지면 안 되는 문장
DISCLOSURE_REQUIRED_PHRASES = ("쿠팡 파트너스 활동의 일환으로", "수수료를 제공받습니다")

log = logging.getLogger("threads_coupas")


# ── 환경변수 ─────────────────────────────────────────────────────────

_dotenv_loaded = False


def load_env_file(path: Path = ENV_PATH) -> bool:
    """.env 를 환경변수로 읽는다. 이미 설정된 변수는 덮어쓰지 않는다."""
    if not Path(path).is_file():
        return False
    from dotenv import load_dotenv
    return load_dotenv(path, override=False)


# 계정마다 달라야 하는 값. 다른 계정 실행이 공용 .env 의 이 값을 물려받으면 엉뚱한 계정으로 발행되므로 읽지 않는다.
ACCOUNT_ONLY_KEYS = frozenset({
    "THREADS_USER_ID", "THREADS_ACCESS_TOKEN", "THREADS_TOKEN_ISSUED_AT",
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GH_DISPATCH_TOKEN",
    "DRY_RUN", "AUTO_PUBLISH_DISABLED",
})


def load_shared_env_file(path: Path = SHARED_ENV_PATH) -> bool:
    """공용 .env 에서 계정 전용 값을 뺀 나머지(LLM 키, 텔레그램, 앱 시크릿)만 읽는다. 이미 있는 값은 덮어쓰지 않는다."""
    if not Path(path).is_file():
        return False
    from dotenv import dotenv_values
    for key, value in dotenv_values(path).items():
        if key not in ACCOUNT_ONLY_KEYS and value is not None:
            os.environ.setdefault(key, value)
    return True


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """env 를 넘기면 그대로 쓰고(테스트용), 아니면 프로젝트 .env 를 한 번 읽은 뒤 os.environ 을 쓴다."""
    global _dotenv_loaded
    if env is not None:
        return env
    if not _dotenv_loaded:
        load_env_file()
        if SHARED_ENV_PATH != ENV_PATH:
            load_shared_env_file(SHARED_ENV_PATH)
        _dotenv_loaded = True
    return os.environ


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def dry_run(env: Mapping[str, str] | None = None) -> bool:
    """규칙 9. 값이 정확히 'false'(대소문자 무시)일 때만 False. 비었거나 오타가 있어도 True 로 남는다."""
    return _get(_environ(env), "DRY_RUN").lower() != "false"


def auto_publish_disabled(env: Mapping[str, str] | None = None) -> bool:
    """규칙 8. 켜져 있으면 발행 작업이 아무것도 하지 않고 정상 종료한다.
    빈 값과 0/false/no/off 만 꺼짐이다. 알 수 없는 값(오타 포함)은 켜짐으로 본다."""
    return _get(_environ(env), "AUTO_PUBLISH_DISABLED").lower() not in ("", "0", "false", "no", "off")


def today_kst() -> date:
    return datetime.now(KST).date()


def _parse_date(value, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if len(text) > 10:
            dt = datetime.fromisoformat(text)
            return (dt.astimezone(KST) if dt.tzinfo else dt).date()
        return date.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{name} 은 YYYY-MM-DD 형식이어야 합니다: {value!r}") from None


# ── 시크릿 ───────────────────────────────────────────────────────────

_SECRET_ENV = {
    "threads_user_id": "THREADS_USER_ID",
    "threads_access_token": "THREADS_ACCESS_TOKEN",
    "threads_app_secret": "THREADS_APP_SECRET",
    "threads_token_issued_at": "THREADS_TOKEN_ISSUED_AT",
    "gemini_api_key": "GEMINI_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "supabase_url": "SUPABASE_URL",
    "supabase_service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}
_OPTIONAL_SECRETS = {"openai_api_key"}


@dataclass(frozen=True)
class Secrets:
    # repr=False: 예외 메시지나 로그에 토큰이 찍히지 않게 한다.
    threads_user_id: str | None = None
    threads_access_token: str | None = field(default=None, repr=False)
    threads_app_secret: str | None = field(default=None, repr=False)
    threads_token_issued_at: date | None = None
    gemini_api_key: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    anthropic_api_key: str | None = field(default=None, repr=False)
    supabase_url: str | None = None
    supabase_service_role_key: str | None = field(default=None, repr=False)
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None

    def api_key_for(self, provider: str) -> str | None:
        return {"google": self.gemini_api_key, "openai": self.openai_api_key,
                "anthropic": self.anthropic_api_key}.get(provider)

    def missing(self, *names: str) -> list[str]:
        """비어 있는 항목의 환경변수 이름. names 를 주지 않으면 선택 항목을 뺀 전체를 본다."""
        names = names or tuple(n for n in _SECRET_ENV if n not in _OPTIONAL_SECRETS)
        return [_SECRET_ENV[n] for n in names if getattr(self, n) is None]

    def require(self, *names: str) -> None:
        missing = self.missing(*names)
        if missing:
            raise ConfigError("환경변수가 비어 있습니다: " + ", ".join(missing))


def load_secrets(env: Mapping[str, str] | None = None) -> Secrets:
    e = _environ(env)
    values: dict = {attr: _get(e, name) or None for attr, name in _SECRET_ENV.items()}

    user_id = values["threads_user_id"]
    if user_id is not None and not re.fullmatch(r"\d+", user_id):
        raise ConfigError("규칙 1: THREADS_USER_ID 에는 숫자로 된 스레드 사용자 ID 하나만 넣습니다 (여러 계정 불가).")

    issued = values["threads_token_issued_at"]
    values["threads_token_issued_at"] = _parse_date(issued, "THREADS_TOKEN_ISSUED_AT") if issued else None
    if values["supabase_url"]:
        values["supabase_url"] = normalize_supabase_url(values["supabase_url"])
    return Secrets(**values)


def normalize_supabase_url(url: str) -> str:
    """https://<ref>.supabase.co 형태로 맞춘다. 대시보드의 Data API URL(…/rest/v1/)을 그대로 복사한 경우
    라이브러리가 /rest/v1 을 한 번 더 붙여 PGRST125(Invalid path) 오류가 나므로 떼어 낸다."""
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    if path in ("", "/rest/v1"):
        path = ""
    if parsed.scheme != "https" or not parsed.hostname or path or parsed.query:
        raise ConfigError("SUPABASE_URL 은 https://<프로젝트>.supabase.co 형태여야 합니다 (뒤에 경로 없이)")
    return f"https://{parsed.netloc}"


# ── 토큰 만료 경고 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenStatus:
    level: str                      # ok | warn | expired | unknown
    message: str
    expires_on: date | None = None
    days_left: int | None = None


def token_status(issued_at: date | None, today: date | None = None) -> TokenStatus:
    """장기 토큰(60일) 만료까지 남은 날. 10일 이하면 warn, 0일 이하면 expired.
    발급 시각을 모르므로 만료일 당일은 이미 만료된 것으로 본다."""
    if issued_at is None:
        return TokenStatus("unknown", "THREADS_TOKEN_ISSUED_AT 이 비어 있어 토큰 만료일을 확인할 수 없습니다.")
    today = today or today_kst()
    expires_on = issued_at + timedelta(days=TOKEN_LIFETIME_DAYS)
    days_left = (expires_on - today).days
    if issued_at > today:
        return TokenStatus(
            "unknown",
            f"토큰 발급일({issued_at})이 오늘({today})보다 뒤입니다. THREADS_TOKEN_ISSUED_AT 을 확인하세요.",
            expires_on, days_left,
        )
    if days_left <= 0:
        return TokenStatus(
            "expired",
            f"스레드 토큰이 만료됐습니다 (만료일 {expires_on}). 새 장기 토큰을 발급하고 THREADS_TOKEN_ISSUED_AT 을 바꾸세요.",
            expires_on, days_left,
        )
    if days_left <= TOKEN_WARN_DAYS:
        return TokenStatus(
            "warn",
            f"스레드 토큰 만료 {days_left}일 전입니다 (만료일 {expires_on}). 토큰을 갱신하고 THREADS_TOKEN_ISSUED_AT 을 바꾸세요.",
            expires_on, days_left,
        )
    return TokenStatus("ok", f"스레드 토큰 만료까지 {days_left}일 남았습니다 (만료일 {expires_on}).", expires_on, days_left)


_TOKEN_LOG_LEVEL = {"ok": logging.INFO, "warn": logging.WARNING, "unknown": logging.WARNING, "expired": logging.ERROR}


def log_token_status(issued_at: date | None, logger: logging.Logger = log, today: date | None = None) -> TokenStatus:
    """발행 로그에 토큰 상태를 남긴다. 발행기·지표 수집기가 시작할 때 부른다."""
    status = token_status(issued_at, today)
    logger.log(_TOKEN_LOG_LEVEL[status.level], status.message)
    return status


# ── config.yaml ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limits:
    max_chars: int
    max_body_chars: int
    max_urls: int


@dataclass(frozen=True)
class Criteria:
    week2_min_median_views: int
    week2_min_coupang_clicks: int
    week4_min_commission_won: int
    week4_min_orders: int


@dataclass(frozen=True)
class Config:
    max_posts_per_day: int
    min_gap_hours: float
    slots_kst: tuple[time, ...]
    max_link_post_ratio: float
    generator_model: str
    generator_fallback_model: str | None
    reviewer_model: str
    limits: Limits
    disclosure_text: str
    banned_words: tuple[str, ...]
    banned_hook_phrases: tuple[str, ...]
    criteria: Criteria
    digest_time_kst: time
    test_start_date: date | None
    # 생성할 글 유형 (없으면 전부). 2026-10-16: 팁·계산·비교 글이 재미없다는 판단으로 pick 만 남김
    post_types: tuple[str, ...] = ("calc", "compare", "pick", "tip")
    # 말투: polite(해요체, 기본) 또는 casual(친근한 반말). 생성 프롬프트는 계정 폴더 prompts/ 에서, 검수 R11 은 이 값으로 정한다
    tone: str = "polite"
    # 참여형(talk) 글 주제. 비우면 generator.TALK_TOPICS(겨울 난방 주제)를 쓴다
    talk_topics: tuple[str, ...] = ()
    # 링크 답글을 본문 발행 후 몇 시간 뒤에 달지 (0 이면 바로). "사라 vs 마라" 글 뒤에 결과·링크를 늦게 다는 방식
    reply_delay_hours: float = 0.0


def provider_of(model: str) -> str:
    """모델 이름으로 제공사를 정한다. 모르는 모델은 회사 비교를 할 수 없으므로 거부한다."""
    name = model.strip().lower()
    for prefix, provider in MODEL_PROVIDERS:
        if name.startswith(prefix):
            return provider
    raise ConfigError(f"제공사를 알 수 없는 모델입니다: {model!r} (gemini-*, claude-*, gpt-* 등만 지원)")


def _parse_models(raw) -> tuple[str, str | None, str]:
    names = {}
    for key in ("generator_model", "generator_fallback_model", "reviewer_model"):
        value = raw[key]
        if value is None and key == "generator_fallback_model":
            names[key] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} 에 모델 이름을 넣어야 합니다.")
        names[key] = value.strip()

    reviewer = provider_of(names["reviewer_model"])
    for key in ("generator_model", "generator_fallback_model"):
        if names[key] and provider_of(names[key]) == reviewer:
            raise ConfigError(
                f"생성 모델과 검수 모델의 제공사가 같습니다 ({key}={names[key]}, reviewer_model={names['reviewer_model']}). "
                "검수는 다른 회사의 모델로 해야 합니다."
            )
    if names["generator_fallback_model"] and provider_of(names["generator_fallback_model"]) == provider_of(names["generator_model"]):
        raise ConfigError("generator_fallback_model 은 generator_model 과 다른 회사여야 합니다 (한도 초과 대비용).")
    return names["generator_model"], names["generator_fallback_model"], names["reviewer_model"]


def load_config(path: Path = CONFIG_PATH) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml 문법 오류: {e}") from None
    return parse_config(raw)


def _check_keys(raw, cls, where: str) -> None:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 는 '키: 값' 목록이어야 합니다.")
    expected = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - expected)
    if unknown:
        raise ConfigError(f"{where} 에 모르는 키가 있습니다 (오타 확인): " + ", ".join(map(str, unknown)))
    optional = {f.name for f in fields(cls) if f.default is not MISSING}
    missing = sorted(expected - optional - set(raw))
    if missing:
        raise ConfigError(f"{where} 에 빠진 키가 있습니다: " + ", ".join(missing))


def _int(value, name: str, lo: int, hi: int | None = None) -> int:
    # bool 은 int 의 하위 타입이라 따로 막는다 (true 가 1로 읽히지 않게).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 은 정수여야 합니다: {value!r}")
    if value < lo or (hi is not None and value > hi):
        allowed = f"{lo} 이상" if hi is None else f"{lo}~{hi}"
        raise ConfigError(f"{name} 은 {allowed} 이어야 합니다: {value}")
    return value


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 은 숫자여야 합니다: {value!r}")
    return float(value)


def _hm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_slots(value, min_gap_hours: float) -> tuple[time, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError('slots_kst 는 "HH:MM" 문자열 목록이어야 합니다.')
    slots = [_parse_hhmm(v, "slots_kst") for v in value]

    minutes = [t.hour * 60 + t.minute for t in slots]
    if minutes != sorted(set(minutes)):
        raise ConfigError("slots_kst 는 중복 없이 이른 시각부터 적어야 합니다.")
    # 마지막 슬롯 → 다음 날 첫 슬롯까지 포함해 모든 간격을 본다.
    for a, b in zip(minutes, minutes[1:] + [minutes[0] + 24 * 60]):
        if b - a < min_gap_hours * 60:
            raise ConfigError(f"규칙 5: 슬롯 간격이 {min_gap_hours:g}시간보다 짧습니다: {_hm(a)} → {_hm(b)}")
    return tuple(slots)


def _parse_disclosure(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("규칙 3: disclosure_text 가 비어 있습니다.")
    if value != value.strip() or "\n" in value:
        raise ConfigError("규칙 3: disclosure_text 는 앞뒤 공백 없는 한 줄이어야 합니다 (본문 첫 줄과 그대로 비교합니다).")
    lacking = [p for p in DISCLOSURE_REQUIRED_PHRASES if p not in value]
    if lacking:
        raise ConfigError("규칙 3: disclosure_text 에 필수 문장이 빠졌습니다: " + ", ".join(lacking))
    return value


def _parse_word_list(value, name: str, required: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(w, str) or not w.strip() for w in value):
        raise ConfigError(f"{name} 는 비어 있지 않은 문자열 목록이어야 합니다.")
    return tuple(dict.fromkeys([*required, *(w.strip() for w in value)]))


def _parse_hhmm(value, name: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ConfigError(f'{name} 은 따옴표로 감싼 "HH:MM" 이어야 합니다: {value!r}')
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"{name} 에 없는 시각이 있습니다: {value!r}") from None


def parse_config(raw) -> Config:
    _check_keys(raw, Config, "config.yaml")

    max_posts = raw["max_posts_per_day"]
    if isinstance(max_posts, bool) or not isinstance(max_posts, int):
        raise ConfigError(f"max_posts_per_day 는 정수여야 합니다: {max_posts!r}")
    if not 1 <= max_posts <= HARD_MAX_POSTS_PER_DAY:
        raise ConfigError(f"규칙 5: max_posts_per_day 는 1~{HARD_MAX_POSTS_PER_DAY} 이어야 합니다: {max_posts}")

    min_gap = _number(raw["min_gap_hours"], "min_gap_hours")
    if min_gap < HARD_MIN_GAP_HOURS:
        raise ConfigError(f"규칙 5: min_gap_hours 는 {HARD_MIN_GAP_HOURS} 이상이어야 합니다: {min_gap:g}")

    ratio = _number(raw["max_link_post_ratio"], "max_link_post_ratio")
    if not 0 < ratio <= 1:
        raise ConfigError(f"max_link_post_ratio 는 0보다 크고 1 이하여야 합니다: {ratio:g}")

    generator_model, fallback_model, reviewer_model = _parse_models(raw)

    disclosure = _parse_disclosure(raw["disclosure_text"])
    banned = _parse_word_list(raw["banned_words"], "banned_words", REQUIRED_BANNED_WORDS)
    banned_hooks = _parse_word_list(raw["banned_hook_phrases"], "banned_hook_phrases", REQUIRED_BANNED_HOOK_PHRASES)
    in_disclosure = [w for w in banned + banned_hooks if w in disclosure]
    if in_disclosure:
        raise ConfigError("disclosure_text 에 금지어가 들어 있어 링크 글이 모두 막힙니다: " + ", ".join(in_disclosure))

    _check_keys(raw["limits"], Limits, "limits")
    max_chars = _int(raw["limits"]["max_chars"], "limits.max_chars", 1, THREADS_MAX_CHARS)
    # 대가성 문구 + 빈 줄 + 본문이 전체 한도 안에 들어가야 한다.
    body_cap = max_chars - len(disclosure) - 2
    limits = Limits(
        max_chars=max_chars,
        max_body_chars=_int(raw["limits"]["max_body_chars"], "limits.max_body_chars", 1, body_cap),
        max_urls=_int(raw["limits"]["max_urls"], "limits.max_urls", 0, THREADS_MAX_LINKS),
    )

    _check_keys(raw["criteria"], Criteria, "criteria")
    criteria = Criteria(**{k: _int(v, f"criteria.{k}", 0) for k, v in raw["criteria"].items()})

    start = raw["test_start_date"]
    types = raw.get("post_types", Config.post_types)
    if (not isinstance(types, list | tuple) or not types
            or any(t not in POST_TYPE_NAMES for t in types)):
        raise ConfigError(f"post_types 는 {', '.join(POST_TYPE_NAMES)} 중 하나 이상의 목록이어야 합니다: {types!r}")
    tone = raw.get("tone", Config.tone)
    if tone not in TONES:
        raise ConfigError(f"tone 은 {', '.join(TONES)} 중 하나여야 합니다: {tone!r}")
    topics = raw.get("talk_topics") or []
    if (not isinstance(topics, list | tuple) or any(not isinstance(t, str) or not t.strip() or len(t) > 60 for t in topics)
            or len(set(topics)) != len(topics)):
        raise ConfigError("talk_topics 는 겹치지 않는 60자 이하 문장의 목록이어야 합니다.")
    delay = raw.get("reply_delay_hours", Config.reply_delay_hours)
    if isinstance(delay, bool) or not isinstance(delay, int | float) or not 0 <= delay <= MAX_REPLY_DELAY_HOURS:
        raise ConfigError(f"reply_delay_hours 는 0~{MAX_REPLY_DELAY_HOURS} 시간 사이 숫자여야 합니다: {delay!r}")
    return Config(
        max_posts_per_day=max_posts,
        min_gap_hours=min_gap,
        slots_kst=_parse_slots(raw["slots_kst"], min_gap),
        max_link_post_ratio=ratio,
        generator_model=generator_model,
        generator_fallback_model=fallback_model,
        reviewer_model=reviewer_model,
        limits=limits,
        disclosure_text=disclosure,
        banned_words=banned,
        banned_hook_phrases=banned_hooks,
        criteria=criteria,
        digest_time_kst=_parse_hhmm(raw["digest_time_kst"], "digest_time_kst"),
        test_start_date=None if start is None else _parse_date(start, "test_start_date"),
        post_types=tuple(types),
        tone=tone,
        talk_topics=tuple(t.strip() for t in topics),
        reply_delay_hours=float(delay),
    )


# ── 점검 명령 ─────────────────────────────────────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 1

    print("config.yaml: OK")
    print(f"  하루 최대 {cfg.max_posts_per_day}개 (코드 상한 {HARD_MAX_POSTS_PER_DAY}), 간격 {cfg.min_gap_hours:g}시간 이상")
    print(f"  슬롯(KST): {' '.join(t.strftime('%H:%M') for t in cfg.slots_kst)}")
    print(f"  링크 글 비율 상한 {cfg.max_link_post_ratio:.0%}, 금지어 {len(cfg.banned_words)}개, 금지 훅 문구 {len(cfg.banned_hook_phrases)}개")
    print(f"  생성 {cfg.generator_model} (대체 {cfg.generator_fallback_model or '없음'}) / 검수 {cfg.reviewer_model}")
    print(f"DRY_RUN: {'true' if dry_run() else 'false'}")
    print(f"AUTO_PUBLISH_DISABLED: {'true' if auto_publish_disabled() else 'false'}")
    missing = secrets.missing()
    print("시크릿: " + ("모두 설정됨" if not missing else "비어 있음 → " + ", ".join(missing)), flush=True)
    log_token_status(secrets.threads_token_issued_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
