"""
Threads 장기 토큰 자동 연장 (2026-11-26, refresh_token.yml 매주 월요일).
11-26 openbrain 토큰이 60일을 채워 만료됐고, 만료된 토큰은 연장할 수 없어 사람이 새로 발급해야 했다.
만료 전에 GET /refresh_access_token (grant_type=th_refresh_token) 으로 60일을 다시 채운다 (THREADS_API_NOTES.md).

  python refresh_token.py --out <새 토큰을 쓸 파일> [--dry-run]

- 발급 후 REFRESH_AFTER_DAYS 일이 지났을 때만 연장한다 (Meta 는 발급 24시간 뒤부터 받는다. 매주 돌므로 2~3주마다 한 번)
- 새 토큰은 화면에 출력하지 않는다. --out 파일에만 쓰고, GitHub Actions 에서는 로그 가림(::add-mask::)을 먼저 건다
- 저장(GitHub Secrets)은 워크플로가 한다. 이 스크립트는 연장했으면 GITHUB_OUTPUT 에 refreshed=true 를 남긴다
- 이미 만료됐으면 연장할 수 없으므로 실패(1)로 끝내 알림이 가게 한다
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from settings import ConfigError, _parse_date, today_kst, token_status

API = "https://graph.threads.net/refresh_access_token"
REFRESH_AFTER_DAYS = 14

log = logging.getLogger("threads_coupas")


def decide(issued_at: date | None, today: date) -> tuple[str, str]:
    """(refresh | skip | expired, 설명). 발급일을 모르면 연장해서 발급일을 새로 적는다."""
    status = token_status(issued_at, today)
    if status.level == "expired":
        return "expired", status.message + " 만료된 토큰은 연장할 수 없어 새로 발급해야 합니다."
    if issued_at is None or issued_at > today:
        return "refresh", f"{status.message} 연장해서 발급일을 오늘로 다시 적습니다."
    age = (today - issued_at).days
    if age < REFRESH_AFTER_DAYS:
        return "skip", f"{status.message} 발급 {age}일째라 아직 연장하지 않습니다 ({REFRESH_AFTER_DAYS}일 뒤부터)."
    return "refresh", f"{status.message} 발급 {age}일째라 연장합니다."


def refresh(http, token: str) -> tuple[str, int]:
    """(새 토큰, 유효 일수). 실패하면 RuntimeError (메시지에 토큰을 넣지 않는다)."""
    resp = http.get(API, params={"grant_type": "th_refresh_token", "access_token": token}, timeout=30)
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or not data.get("access_token"):
        message = (data.get("error") or {}).get("message", "") if isinstance(data, dict) else ""
        raise RuntimeError(f"토큰 연장 실패 (HTTP {resp.status_code}) {message}".strip())
    return data["access_token"], int(data.get("expires_in", 0)) // 86400


def main(argv=None, http=None, env=None, today: date | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Threads 장기 토큰 자동 연장")
    ap.add_argument("--out", required=True, help="새 토큰을 쓸 파일 (화면에는 출력하지 않는다)")
    ap.add_argument("--dry-run", action="store_true", help="연장할지 판단만 하고 API 를 부르지 않는다")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env

    token = (env.get("THREADS_ACCESS_TOKEN") or "").strip()
    if not token:
        log.error("THREADS_ACCESS_TOKEN 이 비어 있습니다.")
        return 1
    try:
        raw = (env.get("THREADS_TOKEN_ISSUED_AT") or "").strip()
        issued_at = _parse_date(raw, "THREADS_TOKEN_ISSUED_AT") if raw else None
    except ConfigError as e:
        log.warning(f"{e} — 연장해서 발급일을 새로 적습니다.")
        issued_at = None
    today = today or today_kst()

    action, reason = decide(issued_at, today)
    if action == "expired":
        log.error(reason)
        return 1
    log.info(reason)
    if action == "skip":
        return 0
    if args.dry_run:
        log.info("[DRY_RUN] 연장 API 를 부르지 않습니다.")
        return 0

    if http is None:
        import requests as http
    try:
        new_token, days = refresh(http, token)
    except Exception as e:      # 네트워크 오류 포함. 예외 메시지에 토큰이 들어가지 않게 요청 URL 은 출력하지 않는다
        log.error(e if isinstance(e, RuntimeError) else f"토큰 연장 실패: {type(e).__name__}")
        return 1
    if env.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{new_token}", flush=True)       # 이후 로그에 새 토큰이 나와도 가려진다
    Path(args.out).write_text(new_token, encoding="utf-8")
    log.info(f"토큰을 연장했습니다 (약 {days}일 유효, 새 만료일 약 {today + timedelta(days=days or 60)}).")
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"refreshed=true\nissued_at={today.isoformat()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
