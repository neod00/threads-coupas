"""
발행기 (PLAN v2 Step 6). Supabase pg_cron 이 매시 12분·42분에 GitHub Actions 를 깨워 실행한다 (예비: GitHub 예약 4시간마다). 한 번에 글 1개만 발행한다.
파라미터는 THREADS_API_NOTES.md (2026-09-27 Meta 공식 문서 확인)를 따른다.

실행 순서 (run_once)
  1 킬 스위치 AUTO_PUBLISH_DISABLED → 아무것도 하지 않고 정상 종료 (규칙 8)
  2 자동 정지 상태 → 종료 (규칙 10)
  3 검수자 시험지 합격 확인 → 불합격이면 실제 발행 거부 (규칙 9). 케이스가 바뀐 경우면 자동 정지 (규칙 10-5)
  4 답글만 실패했던 글이 있으면 답글만 재시도
  5 approved 이고 scheduled_at <= now 인 글 중 가장 이른 1개
  6 발행 직전 검사 (publish_post)
      - 이미 threads_media_id 가 있으면 건너뜀 (멱등성)
      - status == approved 가 아니면 거부 (규칙 4)
      - 코드 가드 재실행 (규칙 3 대가성 문구 포함) → 실패하면 held
      - 하루 최대 개수·최소 간격 (실제 발행 기록 기준, 규칙 5) → 어기면 이번에는 발행하지 않고 다음 실행을 기다림
      - 링크 형식 확인 (https + 쿠팡 간편링크 도메인, HTTP 요청 없음) → 실패하면 held
  7 컨테이너 생성 → 상태 확인 → 발행 → threads_media_id 즉시 저장 → (link_in_reply) 답글
DRY_RUN=true 면 API 를 부르지 않고 보낼 요청 내용만 로그에 남기며 DB 도 바꾸지 않는다 (규칙 9).

오류: 네트워크 오류만 1회 재시도. 그 외 실패는 status=failed + 로그, 자동 재발행 안 함. 2회 연속 실패 → 자동 정지.

실행:
  python publisher.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import autostop
import guard
import rules
from settings import HARD_MAX_POSTS_PER_DAY, Config

log = logging.getLogger("threads_coupas")

API_BASE = "https://graph.threads.net/v1.0"
FIRST_WAIT_SECONDS = 30          # 문서 권장: 컨테이너 생성 후 평균 30초 기다렸다가 발행
POLL_WAIT_SECONDS = 10
MAX_POLLS = 6
# 규칙 2: 텍스트 + 링크 미리보기만. 이 키만 요청에 들어갈 수 있다.
ALLOWED_PARAMS = {"media_type", "text", "link_attachment", "topic_tag", "reply_to_id"}


class PublishRefused(Exception):
    """규칙 위반으로 발행하지 않는다 (상태는 바꾸지 않음)."""


class NetworkError(Exception):
    """연결·시간 초과. 1회만 재시도한다."""


class ThreadsApiError(Exception):
    pass


# ── 요청 만들기 (규칙 2·6) ────────────────────────────────────────────────

def container_params(post: dict, *, reply_to_id: str | None = None) -> dict:
    """본문(reply_to_id=None) 또는 링크 답글의 컨테이너 파라미터. media_type 은 TEXT 고정."""
    if reply_to_id is None:
        params = {"media_type": "TEXT", "text": post["text"]}
        if post.get("variant") == "link_in_body" and post.get("link_url"):
            params["link_attachment"] = post["link_url"]
        if post.get("topic_tag"):
            params["topic_tag"] = post["topic_tag"]
    else:
        # 규칙 6: 답글은 이 글의 내 본문에만 단다
        if reply_to_id != post.get("threads_media_id"):
            raise PublishRefused("규칙 6: 답글은 이 글의 본문 미디어 ID 에만 달 수 있습니다")
        params = {"media_type": "TEXT", "text": post["reply_text"], "reply_to_id": reply_to_id}
        if post.get("link_url"):
            params["link_attachment"] = post["link_url"]
    extra = set(params) - ALLOWED_PARAMS
    if extra or params["media_type"] != "TEXT":
        raise PublishRefused(f"규칙 2: 텍스트 외 요청은 만들지 않습니다: {sorted(extra)}")
    return params


# ── API ─────────────────────────────────────────────────────────────────

class ThreadsApi:
    """실제 Threads Graph API. 계정은 THREADS_USER_ID 하나뿐이다 (규칙 1)."""

    def __init__(self, user_id: str, access_token: str, session=None, timeout: float = 30):
        import requests
        self.user_id = user_id
        self.token = access_token
        self.http = session or requests.Session()
        self.timeout = timeout

    def _request(self, method: str, path: str, **params) -> dict:
        import requests
        params["access_token"] = self.token
        try:
            if method == "GET":
                resp = self.http.get(f"{API_BASE}/{path}", params=params, timeout=self.timeout)
            else:
                resp = self.http.post(f"{API_BASE}/{path}", data=params, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise NetworkError(str(e)) from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            raise ThreadsApiError(f"HTTP {resp.status_code} {err.get('message') or resp.text[:200]}")
        return data

    def create_container(self, params: dict) -> str:
        return self._request("POST", f"{self.user_id}/threads", **params)["id"]

    def container_status(self, container_id: str) -> tuple[str, str | None]:
        data = self._request("GET", container_id, fields="status,error_message")
        return data.get("status", ""), data.get("error_message")

    def publish(self, creation_id: str) -> str:
        return self._request("POST", f"{self.user_id}/threads_publish", creation_id=creation_id)["id"]

    # ── 읽기 전용 (Step 8) ──
    def insights(self, media_id: str) -> dict:
        """GET /{media-id}/insights → {metric: 값 또는 None}. views·shares 는 비어 올 수 있다 (THREADS_API_NOTES.md)."""
        data = self._request("GET", f"{media_id}/insights", metric=",".join(INSIGHT_METRICS))
        return parse_insights(data)

    def conversation(self, media_id: str, max_pages: int = 5) -> list[dict]:
        """GET /{media-id}/conversation: 깊이와 상관없이 모든 답글 (최신순)."""
        replies, after = [], None
        for _ in range(max_pages):
            params = {"fields": "id,text,username,timestamp,is_reply_owned_by_me"}
            if after:
                params["after"] = after
            data = self._request("GET", f"{media_id}/conversation", **params)
            replies += data.get("data") or []
            after = ((data.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not (data.get("paging") or {}).get("next"):
                break
        return replies


INSIGHT_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")


def parse_insights(data: dict) -> dict:
    """인사이트 응답의 metric 별 값. values[0].value 또는 total_value.value 형식을 모두 읽는다."""
    out = {m: None for m in INSIGHT_METRICS}
    for item in data.get("data") or []:
        name = item.get("name")
        if name not in out:
            continue
        values = item.get("values") or []
        value = values[0].get("value") if values else (item.get("total_value") or {}).get("value")
        out[name] = int(value) if isinstance(value, (int, float)) else None
    return out


class DryRunApi:
    """API 를 부르지 않고 보낼 요청을 로그에 남긴다. access_token 은 남기지 않는다."""

    def __init__(self, user_id: str | None):
        self.user_id = user_id or "<THREADS_USER_ID>"
        self.requests: list[dict] = []
        self._n = 0

    def _log(self, method: str, path: str, params: dict) -> None:
        self.requests.append({"method": method, "path": path, "params": dict(params)})
        log.info(f"[DRY_RUN] {method} {API_BASE}/{path} {json.dumps(params, ensure_ascii=False)}")

    def _placeholder(self, kind: str) -> str:
        self._n += 1
        return f"<{kind} {self._n}>"

    def create_container(self, params: dict) -> str:
        self._log("POST", f"{self.user_id}/threads", params)
        return self._placeholder("컨테이너 ID")

    def container_status(self, container_id: str) -> tuple[str, str | None]:
        self._log("GET", container_id, {"fields": "status,error_message"})
        return "FINISHED", None

    def publish(self, creation_id: str) -> str:
        self._log("POST", f"{self.user_id}/threads_publish", {"creation_id": creation_id})
        return self._placeholder("미디어 ID")


def _once_more_on_network_error(fn, *args):
    try:
        return fn(*args)
    except NetworkError as e:
        log.warning(f"[발행] 네트워크 오류, 1회 재시도: {e}")
        return fn(*args)


def publish_container(api, params: dict, sleep: Callable[[float], None], wait: bool = True) -> str:
    """컨테이너 생성 → 상태 FINISHED 확인 → 발행. 미디어 ID 를 돌려준다."""
    container = _once_more_on_network_error(api.create_container, params)
    if wait:
        sleep(FIRST_WAIT_SECONDS)
    for poll in range(MAX_POLLS):
        status, error = _once_more_on_network_error(api.container_status, container)
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise ThreadsApiError(f"컨테이너 {status}: {error or ''}")
        if status == "PUBLISHED":
            # 이미 발행된 컨테이너 — 다시 발행하면 중복이 된다. 미디어 ID 를 알 수 없으니 사람이 확인해야 한다.
            raise ThreadsApiError("컨테이너가 이미 PUBLISHED 입니다. 중복 발행을 막기 위해 멈춥니다 (스레드 앱에서 확인)")
        if wait:
            sleep(POLL_WAIT_SECONDS)
    else:
        raise ThreadsApiError(f"컨테이너가 {MAX_POLLS}번 확인하는 동안 FINISHED 가 되지 않았습니다")
    try:
        return api.publish(container)
    except NetworkError as e:
        # 요청은 서버에서 성공했을 수 있다. 컨테이너가 아직 FINISHED 일 때만 한 번 더 발행한다.
        log.warning(f"[발행] 발행 요청 네트워크 오류: {e}")
        status, _ = api.container_status(container)
        if status != "FINISHED":
            raise ThreadsApiError(f"발행 요청 오류 후 컨테이너 상태 {status} — 중복 방지를 위해 재시도하지 않습니다")
        return api.publish(container)


# ── 링크 확인 ─────────────────────────────────────────────────────────────

def check_link(url: str) -> str | None:
    """링크 형식만 확인한다: https + 쿠팡 파트너스 간편링크 도메인. 문제가 없으면 None.
    HTTP 요청은 보내지 않는다 — 간편링크에 자동으로 접속하면 파트너스 리포트에 클릭으로 잡혀
    판정 지표를 부풀리고 부정 클릭으로 보일 수 있다 (2026-09-27 결정, PLAN Step 6)."""
    from products import LINK_HOSTS

    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https":
        return f"링크가 https 가 아닙니다: {url}"
    if (parsed.hostname or "").lower() not in LINK_HOSTS:
        return f"링크가 쿠팡 파트너스 간편링크({', '.join(LINK_HOSTS)})가 아닙니다: {parsed.hostname}"
    if not parsed.path.strip("/"):
        return f"링크에 상품 경로가 없습니다: {url}"
    return None


# ── 발행 직전 검사 + 발행 ──────────────────────────────────────────────────

def publish_time_problems(post: dict, published: list[dict], cfg: Config, now: datetime) -> list[str]:
    """규칙 5 를 실제 발행 기록으로 다시 본다 (하루 최대 개수, 직전 발행과 최소 간격)."""
    problems = []
    today = rules.kst_date(now)
    times = [rules.as_dt(p["published_at"]) for p in published if p.get("published_at") and p["id"] != post["id"]]
    cap = min(cfg.max_posts_per_day, HARD_MAX_POSTS_PER_DAY)
    if sum(rules.kst_date(t) == today for t in times) >= cap:
        problems.append(f"규칙 5: 오늘 이미 {cap}개를 발행했습니다")
    recent = [t for t in times if abs(now - t).total_seconds() < cfg.min_gap_hours * 3600]
    if recent:
        problems.append(f"규칙 5: 직전 발행과 {cfg.min_gap_hours:g}시간이 지나지 않았습니다")
    return problems


def _hold(store, post: dict, stage: str, problems: list[str]) -> dict:
    attempts = [*((post.get("guard_log") or {}).get("attempts") or []),
                {"attempt": "publish", "stage": stage, "hook_style": post.get("hook_style"), "problems": problems}]
    return store.update_post(post["id"], {"status": "held", "guard_log": {**(post.get("guard_log") or {}), "attempts": attempts}})


def publish_post(post: dict, *, store, cfg: Config, api, link_checker, notifier, now: datetime,
                 dry_run: bool, sleep: Callable[[float], None] = time.sleep) -> dict:
    """글 1개를 검사하고 발행한다. 결과 dict: {"action": skipped|held|published|dry_run|failed, ...}.
    승인되지 않았거나 규칙 5 를 어기면 PublishRefused."""
    if post.get("threads_media_id"):
        return {"action": "skipped", "reason": "이미 발행됨 (threads_media_id 있음)", "post_id": post["id"]}
    if post.get("status") != "approved":
        raise PublishRefused(f"규칙 4: 승인(approved)되지 않은 글은 발행하지 않습니다 (현재 {post.get('status')})")

    problems = guard.check_post(post, cfg)
    if problems:
        log.warning(f"[발행] 가드 재검사 실패 → held: {problems}")
        if not dry_run:
            _hold(store, post, "guard", problems)
        return {"action": "held", "problems": problems, "post_id": post["id"]}

    timing = publish_time_problems(post, store.list_posts(["published"]), cfg, now)
    if timing:
        raise PublishRefused(" / ".join(timing))

    if post.get("has_link") and post.get("link_url"):
        link_problem = link_checker(post["link_url"])
        if link_problem:
            log.warning(f"[발행] {link_problem} → held")
            if not dry_run:
                _hold(store, post, "link", [link_problem])
            return {"action": "held", "problems": [link_problem], "post_id": post["id"]}

    try:
        media_id = publish_container(api, container_params(post), sleep, wait=not dry_run)
    except (ThreadsApiError, NetworkError) as e:
        log.error(f"[발행] 실패 → failed (자동 재발행 안 함): {e}")
        if not dry_run:
            store.update_post(post["id"], {"status": "failed"})
            store.log_event("publish_failed", {"post_id": post["id"], "error": str(e)})
            autostop.record_publish_result(store, notifier, False, {"post_id": post["id"], "error": str(e)})
        return {"action": "failed", "error": str(e), "post_id": post["id"]}

    if dry_run:
        post = dict(post, threads_media_id=media_id)
    else:
        # 멱등성 키를 가장 먼저 저장한다 (중간 상태를 두지 않는다 — 티스토리 앱 d089cf1 교훈)
        post = store.update_post(post["id"], {"threads_media_id": media_id, "status": "published",
                                              "published_at": now.astimezone(timezone.utc).isoformat()})
        store.log_event("published", {"post_id": post["id"], "threads_media_id": media_id})
        autostop.record_publish_result(store, notifier, True)

    reply = publish_reply(post, store=store, api=api, notifier=notifier, dry_run=dry_run, sleep=sleep)
    return {"action": "dry_run" if dry_run else "published", "post_id": post["id"],
            "threads_media_id": media_id, "reply": reply}


def publish_reply(post: dict, *, store, api, notifier, dry_run: bool, sleep=time.sleep) -> str | None:
    """link_in_reply 글의 링크 답글. 실패해도 본문은 published 로 두고 다음 실행에서 답글만 재시도한다."""
    if post.get("variant") != "link_in_reply" or post.get("threads_reply_id"):
        return None
    try:
        reply_id = publish_container(api, container_params(post, reply_to_id=post["threads_media_id"]), sleep,
                                     wait=not dry_run)
    except (ThreadsApiError, NetworkError) as e:
        log.error(f"[발행] 답글 실패 (다음 실행에서 답글만 재시도): {e}")
        if not dry_run:
            store.log_event("reply_failed", {"post_id": post["id"], "error": str(e)})
            autostop.record_publish_result(store, notifier, False, {"post_id": post["id"], "error": str(e), "reply": True})
        return None
    if not dry_run:
        store.update_post(post["id"], {"threads_reply_id": reply_id})
        store.log_event("reply_published", {"post_id": post["id"], "threads_reply_id": reply_id})
    return reply_id


def run_once(store, cfg: Config, *, env=None, api, link_checker, notifier, now: datetime | None = None,
             exam_status: tuple[bool, str], sleep: Callable[[float], None] = time.sleep,
             reply_reader=None) -> dict:
    """publish.yml 한 번 실행분. 킬 스위치는 main 에서도 먼저 확인하지만 여기서도 다시 본다.
    reply_reader: 답글을 읽을 API (conversation). 있으면 발행 전에 답글 감시를 먼저 돌린다 (규칙 10-1)."""
    from settings import auto_publish_disabled, dry_run as dry_run_flag

    if auto_publish_disabled(env):
        log.info("AUTO_PUBLISH_DISABLED 가 켜져 있어 발행 작업을 건너뜁니다.")
        return {"action": "kill_switch"}
    now = now or datetime.now(timezone.utc)
    dry_run = dry_run_flag(env)

    if reply_reader is not None:
        import watch_replies
        watch_replies.watch(store, reply_reader, notifier, now)

    if autostop.is_stopped(store):
        log.warning("자동 정지 상태라 발행하지 않습니다.")
        return {"action": "auto_stopped"}

    exam_ok, exam_reason = exam_status
    if not exam_ok:
        if "케이스" in exam_reason:     # 규칙 10-5: 놓친 글이 시험지에 추가됨 → 재시험 합격 전까지 정지
            autostop.trigger(store, notifier, f"검수자 시험지 재시험 필요 — {exam_reason}")
            return {"action": "auto_stopped"}
        if not dry_run:
            log.error(f"규칙 9: 검수자 시험지 불합격 상태라 발행을 거부합니다 — {exam_reason}")
            return {"action": "refused", "reason": exam_reason}
        log.warning(f"검수자 시험지 불합격 — 실제 발행이었다면 거부됩니다 ({exam_reason})")

    # 답글만 실패했던 글: 답글만 재시도
    for post in store.list_posts(["published"]):
        if post.get("variant") == "link_in_reply" and post.get("threads_media_id") and not post.get("threads_reply_id"):
            publish_reply(post, store=store, api=api, notifier=notifier, dry_run=dry_run, sleep=sleep)

    due = sorted((p for p in store.list_posts(["approved"])
                  if rules.post_time(p) and rules.post_time(p) <= now), key=rules.post_time)
    if not due:
        return {"action": "nothing_due"}
    try:
        return publish_post(due[0], store=store, cfg=cfg, api=api, link_checker=link_checker, notifier=notifier,
                            now=now, dry_run=dry_run, sleep=sleep)
    except PublishRefused as e:
        log.warning(f"[발행] 이번 실행에서는 발행하지 않습니다: {e}")
        return {"action": "refused", "reason": str(e), "post_id": due[0]["id"]}


def main() -> int:
    from settings import auto_publish_disabled

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # 규칙 8: 킬 스위치는 설정·DB 를 읽기 전에 확인하고 바로 정상 종료한다
    if auto_publish_disabled():
        log.info("AUTO_PUBLISH_DISABLED 가 켜져 있어 발행 작업을 건너뜁니다.")
        return 0

    import exam
    from notify import make_notifier
    from settings import ConfigError, dry_run, load_config, load_secrets, log_token_status
    from store import open_store

    try:
        cfg, secrets = load_config(), load_secrets()
        store = open_store(secrets)
        if not dry_run():
            secrets.require("threads_user_id", "threads_access_token")
    except ConfigError as e:
        log.error(f"설정 오류: {e}")
        return 1
    token = log_token_status(secrets.threads_token_issued_at)
    if token.level == "expired" and not dry_run():
        log.error("토큰이 만료돼 발행하지 않습니다 (글 상태는 그대로 둡니다).")
        return 1
    api = DryRunApi(secrets.threads_user_id) if dry_run() else ThreadsApi(secrets.threads_user_id, secrets.threads_access_token)
    # 답글 감시는 읽기만 하므로 연습 모드에서도 토큰이 있으면 실제 API 로 돈다
    reader = (ThreadsApi(secrets.threads_user_id, secrets.threads_access_token)
              if secrets.threads_user_id and secrets.threads_access_token else None)
    result = run_once(store, cfg, api=api, link_checker=check_link, notifier=make_notifier(secrets),
                      exam_status=exam.exam_status(cfg), reply_reader=reader)
    log.info(f"결과: {json.dumps(result, ensure_ascii=False)}")
    return 0 if result["action"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
