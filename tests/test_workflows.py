"""Step 7 GitHub Actions 워크플로 설정 검사 (PLAN v2 Step 7 표)."""
import re

import pytest
import yaml

from settings import PROJECT_ROOT, load_config

WF_DIR = PROJECT_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(p.name for p in WF_DIR.glob("*.yml"))


def load(name):
    data = yaml.safe_load((WF_DIR / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))       # YAML 1.1 은 'on' 을 True 로 읽는다
    return data


def test_expected_workflows_exist():
    # metrics.yml·weekly.yml 은 Step 8·9 에서 스크립트와 함께 추가한다
    assert {"generate.yml", "publish.yml", "digest.yml", "cancel_post.yml"} <= set(WORKFLOWS)


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_workflow_has_manual_run_with_dry_run(name):
    inputs = load(name)["on"]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["type"] == "boolean"


@pytest.mark.parametrize("name, cron", [
    ("publish.yml", "3 */4 * * *"),     # 예비. 주 실행은 Supabase pg_cron (db/publish_cron.sql)
    ("generate.yml", "0 14 * * *"),     # 예비. 주 실행은 Supabase pg_cron 20:00 KST (db/generate_cron.sql)
])
def test_schedules(name, cron):
    assert load(name)["on"]["schedule"] == [{"cron": cron}]


def test_digest_cron_matches_config():
    """일일 요약은 Supabase pg_cron 이 깨운다 (GitHub 예약이 있으면 요약이 두 번 간다)."""
    assert "schedule" not in load("digest.yml")["on"]
    sql = (PROJECT_ROOT / "db" / "digest_cron.sql").read_text(encoding="utf-8")
    cron = re.search(r"'threads-coupas-digest',\s*'([^']+)'", sql).group(1)
    minute, hour = map(int, cron.split()[:2])
    t = load_config().digest_time_kst
    assert ((hour + 9) % 24, minute) == (t.hour, t.minute)
    assert "/repos/neod00/threads-coupas/actions/workflows/digest.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert "github_pat_" not in sql and "gh_dispatch_token" in sql


def test_publish_safety_switches():
    wf = load("publish.yml")
    job = wf["jobs"]["publish"]
    assert wf["concurrency"] == {"group": "publish", "cancel-in-progress": False}
    assert load("cancel_post.yml")["concurrency"]["group"] == "publish"
    assert job["if"] == "vars.AUTO_PUBLISH_DISABLED != 'true'"
    assert job["env"]["AUTO_PUBLISH_DISABLED"] == "${{ vars.AUTO_PUBLISH_DISABLED }}"
    # 규칙 9: 저장소 변수 DRY_RUN 이 정확히 'false' 이고 수동 dry_run 이 꺼졌을 때만 실제 발행, 그 밖에는 모두 'true'
    assert job["env"]["DRY_RUN"] == "${{ (vars.DRY_RUN == 'false' && inputs.dry_run != true) && 'false' || 'true' }}"
    assert load("publish.yml")["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


def test_generate_respects_kill_switch():
    job = load("generate.yml")["jobs"]["generate"]
    assert job["if"] == "vars.AUTO_PUBLISH_DISABLED != 'true'"
    assert job["env"]["AUTO_PUBLISH_DISABLED"] == "${{ vars.AUTO_PUBLISH_DISABLED }}"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_referenced_secrets_exist_in_env_example(name):
    # 오타 난 시크릿 이름은 빈 값으로 들어가 조용히 실패하므로 .env.example 의 키와 맞춰 본다
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    used = set(re.findall(r"secrets\.([A-Z_]+)", (WF_DIR / name).read_text(encoding="utf-8")))
    assert used <= keys, used - keys


@pytest.mark.parametrize("name", WORKFLOWS)
def test_least_privilege(name):
    assert load(name)["permissions"] == {"contents": "read"}


def test_supabase_publish_cron():
    """GitHub 예약이 밀려서 Supabase pg_cron 이 publish.yml 을 30분마다 깨운다. 토큰은 저장소에 없다."""
    sql = (PROJECT_ROOT / "db" / "publish_cron.sql").read_text(encoding="utf-8")
    assert "'12,42 * * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/publish.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert "github_pat_" not in sql and "__GH_DISPATCH_TOKEN__" in sql
    # 실제 발행 여부는 여전히 저장소 변수 DRY_RUN·AUTO_PUBLISH_DISABLED 가 정한다
    job = load("publish.yml")["jobs"]["publish"]
    assert "vars.DRY_RUN == 'false'" in job["env"]["DRY_RUN"]
    assert job["if"] == "vars.AUTO_PUBLISH_DISABLED != 'true'"


def test_supabase_generate_cron():
    """생성도 Supabase pg_cron 이 20:00 KST(11:00 UTC)에 깨운다. 토큰은 publish_cron.sql 이 Vault 에 넣어 둔 것을 쓴다."""
    sql = (PROJECT_ROOT / "db" / "generate_cron.sql").read_text(encoding="utf-8")
    assert "'0 11 * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/generate.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert "github_pat_" not in sql and "__GH_DISPATCH_TOKEN__" not in sql
    assert "gh_dispatch_token" in sql
    assert load("generate.yml")["jobs"]["generate"]["if"] == "vars.AUTO_PUBLISH_DISABLED != 'true'"
