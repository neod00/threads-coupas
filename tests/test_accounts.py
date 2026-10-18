"""계정 분리 (PLAN 규칙 1 개정, 2026-10-18): 계정마다 설정·비밀값·DB 를 따로 쓰고 서로 섞이지 않는다."""
import importlib
import subprocess
import sys

import pytest

import settings

ROOT = settings.PROJECT_ROOT


def run_py(code: str, account: str | None, env_extra: dict | None = None):
    import os
    env = {k: v for k, v in os.environ.items() if k != "ACCOUNT"}
    if account is not None:
        env["ACCOUNT"] = account
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")


def test_default_account_uses_project_root():
    r = run_py("import settings as s; print(s.ACCOUNT=='' , s.CONFIG_PATH==s.PROJECT_ROOT/'config.yaml', s.ENV_PATH==s.PROJECT_ROOT/'.env', s.account_label()=='')", None)
    assert r.stdout.split() == ["True"] * 4, r.stderr


def test_account_uses_own_config_and_env_file():
    r = run_py("import settings as s; print(s.CONFIG_PATH.relative_to(s.PROJECT_ROOT).as_posix(), s.ENV_PATH.name, s.account_label())", "sabolkka")
    assert r.stdout.split() == ["accounts/sabolkka/config.yaml", ".env.sabolkka", "[sabolkka]"], r.stderr


@pytest.mark.parametrize("bad", ["Sabol", "a", "../x", "x y", "9abc"])
def test_bad_account_name_is_refused(bad):
    assert run_py("import settings", bad).returncode != 0


def test_account_files_fall_back_to_root_per_file():
    code = ("import settings as s;"
            "print(s.account_path('data','products.csv').relative_to(s.PROJECT_ROOT).as_posix());"
            "print(s.account_path('data','tariff.yaml').relative_to(s.PROJECT_ROOT).as_posix());"
            "print(s.account_path('prompts','reviewer.md').relative_to(s.PROJECT_ROOT).as_posix())")
    r = run_py(code, "sabolkka")
    assert r.stdout.split() == ["accounts/sabolkka/data/products.csv", "data/tariff.yaml", "prompts/reviewer.md"], r.stderr


def test_account_config_has_no_fallback_to_other_account(tmp_path):
    """계정 config.yaml 이 없으면 다른 계정 설정으로 조용히 도는 대신 실패해야 한다."""
    r = run_py("import settings as s; s.load_config()", "no_such_account")
    assert r.returncode != 0 and "설정 파일이 없습니다" in r.stderr


def test_sabolkka_config_loads_and_respects_hard_limits():
    r = run_py("import settings as s; c=s.load_config(); print(c.max_posts_per_day<=s.HARD_MAX_POSTS_PER_DAY)", "sabolkka")
    assert r.stdout.split() == ["True"], r.stderr


