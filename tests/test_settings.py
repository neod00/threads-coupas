"""
Step 1 설정·시크릿 테스트.

8장 체크리스트
  - 토큰 만료 경고 로직 동작 (발급일을 과거로 바꿔 테스트)
  - 생성 모델과 검수 모델의 제공사가 같으면 실행 거부
그 밖에: 규칙 1(계정 1개), 규칙 3·5·7 설정 검증, 규칙 8 킬 스위치, 규칙 9 DRY_RUN 기본값
"""
import logging
import os
from datetime import date, timedelta

import pytest
import yaml

import settings
from settings import ConfigError

TODAY = date(2026, 10, 1)


# ── 8장: 토큰 만료 경고 ─────────────────────────────────────────────────

@pytest.mark.parametrize("days_ago, level", [
    (0, "ok"),
    (49, "ok"),         # 11일 남음
    (50, "warn"),       # 10일 남음 → 여기서부터 경고
    (59, "warn"),       # 1일 남음
    (60, "expired"),    # 만료일 당일
    (75, "expired"),
])
def test_token_status_by_issued_date(days_ago, level):
    status = settings.token_status(TODAY - timedelta(days=days_ago), today=TODAY)
    assert status.level == level
    assert status.days_left == 60 - days_ago


def test_warning_logged_when_issued_date_moved_to_past(caplog):
    # .env 의 발급일을 55일 전으로 바꾼 상황: 로드 → 발행 로그에 WARNING 1건
    issued = settings.today_kst() - timedelta(days=55)
    secrets = settings.load_secrets({"THREADS_TOKEN_ISSUED_AT": issued.isoformat()})
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        status = settings.log_token_status(secrets.threads_token_issued_at)
    assert status.level == "warn"
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "만료 5일 전" in caplog.text


def test_no_warning_for_fresh_token(caplog):
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        settings.log_token_status(TODAY - timedelta(days=10), today=TODAY)
    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_expired_token_logged_as_error(caplog):
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        settings.log_token_status(TODAY - timedelta(days=61), today=TODAY)
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "만료됐습니다" in caplog.text


def test_missing_or_future_issued_date_warns(caplog):
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        assert settings.log_token_status(None, today=TODAY).level == "unknown"
        assert settings.log_token_status(TODAY + timedelta(days=1), today=TODAY).level == "unknown"
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]


@pytest.mark.parametrize("value, expected", [
    ("2026-08-01", date(2026, 8, 1)),
    ("2026-08-01T23:30:00+00:00", date(2026, 8, 2)),   # UTC 23:30 = KST 다음 날 08:30
])
def test_issued_date_formats(value, expected):
    assert settings.load_secrets({"THREADS_TOKEN_ISSUED_AT": value}).threads_token_issued_at == expected


def test_bad_issued_date_rejected():
    with pytest.raises(ConfigError, match="YYYY-MM-DD"):
        settings.load_secrets({"THREADS_TOKEN_ISSUED_AT": "2026/08/01"})


# ── 규칙 9: DRY_RUN ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "true", "TRUE", "1", "0", "no", "off", "flase", "false_"])
def test_dry_run_stays_on_unless_exactly_false(value):
    env = {} if value is None else {"DRY_RUN": value}
    assert settings.dry_run(env) is True


@pytest.mark.parametrize("value", ["false", "False", " FALSE "])
def test_dry_run_off_only_with_false(value):
    assert settings.dry_run({"DRY_RUN": value}) is False


def test_env_example_keeps_dry_run_true():
    example = (settings.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DRY_RUN=true" in example.splitlines()


# ── 규칙 8: 킬 스위치 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value, disabled", [
    (None, False), ("", False), ("false", False), ("0", False), ("no", False), ("off", False),
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("ture", True),     # 오타도 켜짐으로 본다
])
def test_kill_switch_parsing(value, disabled):
    env = {} if value is None else {"AUTO_PUBLISH_DISABLED": value}
    assert settings.auto_publish_disabled(env) is disabled


