"""영어 계정(lang: en, 2026-11-07, accounts/openbrain_en) 시험. 기존 한국어 계정 동작이 바뀌지 않는지도 함께 본다."""
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import capture
import exam
import generator
import guard
import reviewer
import settings
from settings import ConfigError, contains_term, load_config, parse_config

ROOT = Path(__file__).resolve().parent.parent
EN_DIR = ROOT / "accounts" / "openbrain_en"


def en_raw() -> dict:
    return yaml.safe_load((EN_DIR / "config.yaml").read_text(encoding="utf-8"))


def en_cfg():
    return load_config(EN_DIR / "config.yaml")


def ko_app_cfg():
    return load_config(ROOT / "config.yaml")


# ── 설정 ─────────────────────────────────────────────────────────────

def test_en_account_config_loads():
    cfg = en_cfg()
    assert (cfg.lang, cfg.content, cfg.disclosure_text) == ("en", "app", "I built this app.")
    assert set(settings.REQUIRED_BANNED_WORDS_EN) <= set(cfg.banned_words)
    assert set(settings.REQUIRED_BANNED_WORDS) <= set(cfg.banned_words)      # 한국어 필수 금지어도 그대로
    assert set(settings.REQUIRED_BANNED_HOOK_PHRASES_EN) <= set(cfg.banned_hook_phrases)


def test_existing_accounts_default_to_korean():
    assert ko_app_cfg().lang == "ko"
    assert load_config(ROOT / "accounts" / "sabolkka" / "config.yaml").lang == "ko"
    assert not set(settings.REQUIRED_BANNED_WORDS_EN) & set(ko_app_cfg().banned_words)


def test_en_disclosure_must_say_i_built():
    raw = en_raw()
    raw["disclosure_text"] = "Check out my app."
    with pytest.raises(ConfigError, match="I built"):
        parse_config(raw)


def test_unknown_lang_and_en_coupang_are_rejected():
    raw = en_raw()
    raw["lang"] = "ja"
    with pytest.raises(ConfigError, match="lang"):
        parse_config(raw)
    raw = yaml.safe_load((ROOT / "accounts" / "sabolkka" / "config.yaml").read_text(encoding="utf-8"))
    raw["lang"] = "en"
    with pytest.raises(ConfigError, match="content: app"):
        parse_config(raw)


def test_contains_term_korean_is_plain_substring():
    assert contains_term("최저가로 고쳐요", "최저가")
    assert contains_term("Best", "best") is False            # 한국어 계정은 예전처럼 대소문자를 가린다


@pytest.mark.parametrize("text,term,found", [
    ("The Best pdf editor", "best", True),
    ("bestow", "best", False),
    ("unsafe", "safe", False),
    ("it is safe", "safe", True),
    ("We're #1 now", "#1", True),
    ("#12 issue", "#1", False),
    ("100% secure files", "100% secure", True),
    ("cheapest!", "cheapest", True),
])
def test_contains_term_english_word_boundaries(text, term, found):
    assert contains_term(text, term, "en") is found


# ── 가드 ─────────────────────────────────────────────────────────────

def en_post(hook="built a pdf editor that never uploads your file", body="free, no signup\ntell me what breaks", reply=None):
    return {"type": "launch", "text": f"{hook}\n\n{body}", "reply_text": reply or "I built this app.\n\nYou can try it here.",
            "has_link": True, "link_url": "https://pdfmonster.xyz/en/", "variant": "link_in_reply",
            "calc_payload": {"app": {"facts": ["Free"]}}, "media": []}


def test_en_guard_passes_clean_post():
    assert guard.check_post(en_post(), en_cfg()) == []


def test_en_guard_hook_limit_is_60():
    cfg = en_cfg()
    ok = "x" * 60
    assert not [p for p in guard.check_post(en_post(hook=ok), cfg) if p.startswith("[G7]")]
    assert [p for p in guard.check_post(en_post(hook=ok + "x"), cfg) if p.startswith("[G7]")]
    # 한국어 계정은 30자 그대로
    assert guard.HOOK_MAX_CHARS_BY_LANG["ko"] == guard.HOOK_MAX_CHARS == 30


def test_en_guard_banned_words_ignore_case():
    problems = guard.check_post(en_post(body="The BEST editor\nGuaranteed to work"), en_cfg())
    g4 = [p for p in problems if p.startswith("[G4]")]
    assert g4 and "best" in g4[0] and "guaranteed" in g4[0]


