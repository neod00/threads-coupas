"""
내 글 답글 자동 응대 (2026-11-15 사용자 결정, sabol.kka 만. PLAN 6장 "답글 자동 응대").

규칙 6 은 그대로다: 남의 게시물에는 아무것도 달지 않는다. **내 글에 달린 답글에만** 답한다.
좋아요(하트)는 스레드 API 에 기능이 없어 하지 않는다.

- 고르는 기준과 초안 검사는 reply_drafts 와 같다 (최근 24시간, 절반쯤, 글 하나에 3개, 하루 6개,
  비난·항의·가격·건강·안전 답글은 건너뜀, 2줄·80자, 링크·멘션·이모지·금지어·경험 표현·존댓말 없음).
- 여기서 더 좁힌다
  - 답글이 달리고 MIN_AGE_MINUTES 분이 지난 것만 (바로 답하면 봇처럼 보인다)
  - 같은 사람에게는 24시간에 한 번만 (내 답글에 또 답글이 달려도 주고받기가 이어지지 않게)
  - 한 번 실행에 MAX_PER_RUN 개까지, 24시간에 MAX_PER_DAY 개까지 (연습 기록 포함)
  - 한 번 시도한 답글은 실패해도 다시 달지 않는다 (서버에서는 성공했을 수 있다 — 중복 방지)
- 두 스위치가 모두 켜져야 실제로 단다
  - config `reply_auto: true` (sabol.kka 만)
  - 저장소 Variables `REPLY_AUTO_<계정>` 이 정확히 true, 그리고 DRY_RUN 이 꺼져 있을 것
  아니면 무엇을 달았을지 기록만 남기고(연습) 일일 요약에 보여 준다.
- 자동 정지·킬 스위치 중에는 돌지 않는다 (publisher.run_once 가 그 뒤에 부른다).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Mapping

import reply_drafts
import rules
from settings import Config

log = logging.getLogger("threads_coupas")

STATE_KEY = "reply_auto_log"     # [{reply_id, post_id, username, text, draft, at, mode, media_id}]
LOG_LIMIT = 300
MIN_AGE_MINUTES = 20
MAX_PER_RUN = 2
MAX_PER_DAY = reply_drafts.MAX_PER_DAY
MAX_PER_USER_PER_DAY = 1
WINDOW_HOURS = 24


def live(env: Mapping[str, str] | None = None) -> bool:
    """REPLY_AUTO 가 정확히 'true'(대소문자 무시)일 때만 실제로 단다. 비었거나 오타면 연습."""
    import os
    env = os.environ if env is None else env
    return (env.get("REPLY_AUTO") or "").strip().lower() == "true"


def load_log(store) -> list[dict]:
    return list(store.get_state(STATE_KEY, []) or [])


def recent(entries: list[dict], now: datetime, hours: float = WINDOW_HOURS) -> list[dict]:
    since = now - timedelta(hours=hours)
    return [e for e in entries if (at := rules.as_dt(e.get("at"))) is not None and at >= since]


def due(drafts: list[dict], entries: list[dict], now: datetime) -> list[dict]:
    """이번 실행에 달 답글. drafts = reply_drafts.make_drafts 결과 (검사를 통과한 초안만)."""
    done = {e.get("reply_id") for e in entries}
    last_day = recent(entries, now)
    users = {e.get("username") for e in last_day if e.get("username")}
    room = min(MAX_PER_RUN, MAX_PER_DAY - len(last_day))
    out = []
    for d in drafts:
        if room <= 0:
            break
        at = rules.as_dt(d.get("at"))
        if d["reply_id"] in done or at is None or now - at < timedelta(minutes=MIN_AGE_MINUTES):
            continue
        if d.get("username") and d["username"] in users:
            continue
        out.append(d)
        users.add(d.get("username"))
        room -= 1
    return out


def reply_params(text: str, reply_id: str, cfg: Config) -> dict:
    """남의 답글(reply_id)에 다는 글 컨테이너. 초안 검사를 한 번 더 한다 (최종 관문)."""
    problems = reply_drafts.draft_problems(text, cfg)
    if problems or not reply_id:
        raise ValueError(f"자동 답글 검사 실패: {', '.join(problems) or '답글 ID 없음'}")
    return {"media_type": "TEXT", "text": text.strip(), "reply_to_id": str(reply_id)}


def run(store, cfg: Config, *, api, complete, now: datetime, live: bool, dry_run: bool,
        sleep: Callable[[float], None]) -> dict:
    """한 번 실행분. {"posted": n, "practice": n, "failed": n}"""
    result = {"posted": 0, "practice": 0, "failed": 0}
    if not cfg.reply_auto:
        return result
    import publisher

    drafts = reply_drafts.make_drafts(store, cfg, complete, now)
    entries = load_log(store)
    real = live and not dry_run
    for d in due(drafts, entries, now):
        entry = {"reply_id": d["reply_id"], "post_id": d.get("post_id"), "username": d.get("username"),
                 "text": d.get("text"), "permalink": d.get("permalink"), "draft": d["draft"],
                 "at": now.isoformat(), "mode": "posted" if real else "practice", "media_id": None}
        try:
            params = reply_params(d["draft"], d["reply_id"], cfg)
            if real:
                entry["media_id"] = publisher.publish_container(api, params, sleep)
                log.info(f"[자동 답글] @{d.get('username')} 에게 답글: {d['draft']}")
            else:
                log.info(f"[자동 답글 연습] @{d.get('username')} 에게 달았을 답글: {d['draft']}")
        except Exception as e:      # 답글은 덤이다. 실패해도 발행은 계속한다
            entry["mode"], entry["error"] = "failed", str(e)[:200]
            log.error(f"[자동 답글] {d['reply_id']} 실패 (다시 시도하지 않습니다): {e}")
        entries.append(entry)
        store.set_state(STATE_KEY, entries[-LOG_LIMIT:])      # 하나 달 때마다 기록 (중간에 죽어도 중복 없음)
        result[entry["mode"]] += 1
    return result


def section(store, now: datetime) -> list[str]:
    """일일 요약에 넣을 줄: 지난 24시간 자동 답글."""
    entries = recent(load_log(store), now)
    if not entries:
        return []
    posted = sum(e.get("mode") == "posted" for e in entries)
    practice = sum(e.get("mode") == "practice" for e in entries)
    failed = sum(e.get("mode") == "failed" for e in entries)
    head = f"자동 답글 (지난 24시간) 단 것 {posted}개"
    if practice:
        head += f", 연습 {practice}개 (REPLY_AUTO 꺼짐 — 실제로는 안 달림)"
    if failed:
        head += f", 실패 {failed}개"
    lines = [head]
    for i, e in enumerate(entries, start=1):
        mark = {"posted": "", "practice": "[연습] ", "failed": "[실패] "}.get(e.get("mode"), "")
        said = " ".join((e.get("text") or "").split())[:50]
        lines.append(f"{i}) {mark}@{e.get('username') or '?'}: {said}")
        lines.append("   → " + (e.get("draft") or "").replace("\n", " / "))
        if e.get("permalink"):
            lines.append(f"   {e['permalink']}")
    return lines


def logged_ids(store) -> set[str]:
    return {e.get("reply_id") for e in load_log(store)}