def _isolate_env(monkeypatch, *names):
    # setenv 후 delenv: 테스트가 끝나면 원래 상태(없음)로 되돌아가 .env 값이 다른 테스트로 새지 않는다.
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def test_kill_switch_in_env_file_is_honored(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, "AUTO_PUBLISH_DISABLED", "DRY_RUN")
    env_file = tmp_path / ".env"
    env_file.write_text("AUTO_PUBLISH_DISABLED=true\nDRY_RUN=true\n", encoding="utf-8")
    assert settings.load_env_file(env_file)
    assert settings.auto_publish_disabled(os.environ) is True


def test_existing_env_wins_over_env_file(tmp_path, monkeypatch):
    # GitHub Variables 로 켠 킬 스위치를 .env 가 끄지 못한다.
    monkeypatch.setenv("AUTO_PUBLISH_DISABLED", "true")
    env_file = tmp_path / ".env"
    env_file.write_text("AUTO_PUBLISH_DISABLED=false\n", encoding="utf-8")
    settings.load_env_file(env_file)
    assert settings.auto_publish_disabled(os.environ) is True


# ── 규칙 1: 계정 1개 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["123,456", "123 456", "123;456", "abc", "12a"])
def test_multiple_or_invalid_user_ids_rejected(value):
    with pytest.raises(ConfigError, match="규칙 1"):
        settings.load_secrets({"THREADS_USER_ID": value})


def test_single_numeric_user_id_accepted():
    assert settings.load_secrets({"THREADS_USER_ID": " 17841400000000000 "}).threads_user_id == "17841400000000000"


# ── 시크릿 ───────────────────────────────────────────────────────────────

def test_secrets_repr_hides_values():
    s = settings.load_secrets({
        "THREADS_ACCESS_TOKEN": "tok-SECRET",
        "THREADS_APP_SECRET": "app-SECRET",
        "GEMINI_API_KEY": "gem-SECRET",
        "OPENAI_API_KEY": "oai-SECRET",
        "ANTHROPIC_API_KEY": "ant-SECRET",
        "SUPABASE_SERVICE_ROLE_KEY": "sb-SECRET",
        "TELEGRAM_BOT_TOKEN": "tg-SECRET",
    })
    assert "SECRET" not in repr(s)


def test_require_names_only_missing_env_vars():
    s = settings.load_secrets({"THREADS_USER_ID": "1", "THREADS_ACCESS_TOKEN": "  "})
    with pytest.raises(ConfigError) as e:
        s.require("threads_user_id", "threads_access_token")
    assert "THREADS_ACCESS_TOKEN" in str(e.value)
    assert "THREADS_USER_ID" not in str(e.value)


@pytest.mark.parametrize("url", [
    "https://abcdefghijklmnopqrst.supabase.co",
    "https://abcdefghijklmnopqrst.supabase.co/",
    "https://abcdefghijklmnopqrst.supabase.co/rest/v1/",      # 대시보드 Data API URL 을 그대로 복사한 경우
    " https://abcdefghijklmnopqrst.supabase.co/rest/v1 ",
])
def test_supabase_url_normalized(url):
    assert settings.load_secrets({"SUPABASE_URL": url}).supabase_url == "https://abcdefghijklmnopqrst.supabase.co"


@pytest.mark.parametrize("url", [
    "http://abcdefghijklmnopqrst.supabase.co",
    "https://abcdefghijklmnopqrst.supabase.co/project/default",
    "abcdefghijklmnopqrst.supabase.co",
])
def test_bad_supabase_url_rejected(url):
    with pytest.raises(ConfigError, match="SUPABASE_URL"):
        settings.load_secrets({"SUPABASE_URL": url})


def test_openai_key_is_optional():
    assert "OPENAI_API_KEY" not in settings.load_secrets({}).missing()


# ── config.yaml ─────────────────────────────────────────────────────────

@pytest.fixture
def raw():
    from conftest import coupang_raw
    return coupang_raw()


