"""
일일 요약 (PLAN v2 Step 5). 매일 21:00 KST 텔레그램으로 보낸다. 읽지 않아도 발행은 진행된다.

담는 것
  - 내일 발행 예정 글 (post_id·시각·훅·검수 결과) + 취소 방법
  - 최근 7일 동안 처리되지 않은 held 글과 보류 사유 (uncertain 항목·검수 사유·가드·링크 확인 사유)
  - 오늘 발행 글과 조회수
  - 자동 정지 여부, 검수자 시험지 상태, DRY_RUN, 토큰 만료 경고

실행:
  python digest.py            # 텔레그램 발송
  python digest.py --dry-run  # 보내지 않고 로그에만 출력
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import autostop
import rules
from settings import KST, Config, token_status

log = logging.getLogger("threads_coupas")

CANCEL_HELP = ("취소 방법: GitHub 앱 → 저장소 → Actions → 'Cancel post' → Run workflow → post_id 입력 "
               "(또는 모니터링 화면의 취소 버튼)")
TYPE_LABEL = {"calc": "계산", "compare": "비교", "pick": "소개", "bundle": "모음", "talk": "참여", "tip": "팁"}
HELD_WINDOW_DAYS = 7
INBOX_HOURS = 36            # 이 시간 안에 달린 남의 답글을 '답해 줄 답글'로 보여 준다
# 사람이 직접 둘러볼 검색어 (D, 2026-10-16). 하루 3개씩 돌린다. 남의 글에 자동으로 답하지 않는다 (규칙 6)
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
                 exam: tuple[bool, str] | None = None, products=None) -> str:
    today = rules.kst_date(now)
    tomorrow = today + timedelta(days=1)
    lines = [f"[일일 요약] {today:%Y-%m-%d} ({'연습 모드 DRY_RUN' if dry_run else '실제 발행'})", ""]

    # 상태
    stop = autostop.status(store)
    if stop.get("active"):
        lines.append("자동 정지: 정지 중 — 발행 안 됨")
        lines += [f"  - {r['reason']}" for r in stop.get("reasons", [])]
    else:
        lines.append("자동 정지: 없음")
    if exam is not None:
        lines.append(f"검수자 시험지: {'합격' if exam[0] else '불합격 — 발행 불가'} ({exam[1]})")
    tok = token_status(token_issued_at, today)
    if tok.level != "ok":
        lines.append(f"토큰 경고: {tok.message}")
    lines.append("")
    if cfg.ops_advice and products is not None:
        import ops_advice
        notes = ops_advice.advice(store, cfg, products, now, stopped=bool(stop.get("active")))
        if notes:
            lines += notes + [""]

    # 내일 예정
    upcoming = sorted(
        (p for p in store.list_posts(["approved"]) if rules.post_time(p) and rules.kst_date(rules.post_time(p)) == tomorrow),
        key=rules.post_time,
    )
    lines.append(f"내일({tomorrow:%m-%d}) 발행 예정 {len(upcoming)}개")
    for p in upcoming:
        t = rules.post_time(p).astimezone(KST)
        link = "링크" if p.get("has_link") else "링크 없음"
        lines.append(f"- {t:%H:%M} {TYPE_LABEL.get(p['type'], p['type'])}·{p.get('hook_style')}·{link} | {_hook(p)}")
        lines.append(f"  {review_summary(p)}")
        lines.append(f"  post_id: {p['id']}")
    if upcoming:
        lines.append(CANCEL_HELP)
    lines.append("")

    # 보류: 처리(수동 승인·폐기)되지 않은 held 글. 발행 직전 검사에서 held 된 글은 전날 생성됐을 수 있어 7일을 본다
    since = (now - timedelta(days=HELD_WINDOW_DAYS)).astimezone(timezone.utc).isoformat()
    held = store.list_posts(["held"], since=since)
    lines.append(f"보류(held) {len(held)}개 — 발행 안 함")
    for p in held:
        lines.append(f"- {TYPE_LABEL.get(p['type'], p['type'])} | {_hook(p) or '(초안 없음)'}")
        lines.append(f"  사유: {held_reason(p)}")
        lines.append(f"  post_id: {p['id']}")
    lines.append("")

    # D. 사람이 하는 10분 소통 (자동화하지 않는다 — 규칙 6)
    inbox = [r for r in (store.get_state("reply_inbox", []) or [])
             if (t := rules.as_dt(r.get("at"))) is None or t >= now - timedelta(hours=INBOX_HOURS)]
    lines.append(f"답해 줄 답글 {len(inbox)}개 (스레드 앱에서 직접 답해 주세요)")
    for r in inbox[-10:]:
        lines.append(f"- @{r.get('username') or '?'}: {r.get('text') or ''}")
        if r.get("permalink"):
            lines.append(f"  {r['permalink']}")
    start = today.toordinal() * 3
    words = [BROWSE_KEYWORDS[(start + i) % len(BROWSE_KEYWORDS)] for i in range(3)]
    lines.append("오늘 둘러볼 검색어: " + ", ".join(words) + " — 남의 글 5개에 진심 답글 달기 (10분)")
    lines.append("")

    # 오늘 발행
    published = [p for p in store.list_posts(["published"])
                 if p.get("published_at") and rules.kst_date(rules.as_dt(p["published_at"])) == today]
    metrics = store.latest_metrics([p["threads_media_id"] for p in published if p.get("threads_media_id")])
    lines.append(f"오늘 발행 {len(published)}개")
    for p in published:
        m = metrics.get(p.get("threads_media_id"))
        views = f"조회 {m['views']}" if m and m.get("views") is not None else "조회수 수집 전"
        lines.append(f"- {rules.as_dt(p['published_at']).astimezone(KST):%H:%M} {_hook(p)} | {views}")
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
    send_digest(store, cfg, notifier, dry_run=dry_run(), token_issued_at=secrets.threads_token_issued_at,
                exam=exam.exam_status(cfg), products=products)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
