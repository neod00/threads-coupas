"""
.env.<계정> 의 계정 전용 값을 GitHub Secrets 로 등록한다 (값은 화면·로그에 출력하지 않는다).

  python tools/set_github_secrets.py <계정> [--repo neod00/threads-coupas] [--dry-run]

등록하는 Secrets (이름 + _<계정 대문자>):
  THREADS_USER_ID, THREADS_ACCESS_TOKEN, THREADS_TOKEN_ISSUED_AT, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
하지 않는 것: 저장소 변수(DRY_RUN_<계정>, AUTO_PUBLISH_DISABLED_<계정>, ACCOUNTS_JSON). 실제 발행 스위치는 사람이 직접 켠다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYS = ("THREADS_USER_ID", "THREADS_ACCESS_TOKEN", "THREADS_TOKEN_ISSUED_AT", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")


def secret_name(key: str, account: str) -> str:
    return f"{key}_{account.upper()}"


def missing_keys(values: dict) -> list[str]:
    return [k for k in KEYS if not (values.get(k) or "").strip()]


def register(account: str, repo: str, *, dry_run: bool = False, runner=subprocess.run, env_dir: Path = PROJECT_ROOT) -> list[str]:
    """등록한(또는 dry_run 이면 등록할) Secret 이름 목록. 값이 비어 있으면 아무것도 등록하지 않고 ValueError."""
    from dotenv import dotenv_values

    path = env_dir / f".env.{account}"
    if not path.is_file():
        raise ValueError(f"{path.name} 가 없습니다 (tools/new_account.py 로 먼저 만드세요)")
    values = dotenv_values(path)
    missing = missing_keys(values)
    if missing:
        raise ValueError(f"{path.name} 에 값이 비어 있습니다: {', '.join(missing)}")
    done = []
    for key in KEYS:
        name = secret_name(key, account)
        if not dry_run:
            result = runner(["gh", "secret", "set", name, "--repo", repo], input=values[key].strip(),
                            text=True, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(f"{name} 등록 실패: {(result.stderr or '').strip()[:200]}")
        done.append(name)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=".env.<계정> → GitHub Secrets")
    ap.add_argument("account")
    ap.add_argument("--repo", default="neod00/threads-coupas")
    ap.add_argument("--dry-run", action="store_true", help="등록하지 않고 이름만 보여 준다")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        names = register(args.account, args.repo, dry_run=args.dry_run)
    except (ValueError, RuntimeError) as e:
        print(f"실패: {e}")
        return 1
    for n in names:
        print(("(연습) " if args.dry_run else "등록: ") + n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