def test_project_config_matches_plan():
    # 쿠팡 계정 기준 설정 (openbrain 은 2026-10-30 앱 홍보 계정이 됐다)
    from conftest import coupang_config
    cfg = coupang_config()
    assert cfg.max_posts_per_day == 6                      # 2026-10-17 사용자 결정 (규칙 5 개정)
    assert cfg.min_gap_hours == 2
    assert [t.strftime("%H:%M") for t in cfg.slots_kst] == ["08:10", "10:30", "13:00", "16:00", "19:00", "21:30"]
    assert cfg.max_link_post_ratio == 0.6
    assert cfg.disclosure_text == (
        "[광고] 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )
    assert cfg.limits == settings.Limits(max_chars=500, max_body_chars=350, max_urls=1)
    assert cfg.criteria == settings.Criteria(300, 30, 30000, 5)
    assert set(settings.REQUIRED_BANNED_WORDS) <= set(cfg.banned_words)
    assert set(settings.REQUIRED_BANNED_HOOK_PHRASES) <= set(cfg.banned_hook_phrases)
    assert (cfg.generator_model, cfg.generator_fallback_model, cfg.reviewer_model) == (
        "gemini-3.8-flash", None, "gpt-5.4-mini")
    assert cfg.digest_time_kst.strftime("%H:%M") == "21:00"
    assert cfg.test_start_date == date(2026, 10, 18)     # 첫 실제 발행일


# ── 8장: 생성 모델과 검수 모델의 제공사가 같으면 실행 거부 ─────────────────────

@pytest.mark.parametrize("changes", [
    {"generator_model": "claude-sonnet-5", "reviewer_model": "claude-opus-5"},     # 생성도 검수도 Anthropic
    {"reviewer_model": "gemini-2.5-pro"},                                          # 검수도 Google
    {"generator_fallback_model": "gpt-4.1-mini", "reviewer_model": "gpt-4.1"},    # 대체 생성 모델과 검수가 같은 OpenAI
])
def test_same_provider_for_generator_and_reviewer_rejected(raw, changes):
    raw.update({"generator_model": "gemini-3.8-flash", "generator_fallback_model": None, **changes})
    with pytest.raises(ConfigError, match="제공사가 같습니다"):
        settings.parse_config(raw)


def test_same_provider_rejected_when_loading_config_file(tmp_path, raw):
    # 시작 시 config.yaml 을 읽는 모든 작업(생성·발행 등)이 거부된다
    raw["reviewer_model"] = "gemini-2.5-pro"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="제공사가 같습니다"):
        settings.load_config(path)


def test_different_providers_accepted(raw):
    raw.update(generator_model="gpt-4.1-mini", generator_fallback_model=None, reviewer_model="claude-opus-5")
    cfg = settings.parse_config(raw)
    assert settings.provider_of(cfg.generator_model) != settings.provider_of(cfg.reviewer_model)


@pytest.mark.parametrize("changes, message", [
    ({"reviewer_model": "llama-3"}, "제공사를 알 수 없는"),
    ({"generator_fallback_model": "gemini-2.5-pro"}, "다른 회사"),   # 대체 모델이 기본 생성 모델과 같은 회사
    ({"generator_model": ""}, "모델 이름"),
])
def test_invalid_model_config_rejected(raw, changes, message):
    raw.update(changes)
    with pytest.raises(ConfigError, match=message):
        settings.parse_config(raw)


def test_required_hook_phrases_cannot_be_removed(raw):
    raw["banned_hook_phrases"] = []
    assert set(settings.REQUIRED_BANNED_HOOK_PHRASES) <= set(settings.parse_config(raw).banned_hook_phrases)


