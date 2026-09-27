"""
답글 감시 (PLAN v2 Step 8, 규칙 10 조건 1). publish.yml 이 발행 전에 먼저 실행한다.

최근 72시간 안에 발행한 내 글의 답글을 읽어 "스팸", "신고", "광고충", "사기", "허위" 가 있으면
킬 스위치와 같이 발행을 멈추고 알림을 보낸다. 답글에 자동으로 대응하는 기능은 없다 (규칙 6).
- 내가 단 답글(링크 답글)은 보지 않는다 (is_reply_owned_by_me)
- 한 번 본 답글은 system_state 에 기록해 다시 판정하지 않는다
  (그러지 않으면 사람이 정지를 해제해도 같은 답글 때문에 매시간 다시 멈춘다)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import autostop
import rules

log = logging.getLogger("threads_coupas")

WATCH_HOURS = 72
SEEN_KEY = "seen_reply_ids"
SEEN_LIMIT = 5000


def watch(store, api, notifier, now: datetime) -> dict:
    since = now - timedelta(hours=WATCH_HOURS)
    posts = [p for p in store.list_posts(["published"])
             if p.get("threads_media_id") and rules.as_dt(p.get("published_at")) and rules.as_dt(p["published_at"]) >= since]
    seen = list(store.get_state(SEEN_KEY, []) or [])
    seen_set = set(seen)
    checked, stopped = 0, False
    for post in posts:
        try:
            replies = api.conversation(post["threads_media_id"])
        except Exception as e:
            log.error(f"[답글 감시] {post['id'][:8]} 답글 읽기 실패: {e}")
            continue
        for reply in replies:
            rid = reply.get("id")
            if not rid or rid in seen_set or reply.get("is_reply_owned_by_me"):
                continue
            seen.append(rid)
            seen_set.add(rid)
            checked += 1
            if autostop.check_reply_text(store, notifier, reply.get("text") or "",
                                         {"post_id": post["id"], "reply_id": rid, "username": reply.get("username")}):
                stopped = True
    store.set_state(SEEN_KEY, seen[-SEEN_LIMIT:])
    log.info(f"[답글 감시] 글 {len(posts)}개, 새 답글 {checked}개{' — 자동 정지' if stopped else ''}")
    return {"posts": len(posts), "new_replies": checked, "stopped": stopped}
