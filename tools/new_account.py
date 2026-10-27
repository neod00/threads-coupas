"""
새 Threads 계정 폴더 만들기 (ACCOUNTS.md, .claude/skills/threads-new-account).

  python tools/new_account.py <계정> [--tone casual|polite] [--force]

만드는 것 (이미 있으면 거부, --force 면 덮어씀):
  accounts/<계정>/config.yaml            설정 (하루 3개, 링크 답글 3시간 뒤, 운영 알림 켬, talk_topics 는 비어 있음)
  accounts/<계정>/prompts/*.md           casual 이면 반말 "사라 vs 마라" 프롬프트, polite 면 만들지 않음(기존 계정 것을 씀)
  accounts/<계정>/data/products.csv      머리글만 (상품을 채우기 전에는 설정 점검이 실패해 발행이 막힌다)
  accounts/<계정>/db/schema.sql          <계정> 스키마 (기존 Supabase 프로젝트 안)
  accounts/<계정>/db/{publish,generate,digest}_cron.sql   예약 SQL (계정 전용 이름, account=<계정>)
  .env.<계정>                            비밀값 빈 틀 (DRY_RUN=true, AUTO_PUBLISH_DISABLED=true)

아무것도 켜지 않는다: 변수(DRY_RUN_<계정>, ACCOUNTS_JSON)·시크릿·DB 는 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
RESERVED = ("openbrain",)

PRODUCT_HEADER = "product_id,name,category,power_w,default_hours,link,note\n"

ENV_TEMPLATE = """# {name} 전용 값. 공용 .env 의 LLM 키·텔레그램·앱 시크릿은 자동으로 같이 읽는다. 커밋하지 않는다.
# 사용: PowerShell 에서  $env:ACCOUNT="{name}"; python settings.py
THREADS_USER_ID=
THREADS_ACCESS_TOKEN=
THREADS_TOKEN_ISSUED_AT=
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
GH_DISPATCH_TOKEN=
AUTO_PUBLISH_DISABLED=true
DRY_RUN=true
"""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def render_schema(name: str, root: Path = PROJECT_ROOT) -> str:
    base = _read(root / "db" / "schema.sql")
    head = (f"-- {name} 계정용 스키마. 기존 프로젝트(기존 계정과 같은 프로젝트)의 SQL Editor 에서 한 번 실행한다.\n"
            "-- 테이블은 db/schema.sql 과 같고(최신 posts_type_check 포함) public 이 아니라 "
            f"{name} 스키마에 만든다.\n"
            f"-- 실행 후 Project Settings → API → Exposed schemas 에 {name} 를 추가해야 서버 코드가 접근할 수 있다.\n\n"
            f"create schema if not exists {name};\n"
            f"set search_path to {name};\n\n")
    tail = (f"\n-- service role 만 접근한다. anon·authenticated 에는 아무 권한도 주지 않는다 (RLS 도 켜져 있다).\n"
            f"grant usage on schema {name} to service_role;\n"
            f"grant all on all tables in schema {name} to service_role;\n"
            f"grant all on all sequences in schema {name} to service_role;\n"
            f"alter default privileges in schema {name} grant all on tables to service_role;\n"
            f"alter default privileges in schema {name} grant all on sequences to service_role;\n\n"
            "reset search_path;\n")
    return head + base.split("\n", 2)[2].rstrip() + "\n" + tail


def _account_cron(kind: str, name: str, root: Path) -> str:
    """generate·digest 예약: 기존 SQL 에서 예약 이름과 깨울 때 넘기는 입력만 계정용으로 바꾼다."""
    text = _read(root / "db" / f"{kind}_cron.sql")
    text = text.replace(f"threads-coupas-{kind}'", f"threads-coupas-{name}-{kind}'")
    old_body = '"inputs": {"dry_run": "false"}'
    if old_body not in text:
        raise ValueError(f"db/{kind}_cron.sql 의 깨우기 입력 형식이 바뀌었습니다")
    text = text.replace(old_body, f'"inputs": {{"account": "{name}", "dry_run": "false"}}')
    header = (f"-- {name} 계정용. 기존 계정과 같은 Supabase 프로젝트의 SQL Editor 에서 실행한다. "
              f"예약 이름이 {name} 전용이고 account={name} 로 깨운다.\n")
    return header + text


def render_cron(kind: str, name: str, root: Path = PROJECT_ROOT) -> str:
    if kind in ("generate", "digest"):
        return _account_cron(kind, name, root)
    if kind != "publish":
        raise ValueError(kind)
    # 발행 예약은 기존 publish_cron.sql 이 Vault 에 토큰을 넣는 코드를 포함하므로, 토큰을 건드리지 않는 generate 형식에서 만든다
    text = _account_cron("generate", name, root)
    text = (text.replace(f"threads-coupas-{name}-generate", f"threads-coupas-{name}-publish")
                .replace("'0 11 * * *'", "'12,42 * * * *'").replace("generate.yml", "publish.yml"))
    start = text.index("-- 생성 깨우기 예약")
    end = text.index("-- 실행 방법")
    middle = ("-- 발행 깨우기 예약 (Supabase pg_cron → GitHub publish.yml 수동 실행, 매시 12분·42분에 할 일이 있을 때만)\n"
              "-- 토큰은 db/publish_cron.sql 을 실행할 때 Vault 에 저장한 gh_dispatch_token 을 그대로 쓴다. 이 파일에는 토큰이 없다.\n"
              f"-- 실제 발행 여부는 저장소 변수 DRY_RUN_{name.upper()}·AUTO_PUBLISH_DISABLED_{name.upper()} 가 정한다.\n--\n")
    text = text[:start] + middle + text[end:]
    call_end = "\n  );\n  $job$"
    if text.count(call_end) != 1:
        raise ValueError("db/generate_cron.sql 의 예약 본문 형식이 바뀌었습니다")
    return text.replace(call_end, "\n" + publish_gate(name, _template_reply_delay()) + "\n  $job$")


def _template_reply_delay() -> float:
    import yaml
    return float(yaml.safe_load(_read(TEMPLATES / "config.yaml")).get("reply_delay_hours") or 0)


def publish_gate(schema: str, reply_delay_hours: float) -> str:
    """발행 예약이 할 일이 있을 때만 깨우게 하는 조건 (2026-10-27: 30분마다 무조건 깨우다 Actions 무료 시간을 다 씀).
    답글 대기는 reply_delay_hours 에서 발행기의 여유(publisher.REPLY_DUE_GRACE, 15분)를 뺀 값."""
    wait = int(reply_delay_hours * 60) - 15
    reply_wait = (f"\n                   and published_at <= now() - interval '{wait // 60} hours {wait % 60} minutes'"
                  f"   -- reply_delay_hours {reply_delay_hours:g}시간 - 15분 여유 (publisher.REPLY_DUE_GRACE)") if wait > 0 else ""
    return (
        "  )\n"
        "  -- 할 일이 있을 때만 깨운다 (2026-10-27: 30분마다 무조건 깨우다 GitHub Actions 무료 시간을 다 써서 이틀 멈춤).\n"
        "  -- 1) 예약 시각이 지난 approved 글  2) 때가 된 링크 답글(답글만 실패한 글 포함)\n"
        "  -- 자동 정지 중이면 깨우지 않는다. 답글 감시는 publish.yml 의 4시간 예비 실행이 맡는다.\n"
        "  where (\n"
        f"      exists (select 1 from {schema}.posts where status = 'approved' and scheduled_at <= now())\n"
        f"      or exists (select 1 from {schema}.posts\n"
        "                 where status = 'published' and variant = 'link_in_reply'\n"
        "                   and threads_media_id is not null and threads_reply_id is null\n"
        "                   and published_at > now() - interval '1 day'   -- 하루 넘게 못 단 답글로는 깨우지 않는다 (계속 실패하면 무료 시간만 씀)"
        f"{reply_wait}\n"
        "                 )\n"
        "    )\n"
        f"    and not coalesce((select (value->>'active')::boolean from {schema}.system_state where key = 'auto_stop'), false);"
    )


def create_account(name: str, *, tone: str = "casual", root: Path = PROJECT_ROOT, force: bool = False) -> list[Path]:
    import re
    sys.path.insert(0, str(PROJECT_ROOT))
    from settings import ACCOUNT_NAME_RE
    if not re.fullmatch(ACCOUNT_NAME_RE, name) or name in RESERVED:
        raise ValueError(f"계정 이름이 올바르지 않습니다: {name!r} (소문자로 시작, 소문자·숫자·밑줄 2~31자, {RESERVED} 제외)")
    if tone not in ("casual", "polite"):
        raise ValueError("tone 은 casual 또는 polite")
    folder = root / "accounts" / name
    if folder.exists() and not force:
        raise FileExistsError(f"{folder} 가 이미 있습니다 (--force 로 덮어쓸 수 있습니다)")

    files: dict[Path, str] = {}
    config = _read(TEMPLATES / "config.yaml")
    if tone == "polite":
        config = config.replace("tone: casual", "tone: polite")
    files[folder / "config.yaml"] = config
    if tone == "casual":
        for src in sorted((TEMPLATES / "prompts_casual").glob("*.md")):
            files[folder / "prompts" / src.name] = _read(src)
    files[folder / "data" / "products.csv"] = PRODUCT_HEADER
    files[folder / "db" / "schema.sql"] = render_schema(name, root)
    for kind in ("publish", "generate", "digest"):
        files[folder / "db" / f"{kind}_cron.sql"] = render_cron(kind, name, root)
    env = root / f".env.{name}"
    if not env.exists() or force:
        files[env] = ENV_TEMPLATE.format(name=name)

    written = []
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="새 Threads 계정 폴더 만들기")
    ap.add_argument("name")
    ap.add_argument("--tone", choices=("casual", "polite"), default="casual")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        written = create_account(args.name, tone=args.tone, force=args.force)
    except (ValueError, FileExistsError) as e:
        print(f"실패: {e}")
        return 1
    for p in written:
        print("-", p.relative_to(PROJECT_ROOT).as_posix())
    print("\n다음: .claude/skills/threads-new-account/SKILL.md 의 2단계부터 진행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