def test_shared_env_does_not_leak_account_only_values(tmp_path, monkeypatch):
    """다른 계정 실행이 공용 .env 의 Threads 토큰·DB·DRY_RUN 값을 물려받으면 엉뚱한 계정으로 발행된다."""
    shared = tmp_path / ".env"
    shared.write_text("THREADS_ACCESS_TOKEN=OTHER\nSUPABASE_URL=https://other\nDRY_RUN=false\n"
                      "AUTO_PUBLISH_DISABLED=false\nGEMINI_API_KEY=shared-key\nTHREADS_APP_SECRET=app\n", encoding="utf-8")
    for key in ("THREADS_ACCESS_TOKEN", "SUPABASE_URL", "DRY_RUN", "AUTO_PUBLISH_DISABLED", "GEMINI_API_KEY", "THREADS_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    assert settings.load_shared_env_file(shared) is True
    import os
    assert os.environ["GEMINI_API_KEY"] == "shared-key" and os.environ["THREADS_APP_SECRET"] == "app"
    for key in ("THREADS_ACCESS_TOKEN", "SUPABASE_URL", "DRY_RUN", "AUTO_PUBLISH_DISABLED"):
        assert key not in os.environ
    for key in ("GEMINI_API_KEY", "THREADS_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)


def test_account_run_without_own_secrets_stays_in_dry_run_and_empty():
    code = "import settings as s; print(s.dry_run(), s.load_secrets.__name__)"
    r = run_py(code, "sabolkka", {"DRY_RUN": ""})
    assert r.stdout.split()[0] == "True", r.stderr


def test_telegram_messages_carry_account_label(monkeypatch):
    import notify
    sent = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    monkeypatch.setattr(notify, "account_label", lambda: "[sabolkka] ")
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=0: sent.append(req.data.decode()) or Resp())
    notify.TelegramNotifier("t", "c").send("hello")
    assert "%5Bsabolkka%5D+hello" in sent[0]


def test_store_uses_account_schema_only_for_non_default_accounts():
    import store
    mine = store.SupabaseStore("https://x.supabase.co", "sb_secret_" + "a" * 30, schema="sabolkka")
    base = store.SupabaseStore("https://x.supabase.co", "sb_secret_" + "a" * 30)
    assert mine.db.table("posts").headers.get("Accept-Profile") == "sabolkka"
    assert mine.db.table("posts").headers.get("Content-Profile") == "sabolkka"
    assert base.db.table("posts").headers.get("Accept-Profile") in (None, "public")


def test_open_store_passes_account_as_schema(monkeypatch):
    import store
    seen = {}
    monkeypatch.setattr(store, "SupabaseStore", lambda url, key, schema=None: seen.update(url=url, schema=schema))
    secrets = type("S", (), {"supabase_url": "u", "supabase_service_role_key": "k", "require": lambda self, *a: None})()
    monkeypatch.setattr(settings, "ACCOUNT", "sabolkka")
    store.open_store(secrets)
    assert seen["schema"] == "sabolkka"
    monkeypatch.setattr(settings, "ACCOUNT", "")
    store.open_store(secrets)
    assert seen["schema"] is None


def test_sabolkka_schema_sql_matches_base_schema():
    """sabolkka 스키마는 db/schema.sql 과 같은 테이블·제약을 sabolkka 스키마에 만든다 (한쪽만 고치면 어긋난다)."""
    base = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    mine = (ROOT / "accounts" / "sabolkka" / "db" / "schema.sql").read_text(encoding="utf-8")
    def tables(sql):
        import re
        return sorted(re.findall(r"create table if not exists (\w+) \((.*?)\n\);", sql, re.S))
    assert tables(mine) == tables(base) and len(tables(base)) == 5
    assert "create schema if not exists sabolkka;" in mine and "set search_path to sabolkka;" in mine
    assert "grant usage on schema sabolkka to service_role;" in mine
    assert "to anon" not in mine and "to authenticated" not in mine
    assert mine.count("enable row level security") == base.count("enable row level security") == 5


def test_reviewer_tone_rule_per_account_and_polite_prompt_unchanged():
    import dataclasses
    import reviewer
    polite = settings.load_config()
    casual = dataclasses.replace(polite, tone="casual")
    p, c = reviewer.system_prompt(polite), reviewer.system_prompt(casual)
    assert "- R11 말투가 존댓말(해요체 또는 합니다체)로 일관되고" in p and "반말·자음 표현(ㅋㅋ)" in p
    assert "반말·해요체·합니다체 모두 허용" in c and "반말·자음 표현" not in c
    assert reviewer.prompt_hash(polite) != reviewer.prompt_hash(casual)       # 계정마다 시험지를 따로 본다


def test_generator_reply_line_spec_follows_tone():
    import dataclasses
    import generator
    polite = settings.load_config()
    assert "해요체" in generator.system_prompt(polite, "pick", "link_in_reply")
    casual = dataclasses.replace(polite, tone="casual")
    assert "친근한 반말" in generator.system_prompt(casual, "pick", "link_in_reply")


def test_talk_topics_from_config_replace_default_list():
    import generator
    assert [s["tip_topic"] for s in generator.subjects([], "talk", talk_topics=("a 사라 vs 마라",))] == ["a 사라 vs 마라"]
    assert len(generator.subjects([], "talk")) == len(generator.TALK_TOPICS)
