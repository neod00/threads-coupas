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
    assert "publish-" in wf["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False
    assert wf["concurrency"]["group"] == load("cancel_post.yml")["concurrency"]["group"]
    # 킬 스위치는 계정별 변수 AUTO_PUBLISH_DISABLED(_계정)를 환경변수로 넘기고, 발행 코드가 정상 종료한다 (job if 는 matrix 를 못 쓴다)
    assert "if" not in job
    assert "vars[format('AUTO_PUBLISH_DISABLED{0}'" in job["env"]["AUTO_PUBLISH_DISABLED"]
    # 규칙 9: 계정별 변수 DRY_RUN(_계정)이 정확히 'false' 이고 수동 dry_run 이 꺼졌을 때만 실제 발행, 그 밖에는 모두 'true'
    dry = job["env"]["DRY_RUN"]
    assert "vars[format('DRY_RUN{0}'" in dry and "== 'false'" in dry and "inputs.dry_run != true" in dry
    assert dry.endswith("&& 'false' || 'true' }}")
    assert load("publish.yml")["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


def test_generate_respects_kill_switch():
    job = load("generate.yml")["jobs"]["generate"]
    assert "if" not in job
    assert "vars[format('AUTO_PUBLISH_DISABLED{0}'" in job["env"]["AUTO_PUBLISH_DISABLED"]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_job_runs_one_account_per_matrix_entry(name):
    """계정 하나당 job 하나. 계정 전용 비밀값은 접미사 규칙(THREADS_USER_ID_SABOLKKA)으로만 고른다."""
    wf = load(name)
    assert wf["on"]["workflow_dispatch"]["inputs"]["account"]["default"] == "openbrain"
    for job in wf["jobs"].values():
        assert job["strategy"]["fail-fast"] is False
        matrix = job["strategy"]["matrix"]["account"]
        assert "fromJSON" in matrix and "vars.ACCOUNTS_JSON || '[\"openbrain\"]'" in matrix   # 예약 실행은 변수에 적은 계정들, 변수가 없으면 기존 계정만
        assert job["env"]["ACCOUNT"] == "${{ matrix.account != 'openbrain' && matrix.account || '' }}"
        assert job["env"]["SFX"] == "${{ matrix.account != 'openbrain' && format('_{0}', matrix.account) || '' }}"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_account_only_secrets_never_use_plain_names(name):
    text = (WF_DIR / name).read_text(encoding="utf-8")
    from settings import ACCOUNT_ONLY_KEYS
    for key in ACCOUNT_ONLY_KEYS - {"DRY_RUN", "AUTO_PUBLISH_DISABLED", "GH_DISPATCH_TOKEN"}:
        assert f"secrets.{key}" not in text, f"{name}: {key} 는 secrets[format('{key}{{0}}', env.SFX)] 로 읽어야 한다"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_referenced_secrets_exist_in_env_example(name):
    # 오타 난 시크릿 이름은 빈 값으로 들어가 조용히 실패하므로 .env.example 의 키와 맞춰 본다
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    text = (WF_DIR / name).read_text(encoding="utf-8")
    used = set(re.findall(r"secrets\.([A-Z_]+)", text)) | set(re.findall(r"secrets\[format\('([A-Z_]+)\{0\}'", text))
    assert used <= keys, used - keys


@pytest.mark.parametrize("name", WORKFLOWS)
def test_least_privilege(name):
    # 캡처만 그림을 커밋하려고 contents: write. 대신 비밀값을 하나도 읽지 않는다
    if name == "capture.yml":
        assert load(name)["permissions"] == {"contents": "write"}
        assert "secrets" not in (WF_DIR / name).read_text(encoding="utf-8")
        return
    assert load(name)["permissions"] == {"contents": "read"}


def test_supabase_publish_cron():
    """GitHub 예약이 밀려서 Supabase pg_cron 이 publish.yml 을 30분마다 깨운다. 토큰은 저장소에 없다."""
    sql = (PROJECT_ROOT / "db" / "publish_cron.sql").read_text(encoding="utf-8")
    assert "'12,42 * * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/publish.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert not re.search(r"(github_pat_|ghp_)[A-Za-z0-9_]{20,}", sql) and "__GH_DISPATCH_TOKEN__" in sql   # 토큰 모양을 검사하는 정규식은 있어도 진짜 토큰은 없다
    # 실제 발행 여부는 여전히 저장소 변수 DRY_RUN·AUTO_PUBLISH_DISABLED 가 정한다
    job = load("publish.yml")["jobs"]["publish"]
    assert "vars[format('DRY_RUN{0}'" in job["env"]["DRY_RUN"] and "== 'false'" in job["env"]["DRY_RUN"]
    assert "if" not in job and "AUTO_PUBLISH_DISABLED" in job["env"]


def test_supabase_generate_cron():
    """생성도 Supabase pg_cron 이 20:00 KST(11:00 UTC)에 깨운다. 토큰은 publish_cron.sql 이 Vault 에 넣어 둔 것을 쓴다."""
    sql = (PROJECT_ROOT / "db" / "generate_cron.sql").read_text(encoding="utf-8")
    assert "'0 11 * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/generate.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert "github_pat_" not in sql and "__GH_DISPATCH_TOKEN__" not in sql
    assert "gh_dispatch_token" in sql
    assert "AUTO_PUBLISH_DISABLED" in load("generate.yml")["jobs"]["generate"]["env"]


@pytest.mark.parametrize("name", ["publish", "generate", "digest"])
def test_sabolkka_cron_wakes_only_its_own_account(name):
    """같은 Supabase 프로젝트를 쓰므로 예약 이름이 기존 것과 겹치면 안 되고, 깨울 때 account=sabolkka 를 넘긴다.
    토큰(Vault)은 기존 publish_cron.sql 이 넣어 둔 것을 쓰므로 sabolkka 쪽 SQL 은 토큰을 건드리지 않는다."""
    mine = (PROJECT_ROOT / "accounts" / "sabolkka" / "db" / f"{name}_cron.sql").read_text(encoding="utf-8")
    base = (PROJECT_ROOT / "db" / f"{name}_cron.sql").read_text(encoding="utf-8")
    assert '"inputs": {"account": "sabolkka", "dry_run": "false"}' in mine
    assert f"'threads-coupas-sabolkka-{name}'" in mine and f"'threads-coupas-{name}'" not in mine
    assert f"/actions/workflows/{name}.yml/dispatches" in mine
    assert "create_secret" not in mine and "update_secret" not in mine and "__GH_DISPATCH_TOKEN__" not in mine
    assert "account" not in base.replace("accounts", "")
    cron = lambda s: re.search(r"cron\.schedule\(\s*'[^']+',\s*'([^']+)'", s).group(1)
    assert cron(mine) == cron(base)


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_job_runs_one_account_per_matrix_entry(name):
    """계정 하나당 job 하나. 계정 전용 비밀값은 접미사 규칙(THREADS_USER_ID_SABOLKKA)으로만 고른다."""
    wf = load(name)
    assert wf["on"]["workflow_dispatch"]["inputs"]["account"]["default"] == "openbrain"
    for job in wf["jobs"].values():
        assert job["strategy"]["fail-fast"] is False
        matrix = job["strategy"]["matrix"]["account"]
        assert "fromJSON" in matrix and "vars.ACCOUNTS_JSON || '[\"openbrain\"]'" in matrix   # 예약 실행은 변수에 적은 계정들, 변수가 없으면 기존 계정만
        assert job["env"]["ACCOUNT"] == "${{ matrix.account != 'openbrain' && matrix.account || '' }}"
        assert job["env"]["SFX"] == "${{ matrix.account != 'openbrain' && format('_{0}', matrix.account) || '' }}"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_account_only_secrets_never_use_plain_names(name):
    text = (WF_DIR / name).read_text(encoding="utf-8")
    from settings import ACCOUNT_ONLY_KEYS
    for key in ACCOUNT_ONLY_KEYS - {"DRY_RUN", "AUTO_PUBLISH_DISABLED", "GH_DISPATCH_TOKEN"}:
        assert f"secrets.{key}" not in text, f"{name}: {key} 는 secrets[format('{key}{{0}}', env.SFX)] 로 읽어야 한다"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_referenced_secrets_exist_in_env_example(name):
    # 오타 난 시크릿 이름은 빈 값으로 들어가 조용히 실패하므로 .env.example 의 키와 맞춰 본다
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    text = (WF_DIR / name).read_text(encoding="utf-8")
    used = set(re.findall(r"secrets\.([A-Z_]+)", text)) | set(re.findall(r"secrets\[format\('([A-Z_]+)\{0\}'", text))
    assert used <= keys, used - keys


@pytest.mark.parametrize("name", WORKFLOWS)
def test_least_privilege(name):
    # 캡처만 그림을 커밋하려고 contents: write. 대신 비밀값을 하나도 읽지 않는다
    if name == "capture.yml":
        assert load(name)["permissions"] == {"contents": "write"}
        assert "secrets" not in (WF_DIR / name).read_text(encoding="utf-8")
        return
    assert load(name)["permissions"] == {"contents": "read"}


def test_supabase_publish_cron():
    """GitHub 예약이 밀려서 Supabase pg_cron 이 publish.yml 을 30분마다 깨운다. 토큰은 저장소에 없다."""
    sql = (PROJECT_ROOT / "db" / "publish_cron.sql").read_text(encoding="utf-8")
    assert "'12,42 * * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/publish.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert not re.search(r"(github_pat_|ghp_)[A-Za-z0-9_]{20,}", sql) and "__GH_DISPATCH_TOKEN__" in sql   # 토큰 모양을 검사하는 정규식은 있어도 진짜 토큰은 없다
    # 실제 발행 여부는 여전히 저장소 변수 DRY_RUN·AUTO_PUBLISH_DISABLED 가 정한다
    job = load("publish.yml")["jobs"]["publish"]
    assert "vars[format('DRY_RUN{0}'" in job["env"]["DRY_RUN"] and "== 'false'" in job["env"]["DRY_RUN"]
    assert "if" not in job and "AUTO_PUBLISH_DISABLED" in job["env"]


def test_supabase_generate_cron():
    """생성도 Supabase pg_cron 이 20:00 KST(11:00 UTC)에 깨운다. 토큰은 publish_cron.sql 이 Vault 에 넣어 둔 것을 쓴다."""
    sql = (PROJECT_ROOT / "db" / "generate_cron.sql").read_text(encoding="utf-8")
    assert "'0 11 * * *'" in sql
    assert "/repos/neod00/threads-coupas/actions/workflows/generate.yml/dispatches" in sql
    assert '{"ref": "main", "inputs": {"dry_run": "false"}}' in sql
    assert "github_pat_" not in sql and "__GH_DISPATCH_TOKEN__" not in sql
    assert "gh_dispatch_token" in sql
    assert "AUTO_PUBLISH_DISABLED" in load("generate.yml")["jobs"]["generate"]["env"]


@pytest.mark.parametrize("name", WORKFLOWS)
def test_no_empty_string_on_the_left_of_or(name):
    """GitHub 표현식은 빈 문자열을 거짓으로 본다: `cond && '' || x` 는 cond 가 참이어도 x 가 된다 (2026-10-18 기존 계정이
    ACCOUNT=openbrain 으로 돌아 실패한 원인). 기존 계정 값을 '' 로 만들 때는 `account != 'openbrain' && x || ''` 로 쓴다."""
    text = (WF_DIR / name).read_text(encoding="utf-8")
    assert not re.search(r"&&\s*''\s*\|\|", text), "`&& '' ||` 패턴은 빈 값이 거짓이라 쓸 수 없다"


@pytest.mark.parametrize("path, schema, account", [
    ("db/publish_cron.sql", "public", None),
    ("accounts/sabolkka/db/publish_cron.sql", "sabolkka", "sabolkka"),
])
def test_publish_cron_wakes_only_when_something_is_due(path, schema, account):
    """2026-10-27: 30분마다 무조건 깨우다 비공개 저장소의 Actions 무료 시간(월 2,000분)을 다 써서 이틀 멈췄다.
    pg_cron 은 그 계정 스키마에 할 일(예약 시각이 지난 approved 글, 때가 된 링크 답글)이 있고 자동 정지가 아닐 때만 깨운다."""
    sql = (PROJECT_ROOT / path).read_text(encoding="utf-8")
    job = re.search(r"\$job\$(.*?)\$job\$", sql, re.S).group(1)
    assert f"from {schema}.posts where status = 'approved' and scheduled_at <= now()" in job
    assert "threads_reply_id is null" in job and "published_at > now() - interval '1 day'" in job
    assert f"from {schema}.system_state where key = 'auto_stop'" in job
    # 답글 대기 시간은 계정 설정(reply_delay_hours) - 발행기의 여유(REPLY_DUE_GRACE)와 같아야 한다
    from datetime import timedelta
    import publisher
    config = PROJECT_ROOT / "accounts" / account / "config.yaml" if account else PROJECT_ROOT / "config.yaml"
    delay = load_config(config).reply_delay_hours
    wait = timedelta(hours=delay) - publisher.REPLY_DUE_GRACE
    if wait > timedelta(0):
        h, m = divmod(int(wait.total_seconds()) // 60, 60)
        assert f"published_at <= now() - interval '{h} hours {m} minutes'" in job
    else:
        assert "published_at <= now() -" not in job


def test_publish_cron_refuses_a_placeholder_token():
    """2026-10-27: 자리표시자가 남은 원본을 실행해 Vault 의 토큰이 그 글자로 덮어써졌고, 모든 깨우기가 401 로 거절됐다.
    진짜 토큰 모양이 아니면 오류로 멈추고 Vault 를 건드리지 않는다. 사본을 만들 때 치환되는 자리표시자는 한 곳에만 있어야 한다."""
    sql = (PROJECT_ROOT / "db" / "publish_cron.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--")[0] for line in sql.splitlines())          # 주석 제외
    assert code.count("__GH_DISPATCH_TOKEN__") == 1
    assert "new_token text := '__GH_DISPATCH_TOKEN__'" in code
    guard = re.search(r"new_token !~ '([^']+)'", code)
    assert guard, "토큰 모양 검사가 없습니다"
    shape = re.compile(guard.group(1))
    for bad in ("__GH_DISPATCH_TOKEN__", "", "abc", "github_pat_short", "github_pat_" + "A" * 30 + "\n", " ghp_" + "A" * 30):
        assert not shape.fullmatch(bad), bad
    for good in ("github_pat_11" + "ABCdef0123" * 5, "ghp_" + "Zy9876543210" * 3):
        assert shape.fullmatch(good), good
    # 검사는 Vault 를 바꾸는 문장보다 먼저 와야 한다
    assert code.index("raise exception") < code.index("vault.create_secret") < code.index("vault.update_secret")
    assert "perform vault.create_secret(new_token" in code and "perform vault.update_secret(sid, new_token)" in code