def test_en_guard_blocks_hangul():
    problems = guard.check_post(en_post(body="free\n써 보고 알려 줘"), en_cfg())
    assert any(p.startswith("[G10]") for p in problems)
    # 한국어 계정에는 G10 이 없다
    ko_post = dict(en_post(body="써 보고 알려 줘"), reply_text="내가 만든 앱이야.\n\n여기서 써 봐")
    assert not any(p.startswith("[G10]") for p in guard.check_post(ko_post, ko_app_cfg()))


def test_en_guard_requires_disclosure_first_line():
    problems = guard.check_post(en_post(reply="You can try it here."), en_cfg())
    assert any(p.startswith("[G2]") for p in problems)


# ── 생성기·검수자·시험지·캡처 ───────────────────────────────────────

def test_en_generator_messages_are_english(monkeypatch):
    monkeypatch.setattr(generator, "_read_prompt", lambda name: f"<{name}>")
    msg = generator.user_message("feature", {"a": 1}, feedback=["[G4] 금지어"], previous={"body": "x"},
                                 recent_texts=["old post"], lang="en")
    assert "App data (JSON)" in msg and "Recent post 1" in msg and "Write only in English" in msg
    assert "앱 데이터" not in msg and "최근에 올린 글" not in msg
    ko = generator.user_message("feature", {"a": 1}, recent_texts=["old"])
    assert "앱 데이터" in ko


def test_en_system_prompt_uses_english_reply_spec(monkeypatch):
    # 테스트는 ACCOUNT 없이 돌아서 계정 폴더 프롬프트를 직접 읽게 한다
    monkeypatch.setattr(generator, "_read_prompt", lambda name: (EN_DIR / "prompts" / name).read_text(encoding="utf-8"))
    cfg = en_cfg()
    text = generator.system_prompt(cfg, "launch", "link_in_reply")
    assert generator.APP_REPLY_SPEC_EN in text and "Never use Korean" in text


def test_en_reviewer_prompt_and_exam_cases():
    cfg = en_cfg()
    assert reviewer.prompt_path_for(cfg) == reviewer.APP_EN_PROMPT_PATH
    assert reviewer.prompt_path_for(ko_app_cfg()) == reviewer.APP_PROMPT_PATH
    assert "I built this app." in reviewer.system_prompt(cfg)
    assert exam.cases_path_for(cfg) == exam.APP_EN_CASES_PATH
    assert exam.cases_path_for(ko_app_cfg()) == exam.APP_CASES_PATH


def test_en_exam_cases_build_and_are_balanced():
    cfg = en_cfg()
    data = exam.load_cases(exam.APP_EN_CASES_PATH)
    expects = [c["expect"] for c in data["cases"]]
    assert expects.count("block") == 20 and expects.count("pass") == 10
    for case in data["cases"]:
        post, rows, recent = exam.build_case(case, data, cfg)
        assert post["text"] and rows
        if case["expect"] == "pass":
            # 정상 글은 코드 가드도 통과해야 한다 (시험지가 가드와 어긋나지 않게)
            assert guard.check_post(dict(post, media=[]), cfg) == [], case["id"]


def test_en_capture_locale_and_banned_captions():
    assert capture.LOCALES["en"] == "en-US"
    apps = capture.load_apps(EN_DIR / "data" / "apps.yaml")
    cfg = en_cfg()
    assert capture.banned_text_problems(apps, cfg.banned_words, cfg.lang) == []
    for app in apps:
        for item in (*app.shots, *app.clips):
            steps = [*getattr(item, "setup", ()), *item.steps]
            # 앱 주소가 /en/ 이므로 goto 는 그 아래 경로여야 한다 (/en/en/ 으로 가지 않게)
            assert all(not str(s.get("goto", "")).startswith("/en") for s in steps), item.id


def test_en_app_data_has_no_korean():
    text = (EN_DIR / "data" / "apps.yaml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not guard.HANGUL_RE.search(body)
    for name in ("app_base.md", "tpl_launch.md", "tpl_feature.md", "tpl_devlog.md"):
        assert not guard.HANGUL_RE.search((EN_DIR / "prompts" / name).read_text(encoding="utf-8")), name