@pytest.mark.parametrize("changes, message", [
    ({"max_posts_per_day": 7}, "규칙 5"),
    ({"max_posts_per_day": 0}, "규칙 5"),
    ({"max_posts_per_day": True}, "정수"),
    ({"min_gap_hours": 1.5}, "규칙 5"),
    ({"slots_kst": ["08:10", "09:30"]}, "규칙 5"),
    ({"slots_kst": ["00:30", "23:30"]}, "규칙 5"),          # 자정을 넘는 간격이 1시간
    ({"slots_kst": ["12:30", "08:10"]}, "이른 시각부터"),
    ({"slots_kst": [750]}, "따옴표"),                       # 따옴표 없는 12:30 을 YAML 이 750 으로 읽은 경우
    ({"slots_kst": ["25:00"]}, "없는 시각"),
    ({"max_link_post_ratio": 1.5}, "max_link_post_ratio"),
    ({"disclosure_text": ""}, "규칙 3"),
    ({"disclosure_text": "[광고] 수수료를 받습니다."}, "규칙 3"),
    ({"disclosure_text": "[광고] 이 포스팅은 쿠팡 파트너스 활동의 일환으로,\n수수료를 제공받습니다."}, "규칙 3"),
    ({"limits": {"max_chars": 501}}, "max_chars"),
    ({"limits": {"max_urls": 6}}, "max_urls"),
    ({"limits": {"max_body_chars": 446}}, "max_body_chars"),  # 500 - 문구 53자 - 빈 줄 2 = 445
    ({"criteria": {"week4_min_orders": -1}}, "week4_min_orders"),
    ({"max_post_per_day": 4}, "모르는 키"),                  # 오타 키
    ({"digest_time_kst": 2100}, "따옴표"),
])
def test_invalid_config_rejected(raw, changes, message):
    for key, value in changes.items():
        raw[key] = {**raw[key], **value} if isinstance(raw.get(key), dict) else value
    with pytest.raises(ConfigError, match=message):
        settings.parse_config(raw)


def test_missing_key_rejected(raw):
    del raw["criteria"]
    with pytest.raises(ConfigError, match="빠진 키"):
        settings.parse_config(raw)


def test_required_banned_words_cannot_be_removed(raw):
    raw["banned_words"] = ["가성비"]
    words = settings.parse_config(raw).banned_words
    assert set(settings.REQUIRED_BANNED_WORDS) <= set(words)
    assert "가성비" in words


def test_disclosure_with_banned_word_rejected(raw):
    raw["banned_words"] = ["수수료"]
    with pytest.raises(ConfigError, match="금지어"):
        settings.parse_config(raw)


def test_test_start_date_parsed(raw):
    raw["test_start_date"] = "2026-11-02"
    assert settings.parse_config(raw).test_start_date == date(2026, 11, 2)


# ── 계정별 설정: tone·talk_topics·reply_delay_hours (2026-10-18) ──────────────

def _raw_config():
    import yaml
    return yaml.safe_load(settings.PROJECT_ROOT.joinpath("config.yaml").read_text(encoding="utf-8"))


def test_account_options_default_to_existing_behaviour():
    cfg = settings.parse_config(_raw_config())
    assert (cfg.tone, cfg.talk_topics, cfg.reply_delay_hours) == ("polite", (), 0.0)


def test_account_options_parse():
    raw = _raw_config() | {"tone": "casual", "talk_topics": ["식세기 사라 vs 마라", "건조기 사라 vs 마라"], "reply_delay_hours": 3}
    cfg = settings.parse_config(raw)
    assert cfg.tone == "casual" and cfg.talk_topics == ("식세기 사라 vs 마라", "건조기 사라 vs 마라") and cfg.reply_delay_hours == 3.0


@pytest.mark.parametrize("change", [
    {"tone": "rude"}, {"talk_topics": ["a", "a"]}, {"talk_topics": [""]}, {"talk_topics": ["가" * 61]},
    {"reply_delay_hours": -1}, {"reply_delay_hours": 13}, {"reply_delay_hours": "3"}, {"reply_delay_hours": True},
])
def test_bad_account_options_refused(change):
    with pytest.raises(settings.ConfigError):
        settings.parse_config(_raw_config() | change)
