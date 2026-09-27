"""
스레드 인사이트 수집 (PLAN v2 Step 8, metrics.yml 매일 09:00 KST).

발행 후 24시간·72시간이 지난 글의 인사이트를 한 번씩 저장한다 (metrics_post, 체크포인트별 1행).
metric: views, likes, replies, reposts, quotes, shares (THREADS_API_NOTES.md). views·shares 는 비어 올 수 있어 null 로 둔다.
Threads 는 읽기만 한다. 킬 스위치와 상관없이 돈다 (발행 작업이 아니다).

실행:
  python metrics.py            # 수집해서 저장
  python metrics.py --dry-run  # 수집만 하고 저장하지 않음
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import rules

log = logging.getLogger("threads_coupas")

CHECKPOINTS = (24, 72)


def due_checkpoints(posts, done: set, now: datetime) -> list[tuple[dict, int]]:
    """(글, 체크포인트) 목록. 발행 후 그 시간이 지났고 아직 저장하지 않은 것만."""
    out = []
    for p in posts:
        published = rules.as_dt(p.get("published_at"))
        if not p.get("threads_media_id") or published is None:
            continue
        elapsed = (now - published).total_seconds() / 3600
        for hours in CHECKPOINTS:
            if elapsed >= hours and (p["threads_media_id"], hours) not in done:
                out.append((p, hours))
    return out


def collect(store, api, now: datetime, save: bool = True) -> list[dict]:
    posts = store.list_posts(["published"])
    done = store.metric_checkpoints([p["threads_media_id"] for p in posts if p.get("threads_media_id")])
    rows = []
    for post, hours in due_checkpoints(posts, done, now):
        try:
            values = api.insights(post["threads_media_id"])
        except Exception as e:      # 한 글이 실패해도 나머지는 모은다
            log.error(f"[지표] {post['id']} {hours}시간 수집 실패: {e}")
            continue
        row = {"threads_media_id": post["threads_media_id"], "hours_since_publish": hours,
               "collected_at": now.astimezone(timezone.utc).isoformat(), **values}
        rows.append(row)
        if save:
            store.insert_metric(row)
        log.info(f"[지표] {post['id'][:8]} {hours}시간: {values}")
    return rows


def main() -> int:
    from publisher import ThreadsApi
    from settings import ConfigError, load_secrets, log_token_status
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="발행 24·72시간 뒤 인사이트 수집")
    ap.add_argument("--dry-run", action="store_true", help="수집만 하고 저장하지 않음")
    args = ap.parse_args()
    try:
        secrets = load_secrets()
        secrets.require("threads_user_id", "threads_access_token")
        store = open_store(secrets)
    except ConfigError as e:
        log.error(f"설정 오류: {e}")
        return 1
    log_token_status(secrets.threads_token_issued_at)
    rows = collect(store, ThreadsApi(secrets.threads_user_id, secrets.threads_access_token),
                   datetime.now(timezone.utc), save=not args.dry_run)
    log.info(f"수집 {len(rows)}건{' (저장 안 함)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
