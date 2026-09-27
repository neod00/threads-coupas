"""
Threads 토큰 정리 도구. .env 에 받은 토큰을 넣고 실행하면 나머지를 채운다. 토큰 값은 화면에 출력하지 않는다.

  준비: .env 에 THREADS_ACCESS_TOKEN(대시보드·Graph API Explorer 에서 받은 토큰), THREADS_APP_SECRET 을 넣는다
  실행: python threads_token.py

  1) 단기 토큰(1시간)이면 장기 토큰(60일)으로 바꾼다 (grant_type=th_exchange_token, THREADS_API_NOTES.md)
     이미 장기 토큰이면 교환이 거절되므로 그대로 쓴다
  2) GET /me 로 THREADS_USER_ID(숫자)와 계정 이름을 확인한다
  3) .env 의 THREADS_ACCESS_TOKEN·THREADS_USER_ID·THREADS_TOKEN_ISSUED_AT(오늘, KST)을 갱신한다
"""
from __future__ import annotations

import sys

from settings import ENV_PATH, today_kst

API = "https://graph.threads.net"


def exchange_long_lived(http, token: str, app_secret: str) -> tuple[str | None, str]:
    """(장기 토큰 또는 None, 설명). 이미 장기 토큰이면 Meta 가 교환을 거절한다."""
    resp = http.get(f"{API}/access_token", params={
        "grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": token}, timeout=30)
    data = resp.json() if resp.content else {}
    if resp.status_code == 200 and data.get("access_token"):
        days = int(data.get("expires_in", 0)) // 86400
        return data["access_token"], f"단기 토큰을 장기 토큰으로 바꿨습니다 (약 {days}일 유효)"
    message = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
    return None, f"교환하지 않고 그대로 씁니다 (이미 장기 토큰이거나 교환 불가: {message})"


def fetch_me(http, token: str) -> dict:
    resp = http.get(f"{API}/v1.0/me", params={"fields": "id,username", "access_token": token}, timeout=30)
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or "id" not in data:
        message = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
        raise RuntimeError(f"토큰으로 계정 정보를 읽지 못했습니다: {message}")
    return data


def run(http, env: dict, env_path=ENV_PATH, today=None) -> list[str]:
    """실행 결과 설명 목록. .env 에 THREADS_* 3개를 쓴다."""
    from dotenv import set_key

    token = (env.get("THREADS_ACCESS_TOKEN") or "").strip()
    secret = (env.get("THREADS_APP_SECRET") or "").strip()
    if not token:
        raise RuntimeError(".env 의 THREADS_ACCESS_TOKEN 이 비어 있습니다. 받은 토큰을 먼저 넣으세요.")
    notes = []
    if secret:
        long_token, note = exchange_long_lived(http, token, secret)
        notes.append(note)
        token = long_token or token
    else:
        notes.append("THREADS_APP_SECRET 이 없어 장기 토큰 교환을 건너뜁니다 (받은 토큰이 장기 토큰이면 괜찮습니다)")
    me = fetch_me(http, token)
    issued = (today or today_kst()).isoformat()
    set_key(str(env_path), "THREADS_ACCESS_TOKEN", token, quote_mode="never")
    set_key(str(env_path), "THREADS_USER_ID", str(me["id"]), quote_mode="never")
    set_key(str(env_path), "THREADS_TOKEN_ISSUED_AT", issued, quote_mode="never")
    notes.append(f"계정 @{me.get('username')} 확인, THREADS_USER_ID 저장")
    notes.append(f"THREADS_TOKEN_ISSUED_AT={issued} 저장 (60일 뒤 만료, 10일 전부터 경고)")
    return notes


def main() -> int:
    import requests
    from dotenv import dotenv_values

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        for note in run(requests, dotenv_values(ENV_PATH)):
            print("- " + note)
    except Exception as e:
        print(f"실패: {e}")
        return 1
    print("완료. 토큰 값은 .env 에만 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
