"""
저장소. 운영은 Supabase(db/schema.sql), 테스트는 네트워크 없는 MemoryStore.
두 구현은 같은 메서드를 가진다. 게시물은 dict(posts 행)로 주고받는다. 시각은 ISO 문자열(UTC)로 저장한다.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    def __init__(self):
        self.posts: dict[str, dict] = {}
        self.metrics: list[dict] = []
        self.state: dict[str, object] = {}
        self.events: list[dict] = []

    # posts
    def insert_post(self, post: dict) -> dict:
        if post["id"] in self.posts:
            raise ValueError(f"이미 있는 post_id: {post['id']}")
        self.posts[post["id"]] = copy.deepcopy(post)
        return copy.deepcopy(post)

    def update_post(self, post_id: str, fields: dict) -> dict:
        if post_id not in self.posts:
            raise KeyError(post_id)
        self.posts[post_id].update(copy.deepcopy(fields))
        return copy.deepcopy(self.posts[post_id])

    def get_post(self, post_id: str) -> dict | None:
        post = self.posts.get(post_id)
        return copy.deepcopy(post) if post else None

    def list_posts(self, statuses=None, since: str | None = None) -> list[dict]:
        """statuses 에 해당하고 created_at 이 since 이후인 글 (created_at 순)."""
        rows = [p for p in self.posts.values()
                if (not statuses or p["status"] in statuses) and (not since or p["created_at"] >= since)]
        return copy.deepcopy(sorted(rows, key=lambda p: p["created_at"]))

    # metrics
    def latest_metrics(self, media_ids) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for m in self.metrics:
            if m["threads_media_id"] in media_ids:
                cur = latest.get(m["threads_media_id"])
                if cur is None or m["collected_at"] > cur["collected_at"]:
                    latest[m["threads_media_id"]] = m
        return copy.deepcopy(latest)

    def metric_checkpoints(self, media_ids) -> set[tuple[str, int]]:
        return {(m["threads_media_id"], m["hours_since_publish"]) for m in self.metrics if m["threads_media_id"] in media_ids}

    def insert_metric(self, row: dict) -> None:
        key = (row["threads_media_id"], row["hours_since_publish"])
        self.metrics = [m for m in self.metrics if (m["threads_media_id"], m["hours_since_publish"]) != key]
        self.metrics.append(copy.deepcopy(row))

    def upsert_coupang(self, rows: list[dict]) -> None:
        self.coupang = {**getattr(self, "coupang", {}), **{r["date"]: copy.deepcopy(r) for r in rows}}

    def list_coupang(self, start: str, end: str) -> list[dict]:
        """start <= date <= end (YYYY-MM-DD) 인 쿠팡 일별 행."""
        rows = [r for d, r in getattr(self, "coupang", {}).items() if start <= d <= end]
        return copy.deepcopy(sorted(rows, key=lambda r: r["date"]))

    def all_metrics(self, media_ids) -> list[dict]:
        return copy.deepcopy([m for m in self.metrics if m["threads_media_id"] in media_ids])

    # system_state / events
    def get_state(self, key: str, default=None):
        return copy.deepcopy(self.state.get(key, default))

    def set_state(self, key: str, value) -> None:
        self.state[key] = copy.deepcopy(value)

    def log_event(self, kind: str, detail: dict) -> None:
        self.events.append({"kind": kind, "detail": copy.deepcopy(detail), "created_at": now_iso()})


class SupabaseStore:
    """supabase-py 로 같은 동작을 한다. 서버 쪽(GitHub Actions)에서 service role key 로만 쓴다."""

    def __init__(self, url: str, service_role_key: str):
        from supabase import create_client
        self.db = create_client(url, service_role_key)

    def insert_post(self, post: dict) -> dict:
        return self.db.table("posts").insert(post).execute().data[0]

    def update_post(self, post_id: str, fields: dict) -> dict:
        data = self.db.table("posts").update(fields).eq("id", post_id).execute().data
        if not data:
            raise KeyError(post_id)
        return data[0]

    def get_post(self, post_id: str) -> dict | None:
        data = self.db.table("posts").select("*").eq("id", post_id).limit(1).execute().data
        return data[0] if data else None

    def list_posts(self, statuses=None, since: str | None = None) -> list[dict]:
        q = self.db.table("posts").select("*")
        if statuses:
            q = q.in_("status", list(statuses))
        if since:
            q = q.gte("created_at", since)
        return q.order("created_at").execute().data

    def latest_metrics(self, media_ids) -> dict[str, dict]:
        ids = list(media_ids)
        if not ids:
            return {}
        rows = (self.db.table("metrics_post").select("*").in_("threads_media_id", ids)
                .order("collected_at", desc=True).execute().data)
        latest: dict[str, dict] = {}
        for r in rows:
            latest.setdefault(r["threads_media_id"], r)
        return latest

    def metric_checkpoints(self, media_ids) -> set[tuple[str, int]]:
        ids = list(media_ids)
        if not ids:
            return set()
        rows = (self.db.table("metrics_post").select("threads_media_id,hours_since_publish")
                .in_("threads_media_id", ids).execute().data)
        return {(r["threads_media_id"], r["hours_since_publish"]) for r in rows}

    def insert_metric(self, row: dict) -> None:
        self.db.table("metrics_post").upsert(row).execute()

    def upsert_coupang(self, rows: list[dict]) -> None:
        if rows:
            self.db.table("metrics_coupang_daily").upsert(rows).execute()

    def list_coupang(self, start: str, end: str) -> list[dict]:
        return (self.db.table("metrics_coupang_daily").select("*").gte("date", start).lte("date", end)
                .order("date").execute().data)

    def all_metrics(self, media_ids) -> list[dict]:
        ids = list(media_ids)
        if not ids:
            return []
        return self.db.table("metrics_post").select("*").in_("threads_media_id", ids).execute().data

    def get_state(self, key: str, default=None):
        data = self.db.table("system_state").select("value").eq("key", key).limit(1).execute().data
        return data[0]["value"] if data else default

    def set_state(self, key: str, value) -> None:
        self.db.table("system_state").upsert({"key": key, "value": value, "updated_at": now_iso()}).execute()

    def log_event(self, kind: str, detail: dict) -> None:
        self.db.table("events").insert({"kind": kind, "detail": detail}).execute()


def open_store(secrets):
    secrets.require("supabase_url", "supabase_service_role_key")
    return SupabaseStore(secrets.supabase_url, secrets.supabase_service_role_key)
