"""
사람이 하는 운영 조작 (PLAN v2 Step 5). cancel_post.yml 과 monitor_app.py 가 같은 함수를 쓴다.

- cancel_post  : 예정 글(approved, 아직 발행 전) → cancelled
- swap_hook    : held 글의 훅을 다른 후보로 교체 (코드 가드 재통과 필요)
- manual_approve: held 글 수동 승인. 사람이 검수자 역할을 하지만 코드 가드는 다시 통과해야 한다 → 다음 빈 슬롯 배정
- discard      : held·draft 글 폐기 → cancelled

실행 (cancel_post.yml):
  python actions.py cancel <post_id>
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import guard
import rules
from settings import Config


class ActionError(ValueError):
    pass


def _get(store, post_id: str) -> dict:
    post = store.get_post(post_id.strip())
    if not post:
        raise ActionError(f"post_id 를 찾을 수 없습니다: {post_id}")
    return post


def cancel_post(store, post_id: str, by: str = "cancel_post", dry_run: bool = False) -> dict:
    """dry_run 이면 취소할 수 있는지만 확인하고 바꾸지 않는다."""
    post = _get(store, post_id)
    if post["status"] == "published" or post.get("threads_media_id"):
        raise ActionError("이미 발행된 글은 취소할 수 없습니다 (스레드 앱에서 직접 삭제하세요)")
    if post["status"] != "approved":
        raise ActionError(f"예정 글(approved)만 취소할 수 있습니다. 현재 상태: {post['status']}")
    if dry_run:
        return post
    updated = store.update_post(post["id"], {"status": "cancelled"})
    store.log_event("cancel_post", {"post_id": post["id"], "scheduled_at": post.get("scheduled_at"), "by": by})
    return updated


def discard(store, post_id: str, by: str) -> dict:
    post = _get(store, post_id)
    if post["status"] not in ("held", "draft"):
        raise ActionError(f"held·draft 글만 폐기할 수 있습니다. 현재 상태: {post['status']}")
    updated = store.update_post(post["id"], {"status": "cancelled"})
    store.log_event("discard", {"post_id": post["id"], "by": by})
    return updated


def _hook_index(post: dict) -> int:
    lines = (post.get("text") or "").split("\n")
    return 1 if lines and lines[0].lstrip().startswith("[광고]") else 0     # 예전 형식만 첫 줄이 대가성 문구


def swap_hook(store, cfg: Config, post_id: str, style: str) -> tuple[dict, list[str]]:
    """훅을 hook_candidates 의 style 후보로 바꾼다. (갱신된 글, 가드 문제). 가드에 걸리면 저장하지 않는다."""
    post = _get(store, post_id)
    if post["status"] not in ("held", "draft"):
        raise ActionError(f"held·draft 글만 훅을 바꿀 수 있습니다. 현재 상태: {post['status']}")
    candidate = next((h for h in post.get("hook_candidates") or [] if h["style"] == style), None)
    if not candidate:
        raise ActionError(f"'{style}' 훅 후보가 없습니다")
    lines = post["text"].split("\n")
    lines[_hook_index(post)] = candidate["text"]
    trial = dict(post, text="\n".join(lines), hook_style=style)
    problems = guard.check_post(trial, cfg)
    if problems:
        return post, problems
    return store.update_post(post["id"], {"text": trial["text"], "hook_style": style}), []


def manual_approve(store, cfg: Config, post_id: str, by: str, now: datetime | None = None) -> dict:
    """held 글 수동 승인. 코드 가드 재통과 + 규칙 5 에 맞는 다음 빈 슬롯 배정."""
    if not (by or "").strip():
        raise ActionError("승인한 사람을 입력해야 합니다")
    post = _get(store, post_id)
    if post["status"] not in ("held", "draft"):
        raise ActionError(f"held·draft 글만 수동 승인할 수 있습니다. 현재 상태: {post['status']}")
    problems = guard.check_post(post, cfg)
    if problems:
        raise ActionError("코드 가드를 통과하지 못해 승인할 수 없습니다: " + " / ".join(problems))
    now = now or datetime.now(timezone.utc)
    slot = rules.next_slot(store.list_posts(rules.OCCUPYING), post["has_link"], cfg, after=now)
    if slot is None:
        raise ActionError("7일 안에 빈 슬롯이 없습니다")
    updated = store.update_post(post["id"], {"status": "approved", "scheduled_at": slot.astimezone(timezone.utc).isoformat()})
    store.log_event("manual_approve", {"post_id": post["id"], "by": by.strip(), "scheduled_at": updated["scheduled_at"]})
    return updated


def main(argv=None) -> int:
    from settings import ConfigError, load_secrets
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(argv if argv is not None else sys.argv[1:])
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if len(argv) != 2 or argv[0] != "cancel":
        print("사용법: python actions.py cancel <post_id> [--dry-run]")
        return 2
    try:
        post = cancel_post(open_store(load_secrets()), argv[1], dry_run=dry_run)
    except (ActionError, ConfigError) as e:
        print(f"취소 실패: {e}")
        return 1
    print(f"{'취소 가능 (dry_run, 바꾸지 않음)' if dry_run else '취소됨'}: {post['id']} (예정 {post.get('scheduled_at')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
