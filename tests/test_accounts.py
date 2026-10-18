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
