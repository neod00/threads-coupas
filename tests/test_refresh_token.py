"""Threads 장기 토큰 자동 연장 (2026-11-26, refresh_token.py / refresh_token.yml)."""
from datetime import date

import pytest
import yaml

import refresh_token
from settings import PROJECT_ROOT

TODAY = date(2026, 12, 1)
TOKEN = "THAAold" + "x" * 40
NEW = "THAAnew" + "y" * 40


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.content = status, data, b"x"

    def json(self):
        return self._data


class Http:
    def __init__(self, resp=None, exc=None):
        self.calls, self.resp, self.exc = [], resp, exc

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.exc:
            raise self.exc
        return self.resp


def run(tmp_path, issued, http, *, dry=False, extra_env=None):
    out = tmp_path / "new_token"
    gh_out = tmp_path / "gh_output"
    env = {"THREADS_ACCESS_TOKEN": TOKEN, "THREADS_TOKEN_ISSUED_AT": issued, "GITHUB_OUTPUT": str(gh_out), **(extra_env or {})}
    code = refresh_token.main(["--out", str(out)] + (["--dry-run"] if dry else []), http=http, env=env, today=TODAY)
    return code, out, gh_out


@pytest.mark.parametrize("issued, action", [
    (date(2026, 11, 25), "skip"),        # 6일째
    (date(2026, 11, 17), "refresh"),     # 14일째
    (date(2026, 10, 10), "refresh"),     # 52일째, 아직 안 만료
    (date(2026, 10, 2), "expired"),      # 60일 지남
    (None, "refresh"),                   # 발급일 모름
])
def test_decide(issued, action):
    assert refresh_token.decide(issued, TODAY)[0] == action


def test_refresh_writes_token_to_file_only_and_reports_output(tmp_path, capsys):
    http = Http(Resp(200, {"access_token": NEW, "token_type": "bearer", "expires_in": 5184000}))
    code, out, gh_out = run(tmp_path, "2026-11-10", http)
    assert code == 0
    assert http.calls == [(refresh_token.API, {"grant_type": "th_refresh_token", "access_token": TOKEN})]
    assert out.read_text(encoding="utf-8") == NEW
    assert gh_out.read_text(encoding="utf-8") == "refreshed=true\nissued_at=2026-12-01\n"
    printed = capsys.readouterr()
    assert NEW not in printed.out + printed.err and TOKEN not in printed.out + printed.err


def test_mask_is_set_before_anything_else_on_actions(tmp_path, capsys):
    http = Http(Resp(200, {"access_token": NEW, "expires_in": 5184000}))
    run(tmp_path, "2026-11-10", http, extra_env={"GITHUB_ACTIONS": "true"})
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"::add-mask::{NEW}"]          # 새 토큰은 가림 명령으로만 나온다


def test_young_token_is_left_alone(tmp_path):
    http = Http()
    code, out, gh_out = run(tmp_path, "2026-11-28", http)
    assert code == 0 and http.calls == [] and not out.exists() and not gh_out.exists()


def test_expired_token_fails_so_an_alert_goes_out(tmp_path):
    http = Http()
    code, out, _ = run(tmp_path, "2026-09-27", http)
    assert code == 1 and http.calls == [] and not out.exists()


def test_dry_run_never_calls_the_api(tmp_path):
    http = Http()
    code, out, gh_out = run(tmp_path, "2026-11-01", http, dry=True)
    assert code == 0 and http.calls == [] and not out.exists() and not gh_out.exists()


@pytest.mark.parametrize("http", [
    Http(Resp(400, {"error": {"message": "Session has expired"}})),
    Http(Resp(200, {})),
    Http(exc=ConnectionError(f"https://graph.threads.net/refresh_access_token?access_token={TOKEN}")),
])
def test_api_failure_fails_without_leaking_the_token(tmp_path, http, caplog):
    code, out, gh_out = run(tmp_path, "2026-11-01", http)
    assert code == 1 and not out.exists() and not gh_out.exists()
    assert TOKEN not in caplog.text


def test_workflow_saves_only_after_a_refresh_and_never_without_the_save_token():
    wf = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "refresh_token.yml").read_text(encoding="utf-8"))
    on = wf.pop(True, wf.get("on"))
    assert on["schedule"] == [{"cron": "20 0 * * 1"}]
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    steps = {s.get("name"): s for s in wf["jobs"]["refresh"]["steps"]}
    refresh = steps["Refresh Threads token"]["run"]
    assert refresh.index('-z "${GH_SECRETS_TOKEN}"') < refresh.index("python refresh_token.py")   # 저장 못 하면 연장도 안 한다
    save = steps["Save to GitHub Secrets"]
    assert save["if"] == "steps.refresh.outputs.refreshed == 'true'"
    assert save["env"]["GH_TOKEN"] == "${{ secrets.GH_SECRETS_TOKEN }}"
    assert '< "${RUNNER_TEMP}/new_token"' in save["run"] and "rm -f" in save["run"]
    assert "echo" not in save["run"].split("rm -f")[0].replace('echo "THREADS_ACCESS_TOKEN', "")  # 토큰 값을 echo 하지 않는다
    assert steps["Alert on failure"]["if"] == "failure() && env.DRY_RUN != 'true'"
