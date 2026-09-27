"""
알림 (일일 요약·자동 정지). 텔레그램 봇으로 보낸다. 테스트·dry_run 에서는 메시지를 모으기만 하는 알림기를 쓴다.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("threads_coupas")

TELEGRAM_LIMIT = 4000       # 한 메시지 최대 4096자. 여유를 둔다


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """줄 단위로 limit 이하 조각으로 나눈다. 한 줄이 limit 보다 길면 그 줄을 자른다."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        for chunk in split_message(text):
            body = urllib.parse.urlencode({"chat_id": self.chat_id, "text": chunk,
                                           "disable_web_page_preview": "true"}).encode()
            with urllib.request.urlopen(urllib.request.Request(self.url, data=body), timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"텔레그램 전송 실패: {result.get('description')}")


class CollectingNotifier:
    """보내지 않고 모은다 (테스트, dry_run). print_to_log=True 면 로그에도 남긴다."""

    def __init__(self, print_to_log: bool = False):
        self.sent: list[str] = []
        self.print_to_log = print_to_log

    def send(self, text: str) -> None:
        self.sent.append(text)
        if self.print_to_log:
            log.info("[알림 dry_run]\n" + text)


def make_notifier(secrets, dry_run: bool = False):
    if dry_run or not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        if not dry_run:
            log.warning("텔레그램 설정이 없어 알림을 로그로만 남깁니다 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
        return CollectingNotifier(print_to_log=True)
    return TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id)
