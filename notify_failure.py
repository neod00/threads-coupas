"""
워크플로 실패 알림 (2026-11-18). GitHub Actions 에서 실패한 단계 뒤에 `if: failure()` 로 부른다.
11-17 sabol.kka 생성이 DB 제약 위반으로 멈췄는데 텔레그램 알림이 없어 11-18 글이 통째로 빠졌다.

  python notify_failure.py --job 생성 --log run.log --url <실행 기록 주소> [--note "..."]

알림이 실패해도 이 스크립트는 항상 0 으로 끝난다 (원래 실패를 덮지 않는다).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ERROR_LINE = re.compile(r"\b\w*(Error|Exception)\b|오류|실패")
ERROR_CHARS = 300


def last_error(log_text: str, limit: int = ERROR_CHARS) -> str:
    """로그에서 마지막 오류 줄. 없으면 마지막 줄."""
    lines = [line.strip() for line in log_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if ERROR_LINE.search(line):
            return line[:limit]
    return lines[-1][:limit] if lines else "로그가 비어 있음"


def message(job: str, error: str, url: str, note: str = "") -> str:
    lines = [f"[실패] {job} 작업이 멈췄습니다."]
    if note:
        lines.append(note)
    lines += [f"오류: {error}", f"기록: {url}"]
    return "\n".join(lines)


def main(argv=None, notifier=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--log", help="실패한 단계의 출력을 담은 파일")
    ap.add_argument("--url", default="")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    log_path = Path(args.log) if args.log else None
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.exists() else ""
    body = message(args.job, last_error(text), args.url, args.note)
    if notifier is None:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            print("텔레그램 설정이 없어 실패 알림을 보내지 않습니다\n" + body)
            return 0
        from notify import TelegramNotifier
        notifier = TelegramNotifier(token, chat)
    try:
        notifier.send(body)
    except Exception as e:      # 알림 실패로 원래 실패를 가리지 않는다
        print(f"실패 알림 전송 실패: {e}\n{body}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
