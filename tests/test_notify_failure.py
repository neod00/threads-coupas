"""워크플로 실패 알림 (2026-11-18): 11-17 sabol.kka 생성이 조용히 멈춰 하루 글이 빠진 뒤 추가."""
import yaml

import notify_failure
from notify import CollectingNotifier
from settings import PROJECT_ROOT

# 2026-11-17 실제 실패 로그의 끝부분
REAL_LOG = """INFO [생성 gift] 시도 1/3: 통과
INFO HTTP Request: POST ***posts "HTTP/2 400 Bad Request"
Traceback (most recent call last):
  File "/home/runner/work/threads-coupas/threads-coupas/pipeline.py", line 164, in run_generate
    store.insert_post(post)
postgrest.exceptions.APIError: {'message': 'new row for relation "posts" violates check constraint "posts_type_check"', 'code': '23514'}
"""


def test_last_error_picks_the_exception_line():
    err = notify_failure.last_error(REAL_LOG)
    assert err.startswith("postgrest.exceptions.APIError") and "posts_type_check" in err


def test_last_error_without_error_line_and_empty_log():
    assert notify_failure.last_error("a\nb\nlast line\n") == "last line"
    assert notify_failure.last_error("") == "로그가 비어 있음"
    assert len(notify_failure.last_error("ValueError: " + "x" * 1000)) == notify_failure.ERROR_CHARS


def test_main_sends_job_error_note_and_url(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(REAL_LOG, encoding="utf-8")
    n = CollectingNotifier()
    assert notify_failure.main(["--job", "생성", "--log", str(log), "--url", "https://github.com/x/runs/1",
                                "--note", "다음 날 글이 비어 있을 수 있어요."], notifier=n) == 0
    text = n.sent[0]
    assert text.startswith("[실패] 생성 작업이 멈췄습니다.")
    assert "다음 날 글이 비어 있을 수 있어요." in text and "posts_type_check" in text
    assert text.endswith("기록: https://github.com/x/runs/1")


def test_missing_log_still_alerts(tmp_path):
    n = CollectingNotifier()
    notify_failure.main(["--job", "생성", "--log", str(tmp_path / "none.log")], notifier=n)
    assert "로그가 비어 있음" in n.sent[0]


def test_alert_failure_never_raises(tmp_path):
    class Broken:
        def send(self, text):
            raise RuntimeError("텔레그램 장애")
    assert notify_failure.main(["--job", "생성"], notifier=Broken()) == 0


def test_no_telegram_settings_is_quiet(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify_failure.main(["--job", "생성"]) == 0
    assert "보내지 않습니다" in capsys.readouterr().out


def test_generate_workflow_alerts_on_failure_but_not_on_dry_run():
    steps = yaml.safe_load((PROJECT_ROOT / ".github/workflows/generate.yml").read_text(encoding="utf-8"))["jobs"]["generate"]["steps"]
    gen = next(s for s in steps if s.get("name") == "Generate tomorrow's posts")
    assert "set -euo pipefail" in gen["run"] and "| tee run.log" in gen["run"]     # pipefail 이라 tee 가 실패를 덮지 않는다
    alert = next(s for s in steps if s.get("name") == "Alert on failure")
    assert steps.index(alert) > steps.index(gen)
    assert alert["if"].startswith("failure()") and "inputs.dry_run != true" in alert["if"]
    assert "notify_failure.py" in alert["run"] and "--log run.log" in alert["run"]
    assert set(alert["env"]) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "RUN_URL"}


def test_publish_workflow_alerts_on_failure_only_when_really_publishing():
    job = yaml.safe_load((PROJECT_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8"))["jobs"]["publish"]
    steps = job["steps"]
    pub = next(s for s in steps if s.get("name") == "Publish one due post")
    # GitHub 기본 셸은 pipefail 이 꺼져 있어, 없으면 tee 가 발행 실패를 성공으로 덮는다
    assert "set -o pipefail" in pub["run"] and "python publisher.py 2>&1 | tee run.log" in pub["run"]
    alert = next(s for s in steps if s.get("name") == "Alert on failure")
    assert steps.index(alert) > steps.index(pub)
    assert alert["if"] == "failure() && env.DRY_RUN != 'true'" and "DRY_RUN" in job["env"]
    assert "--job 발행" in alert["run"] and "--log run.log" in alert["run"]
    assert set(alert["env"]) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "RUN_URL"}
