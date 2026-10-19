"""새 계정 만들기 도구: sabolkka 를 그대로 다시 만들 수 있어야 하고(회귀), 값이 비면 등록하지 않는다."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import settings

ROOT = settings.PROJECT_ROOT
sys.path.insert(0, str(ROOT / "tools"))

import new_account  # noqa: E402
import set_github_secrets  # noqa: E402


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_generated_sql_matches_sabolkka_files():
    """sabolkka 의 SQL 은 이 도구가 만든 것과 같아야 한다 (기존 db/ 가 바뀌면 같이 고치게 하는 안전장치)."""
    mine = ROOT / "accounts" / "sabolkka" / "db"
    assert new_account.render_schema("sabolkka") == read(mine / "schema.sql")
    for kind in ("publish", "generate", "digest"):
        assert new_account.render_cron(kind, "sabolkka") == read(mine / f"{kind}_cron.sql"), kind


def test_create_account_writes_expected_files_and_nothing_is_enabled(tmp_path):
    (tmp_path / "db").mkdir()
    for f in ("schema.sql", "generate_cron.sql", "digest_cron.sql", "publish_cron.sql"):
        (tmp_path / "db" / f).write_text((ROOT / "db" / f).read_text(encoding="utf-8"), encoding="utf-8")
    written = new_account.create_account("kitchenpick", root=tmp_path)
    rel = sorted(p.relative_to(tmp_path).as_posix() for p in written)
    assert rel == sorted([
        "accounts/kitchenpick/config.yaml", "accounts/kitchenpick/data/products.csv",
        "accounts/kitchenpick/db/schema.sql", "accounts/kitchenpick/db/publish_cron.sql",
        "accounts/kitchenpick/db/generate_cron.sql", "accounts/kitchenpick/db/digest_cron.sql",
        "accounts/kitchenpick/prompts/threads_base.md", "accounts/kitchenpick/prompts/tpl_bundle.md",
        "accounts/kitchenpick/prompts/tpl_pick.md", "accounts/kitchenpick/prompts/tpl_talk.md", ".env.kitchenpick"])
    env = (tmp_path / ".env.kitchenpick").read_text(encoding="utf-8")
    assert "DRY_RUN=true" in env and "AUTO_PUBLISH_DISABLED=true" in env and "THREADS_ACCESS_TOKEN=\n" in env
    sql = (tmp_path / "accounts/kitchenpick/db/publish_cron.sql").read_text(encoding="utf-8")
    assert "threads-coupas-kitchenpick-publish" in sql and '"account": "kitchenpick"' in sql
    assert "create_secret" not in sql and "__GH_DISPATCH_TOKEN__" not in sql
    assert "create schema if not exists kitchenpick;" in (tmp_path / "accounts/kitchenpick/db/schema.sql").read_text(encoding="utf-8")


def test_generated_config_is_valid_but_requires_topics_and_products(tmp_path):
    (tmp_path / "db").mkdir()
    for f in ("schema.sql", "generate_cron.sql", "digest_cron.sql"):
        (tmp_path / "db" / f).write_text((ROOT / "db" / f).read_text(encoding="utf-8"), encoding="utf-8")
    new_account.create_account("kitchenpick", root=tmp_path, tone="polite")
    raw = yaml.safe_load((tmp_path / "accounts/kitchenpick/config.yaml").read_text(encoding="utf-8"))
    cfg = settings.parse_config(raw)
    assert (cfg.tone, cfg.max_posts_per_day, cfg.reply_delay_hours, cfg.ops_advice) == ("polite", 3, 3.0, True)
    assert cfg.talk_topics == () and cfg.test_start_date is None
    assert not (tmp_path / "accounts/kitchenpick/prompts").exists()          # polite 는 기존 계정 프롬프트를 쓴다
    import products
    with pytest.raises(settings.ConfigError, match="상품이 없습니다"):
        products.load_products(tmp_path / "accounts/kitchenpick/data/products.csv")


@pytest.mark.parametrize("bad", ["openbrain", "Bad", "a", "1abc", "x y"])
def test_bad_names_refused(bad, tmp_path):
    with pytest.raises(ValueError):
        new_account.create_account(bad, root=tmp_path)


def test_existing_account_is_not_overwritten_without_force(tmp_path):
    (tmp_path / "accounts" / "kitchenpick").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        new_account.create_account("kitchenpick", root=tmp_path)


# ── GitHub Secrets 등록 ─────────────────────────────────────────────────

def write_env(tmp_path, **kw):
    values = {"THREADS_USER_ID": "1", "THREADS_ACCESS_TOKEN": "tok-SECRET", "THREADS_TOKEN_ISSUED_AT": "2026-10-19",
              "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "key-SECRET"} | kw
    (tmp_path / ".env.kitchenpick").write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")


def test_secrets_registered_with_suffix_and_value_only_on_stdin(tmp_path):
    write_env(tmp_path)
    calls = []

    def fake_run(cmd, input=None, **kw):
        calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    names = set_github_secrets.register("kitchenpick", "o/r", runner=fake_run, env_dir=tmp_path)
    assert names == [f"{k}_KITCHENPICK" for k in set_github_secrets.KEYS]
    assert all(c[0][:4] == ["gh", "secret", "set", n] and c[0][-2:] == ["--repo", "o/r"] for c, n in zip(calls, names))
    assert not any("SECRET" in " ".join(c[0]) for c in calls)                    # 값은 명령줄이 아니라 stdin 으로만
    assert [c[1] for c in calls][1] == "tok-SECRET"


def test_secrets_not_registered_when_a_value_is_empty(tmp_path):
    write_env(tmp_path, SUPABASE_URL="")
    calls = []
    with pytest.raises(ValueError, match="SUPABASE_URL"):
        set_github_secrets.register("kitchenpick", "o/r", runner=lambda *a, **k: calls.append(a), env_dir=tmp_path)
    assert calls == []


def test_dry_run_registers_nothing(tmp_path):
    write_env(tmp_path)
    calls = []
    names = set_github_secrets.register("kitchenpick", "o/r", dry_run=True, runner=lambda *a, **k: calls.append(a), env_dir=tmp_path)
    assert len(names) == 5 and calls == []
