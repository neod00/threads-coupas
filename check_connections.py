"""
외부 연결 점검 (Step 7 준비). .env 에 넣은 값으로 Supabase·텔레그램에 실제로 붙는지 확인한다. 키 값은 출력하지 않는다.

  python check_connections.py

- Supabase: db/schema.sql 로 만든 테이블 5개를 각각 읽어 본다 (쓰지 않는다)
- 텔레그램: 봇 토큰 확인(getMe) 후 TELEGRAM_CHAT_ID 로 테스트 메시지 1개를 보낸다
"""
from __future__ import annotations

import json
import sys
import urllib.request

from settings import ConfigError, load_secrets

TABLES = ("posts", "metrics_post", "metrics_coupang_daily", "system_state", "events")


def check_supabase(secrets) -> bool:
    try:
        secrets.require("supabase_url", "supabase_service_role_key")
        from supabase import create_client
        db = create_client(secrets.supabase_url, secrets.supabase_service_role_key)
    except Exception as e:
        print(f"[Supabase] 연결 준비 실패: {e}")
        return False
    ok = True
    for table in TABLES:
        try:
            db.table(table).select("*").limit(1).execute()
            print(f"[Supabase] {table}: OK")
        except Exception as e:
            ok = False
            print(f"[Supabase] {table}: 실패 — {str(e)[:200]}")
    if not ok:
        print("  → SQL Editor 에서 db/schema.sql 을 실행했는지, URL·키가 같은 프로젝트 것인지 확인하세요.")
    return ok


def check_telegram(secrets) -> bool:
    try:
        secrets.require("telegram_bot_token", "telegram_chat_id")
    except ConfigError as e:
        print(f"[텔레그램] {e}")
        return False
    base = f"https://api.telegram.org/bot{secrets.telegram_bot_token}"
    try:
        with urllib.request.urlopen(f"{base}/getMe", timeout=15) as resp:
            me = json.loads(resp.read().decode("utf-8"))["result"]
        print(f"[텔레그램] 봇 토큰 OK: @{me['username']}")
    except Exception as e:
        print(f"[텔레그램] 봇 토큰 확인 실패 (토큰을 다시 복사하세요): {e}")
        return False
    try:
        from notify import TelegramNotifier
        TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id).send(
            "[연결 테스트] 스레드 × 쿠팡 테스트 봇입니다. 이 메시지가 보이면 알림 설정이 끝났습니다.")
        print("[텔레그램] 테스트 메시지 전송 OK — 텔레그램에서 메시지를 확인하세요")
        return True
    except Exception as e:
        print(f"[텔레그램] 메시지 전송 실패 (채팅 ID 확인, 봇에게 먼저 /start 를 보냈는지 확인): {e}")
        return False


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        secrets = load_secrets()
    except ConfigError as e:
        print(f"설정 오류: {e}")
        return 1
    results = [check_supabase(secrets), check_telegram(secrets)]
    print("모두 OK" if all(results) else "실패한 항목이 있습니다")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
