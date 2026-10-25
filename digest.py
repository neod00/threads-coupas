"""
일일 요약 (PLAN v2 Step 5). 매일 21:00 KST 텔레그램으로 보낸다. 읽지 않아도 발행은 진행된다.

담는 것 (2026-10-25 축약: 개수만, 문제가 있을 때만 자세히)
  - 상태: 문제가 없으면 "상태: 정상" 한 줄. 자동 정지·시험지 불합격·토큰 경고가 있을 때만 자세히
  - 운영 알림 (상품 링크 보충·발행 수 늘리기, ops_advice)
  - 오늘 둘러볼 검색어
  - 내일 발행 예정·오늘 발행 개수, 최근 36시간 보류(held) 개수 (보류는 있을 때만)
  개별 글·사유·post_id·답해 줄 답글은 모니터링 화면(monitor_app.py)에서 본다

실행:
  python digest.py            # 텔레그램 발송
  python digest.py --dry-run  # 보내지 않고 로그에만 출력
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import autostop
import rules
from settings import KST, Config, token_status

log = logging.getLogger("threads_coupas")

HELD_WINDOW_HOURS = 36      # 발행 직전 검사에서 held 된 글은 전날 생성됐을 수 있어 하루보다 길게 본다 (자세한 건 모니터링 화면)
# 사람이 직접 둘러볼 검색어 (2026-10-16). 하루 3개씩 돌린다. 남의 글에 자동으로 답하지 않는다 (규칙 6)
BROWSE_KEYWORDS = ("자취 겨울템", "강아지 산책", "고양이 집사", "무드등", "겨울 이불", "전기장판", "붕어빵",
                   "빼빼로데이", "수능 선물", "크리스마스 선물", "집들이 선물", "반려동물 옷", "가습기", "자취 요리")


def _hook(post: dict) -> str:
    lines = (post.get("text") or "").split("\n")
    i = 1 if lines and lines[0].lstrip().startswith("[광고]") else 0     # 예전 형식만 첫 줄이 대가성 문구
    return lines[i] if len(lines) > i else ""


def review_summary(post: dict) -> str:
    r = post.get("review_result") or {}
    if not r:
        return "검수 기록 없음"
    if r.get("status") == "approved":
        return "검수 통과 (R1~R12 pass)"
    if r.get("status") == "format_error":
        return "검수 응답 형식 오류: " + "; ".join(r.get("errors") or [])
    bad = [f"{code} {item['verdict']}: {item['reason']}" for code, item in (r.get("items") or {}).items()
           if item["verdict"] != "pass"]
    return " / ".join(bad) or r.get("status", "")


def held_reason(post: dict) -> str:
    """보류 사유: 검수 판정이 있으면 그것을, 없으면 마지막 가드 사유를 보여 준다."""
    r = post.get("review_result") or {}
    if r and r.get("status") != "approved":
        return review_summary(post)
    attempts = (post.get("guard_log") or {}).get("attempts") or []
    problems = attempts[-1]["problems"] if attempts else []
    return " / ".join(problems[:3]) or "사유 기록 없음"


def build_digest(store, cfg: Config, now: datetime, *, dry_run: bool, token_issued_at=None,
                 exam: tuple[bool, str] | None = None, products=None, hot: list[dict] | None = None) -> str:
    today = rules.kst_date(now)
    tomorrow = today + timedelta(days=1)
    lines = [f"[일일 요약] {today:%Y-%m-%d} ({'연습 모드 DRY_RUN' if dry_run else '실제 발행'})"]

    # 상태: 문제가 있을 때만 자세히
    stop = autostop.status(store)
    problems = []
    if stop.get("active"):
        problems.append("자동 정지: 정지 중 — 발행 안 됨")
        problems += [f"  - {r['reason']}" for r in stop.get("reasons", [])]
    if exam is not None and not exam[0]:
        problems.append(f"검수자 시험지: 불합격 — 발행 불가 ({exam[1]})")
    tok = token_status(token_issued_at, today)
    if tok.level != "ok":
        problems.append(f"토큰 경고: {tok.message}")
    lines += problems or ["상태: 정상"]
    lines.append("")
    if cfg.ops_advice and products is not None:
        import ops_advice
        notes = ops_advice.advice(store, cfg, products, now, stopped=bool(stop.get("active")))
        if notes:
            lines += notes + [""]

    # 사람이 직접 둘러볼 검색어 (남의 글에 자동으로 답하지 않는다 — 규칙 6). 답해 줄 답글 목록은 텔레그램에 넣지 않는다
    if hot:
        import trends
        lines.append("오늘 둘러볼 검색어 (네이버 검색 상승순): " + trends.describe(hot)
                     + " — 남의 글 5개에 진심 답글 달기 (10분)")
    else:
        start = today.toordinal() * 3
        words = [BROWSE_KEYWORDS[(start + i) % len(BROWSE_KEYWORDS)] for i in range(3)]
        lines.append("오늘 둘러볼 검색어: " + ", ".join(words) + " — 남의 글 5개에 진심 답글 달기 (10분)")
    lines.append("")

    # 발행 현황은 개수만. 개별 글은 모니터링 화면에서 본다
    upcoming = [p for p in store.list_posts(["approved"])
                if rules.post_time(p) and rules.kst_date(rules.post_time(p)) == tomorrow]
    published = [p for p in store.list_posts(["published"])
                 if p.get("published_at") and rules.kst_date(rules.as_dt(p["published_at"])) == today]
    lines.append(f"내일({tomorrow:%m-%d}) 발행 예정 {len(upcoming)}개")
    lines.append(f"오늘 발행 {len(published)}개")

    # 보류는 있을 때만, 개수만
    since = (now - timedelta(hours=HELD_WINDOW_HOURS)).astimezone(timezone.utc).isoformat()
    held = store.list_posts(["held"], since=since)
    if held:
        lines.append(f"보류(held) {len(held)}개 — 발행 안 함 (사유는 모니터링 화면)")
    return "\n".join(lines).rstrip()


def send_digest(store, cfg: Config, notifier, now: datetime | None = None, **kwargs) -> str:
    text = build_digest(store, cfg, now or datetime.now(timezone.utc), **kwargs)
    notifier.send(text)
    store.log_event("digest_sent", {"chars": len(text)})
    return text


def main() -> int:
    import exam
    from notify import make_notifier
    from settings import ConfigError, dry_run, load_config, load_secrets
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="일일 요약 발송")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 로그에만 출력")
    args = ap.parse_args()
    try:
        cfg, secrets = load_config(), load_secrets()
        store = open_store(secrets)
    except ConfigError as e:
        print(f"오류: {e}")
        return 1
    notifier = make_notifier(secrets, dry_run=args.dry_run)
    try:
        from products import load_products
        products = load_products()
    except ConfigError as e:
        log.warning(f"상품 목록을 읽지 못해 운영 알림을 건너뜁니다: {e}")
        products = None
    hot = None
    if secrets.naver_client_id and secrets.naver_client_secret and cfg.talk_topics:
        import trends
        try:
            hot = trends.hot_keywords(cfg.talk_topics, trends.naver_fetcher(
                secrets.naver_client_id, secrets.naver_client_secret,
                legacy=os.environ.get("NAVER_TREND_LEGACY", "").lower() == "true"),
                                      datetime.now(KST).date())
        except Exception as e:     # 검색어는 덤이다. 실패해도 요약은 보낸다
            log.warning(f"네이버 검색 트렌드를 읽지 못해 기본 검색어를 씁니다: {e}")
    send_digest(store, cfg, notifier, dry_run=dry_run(), token_issued_at=secrets.threads_token_issued_at,
                exam=exam.exam_status(cfg), products=products, hot=hot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
