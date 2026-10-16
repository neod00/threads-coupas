"""
주간 리포트 (PLAN v2 Step 9, weekly.yml 매주 일요일 21:00 KST).

리포트 항목
  - 유형별(calc/compare/tip)·훅 유형별(question/result/compare)·variant 별 조회수 중앙값, 좋아요·답글 비율
  - 일별 발행 수 대비 쿠팡 클릭·주문
  - 검수 통계: 생성·승인·held 수, 주요 보류 사유, 가드(G1~G8)·검수(R1~R12) 항목별 실패 빈도
  - 0장 판정 기준 대비 한 줄 판정: 계속 / 조정 / 종료
AI 리뷰 "다음 주 바꿀 3가지"
  - 이번 주 발행이 10개 미만이면 LLM 을 부르지 않고 "데이터 부족 — 이번 주 설정 유지"만 쓴다
  - 규칙 위반 제안(댓글로 옮기기, 문구 삭제, 후기, 최저가, 검수 생략, 하루 6개 이상 등)은 코드가 버리고 로그에 남긴다
  - 근거(evidence)에는 리포트에 있는 숫자만 쓸 수 있다
  - 제안은 자동 적용하지 않는다. config 변경은 사람이 한다.

실행:
  python weekly_report.py            # reports/weekly_YYYY-MM-DD.md 저장 + 텔레그램 발송
  python weekly_report.py --dry-run  # 저장만 하고 보내지 않음
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import guard
import rules
from settings import KST, PROJECT_ROOT, Config

log = logging.getLogger("threads_coupas")

REPORTS_DIR = PROJECT_ROOT / "reports"
MIN_POSTS_FOR_REVIEW = 10
DATA_SHORTAGE = "데이터 부족 — 이번 주 설정 유지"
REVIEW_HEADER = "## AI 리뷰: 다음 주 바꿀 3가지"
# 1장 절대 규칙을 어기는 제안 (PLAN Step 9 코드 필터)
FORBIDDEN_SUGGESTION_PATTERNS = [
    (r"댓글로|답글로\s*옮", "링크·문구를 댓글로 옮기기"),
    (r"(대가성\s*)?문구\s*(를\s*)?(삭제|빼|없애|제거|생략|옮기)", "대가성 문구 삭제·이동"),
    (r"후기|리뷰\s*형식|사용\s*경험|써\s*본", "후기·경험 형식"),
    (r"최저가|할인|특가|가격|쿠폰|가성비", "가격·할인 표현"),
    (r"검수\s*(를\s*)?(생략|완화|끄|빼|줄이)|가드\s*(를\s*)?(끄|완화|빼)", "검수·가드 완화"),
    (r"하루\s*(최대\s*)?([6-9]|\d{2,})\s*개", "게시량 상한 초과"),
    (r"다계정|부계정|계정\s*(을\s*)?(늘|추가)", "다계정"),
    (r"자동\s*(댓글|멘션|팔로우)|다른\s*계정.*(댓글|멘션|팔로우)", "타 계정 자동 상호작용"),
    (r"이미지|영상|사진", "남의 이미지·영상 사용"),
]


# ── 집계 ─────────────────────────────────────────────────────────────

def _views_by_media(metric_rows) -> dict[str, dict]:
    """글마다 가장 늦은 체크포인트(72시간 > 24시간)의 지표."""
    best: dict[str, dict] = {}
    for m in metric_rows:
        cur = best.get(m["threads_media_id"])
        if cur is None or m["hours_since_publish"] > cur["hours_since_publish"]:
            best[m["threads_media_id"]] = m
    return best


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values)) if values else None


def _rate(numer, denom):
    return f"{numer / denom:.1%}" if denom else "-"


def _group_table(posts, metrics, key, labels) -> list[str]:
    lines = ["| 구분 | 글 수 | 조회수 중앙값 | 좋아요 비율 | 답글 비율 |", "|---|---|---|---|---|"]
    for label in labels:
        group = [p for p in posts if (p.get(key) or "-") == label]
        rows = [metrics[p["threads_media_id"]] for p in group if p.get("threads_media_id") in metrics]
        views = sum(r.get("views") or 0 for r in rows)
        lines.append(f"| {label} | {len(group)} | {_median([r.get('views') for r in rows]) if rows else '-'} | "
                     f"{_rate(sum(r.get('likes') or 0 for r in rows), views)} | "
                     f"{_rate(sum(r.get('replies') or 0 for r in rows), views)} |")
    return lines


def failure_counts(posts) -> tuple[Counter, Counter, Counter]:
    """(가드 항목별, 검수 항목별, 보류 사유 첫 줄) 실패 빈도."""
    g, r, reasons = Counter(), Counter(), Counter()
    for p in posts:
        for attempt in (p.get("guard_log") or {}).get("attempts") or []:
            for problem in attempt.get("problems") or []:
                m = re.match(r"\[(G\d)\]", problem)
                if m:
                    g[m.group(1)] += 1
        for code, item in ((p.get("review_result") or {}).get("items") or {}).items():
            if item.get("verdict") != "pass":
                r[code] += 1
        if p["status"] == "held":
            attempts = (p.get("guard_log") or {}).get("attempts") or []
            last = attempts[-1]["problems"][0] if attempts and attempts[-1]["problems"] else "사유 기록 없음"
            reasons[last[:60]] += 1
    return g, r, reasons


def verdict(cfg: Config, start: date | None, today: date, all_published, all_metrics, coupang_rows) -> str:
    """0장 판정 기준 대비 한 줄 판정."""
    if start is None:
        return "판정 보류 — config.yaml 의 test_start_date(첫 실제 발행일)가 비어 있습니다"
    week = (today - start).days // 7 + 1
    c = cfg.criteria
    median_views = _median([m.get("views") for m in all_metrics.values()])
    clicks = sum(r["clicks"] for r in coupang_rows)
    orders = sum(r["orders"] for r in coupang_rows)
    commission = sum(r["commission"] for r in coupang_rows)
    status = (f"{week}주차 · 누적 발행 {len(all_published)}개 · 조회수 중앙값 {median_views if median_views is not None else '-'} · "
              f"쿠팡 클릭 {clicks} · 주문 {orders} · 수수료 {commission:,}원")
    if week < 2:
        return f"판정 전 ({status})"
    if week < 4:
        ok = (median_views or 0) >= c.week2_min_median_views and clicks >= c.week2_min_coupang_clicks
        return (f"계속 — 2주차 기준 충족 ({status})" if ok else
                f"조정 — 2주차 기준 미달 (조회수 중앙값 {c.week2_min_median_views}·클릭 {c.week2_min_coupang_clicks} 기준), "
                f"콘텐츠 유형 비율을 바꿔 1주 연장 ({status})")
    ok = commission >= c.week4_min_commission_won or orders >= c.week4_min_orders
    return (f"계속 — 4주차 기준 충족, 확장 검토 ({status})" if ok else
            f"종료 — 4주차 기준 미달 (수수료 {c.week4_min_commission_won:,}원 또는 주문 {c.week4_min_orders}건) ({status})")


def build_report(store, cfg: Config, now: datetime) -> tuple[str, dict]:
    today = rules.kst_date(now)
    week_start = today - timedelta(days=6)
    since = datetime.combine(week_start, datetime.min.time(), tzinfo=KST)

    published_all = [p for p in store.list_posts(["published"]) if p.get("published_at")]
    week_published = [p for p in published_all if rules.as_dt(p["published_at"]) >= since]
    metrics_all = _views_by_media(store.all_metrics([p["threads_media_id"] for p in published_all if p.get("threads_media_id")]))
    week_metrics = {p["threads_media_id"]: metrics_all[p["threads_media_id"]]
                    for p in week_published if p.get("threads_media_id") in metrics_all}
    created = store.list_posts(since=since.astimezone(timezone.utc).isoformat())
    coupang_week = store.list_coupang(week_start.isoformat(), today.isoformat())
    start = cfg.test_start_date
    coupang_all = store.list_coupang(start.isoformat(), today.isoformat()) if start else coupang_week

    lines = [f"# 주간 리포트 {week_start} ~ {today}", "",
             f"**판정:** {verdict(cfg, start, today, published_all, metrics_all, coupang_all)}", ""]

    lines += ["## 유형별", *_group_table(week_published, week_metrics, "type", ["calc", "compare", "pick", "bundle", "talk", "tip"]), ""]
    lines += ["## 훅 유형별", *_group_table(week_published, week_metrics, "hook_style", ["question", "result", "compare"]), ""]
    lines += ["## 링크 위치(variant)별",
              *_group_table([p for p in week_published if p.get("variant")], week_metrics, "variant",
                            ["link_in_body", "link_in_reply"]), ""]

    lines += ["## 일별 발행 수 대비 쿠팡 클릭·주문", "| 날짜 | 발행 | 클릭 | 주문 | 수수료 |", "|---|---|---|---|---|"]
    by_day = Counter(rules.kst_date(rules.as_dt(p["published_at"])).isoformat() for p in week_published)
    coupang_by_day = {r["date"]: r for r in coupang_week}
    for i in range(7):
        d = (week_start + timedelta(days=i)).isoformat()
        c = coupang_by_day.get(d)
        clicks, orders = (c["clicks"], c["orders"]) if c else ("-", "-")
        commission = f"{c['commission']:,}원" if c else "-"
        lines.append(f"| {d} | {by_day.get(d, 0)} | {clicks} | {orders} | {commission} |")
    lines.append("")

    g, r, reasons = failure_counts(created)
    statuses = Counter(p["status"] for p in created)
    lines += ["## 검수 통계",
              f"- 생성 {len(created)}개 · 승인 {statuses['approved'] + statuses['published']}개 · "
              f"held {statuses['held']}개 · 취소 {statuses['cancelled']}개 · 실패 {statuses['failed']}개",
              "- 가드 항목별 실패: " + (", ".join(f"{k} {v}회" for k, v in sorted(g.items())) or "없음"),
              "- 검수 항목별 fail·uncertain: " + (", ".join(f"{k} {v}회" for k, v in sorted(r.items(), key=lambda x: int(x[0][1:]))) or "없음"),
              "- 주요 보류 사유: " + ("; ".join(f"{k} ({v})" for k, v in reasons.most_common(3)) or "없음"), ""]

    ranked = sorted((p for p in week_published if p.get("threads_media_id") in week_metrics),
                    key=lambda p: week_metrics[p["threads_media_id"]].get("views") or 0, reverse=True)
    context = {"published_count": len(week_published), "top": ranked[:3], "bottom": ranked[-3:][::-1] if len(ranked) > 3 else [],
               "metrics": week_metrics}
    return "\n".join(lines).rstrip(), context


# ── AI 리뷰 ──────────────────────────────────────────────────────────

REVIEW_PROMPT = """당신은 스레드 계정 운영 데이터를 읽고 다음 주에 바꿀 점을 제안하는 분석가입니다.
아래 절대 규칙을 어기는 제안은 하지 않습니다 (대가성 문구 이동·삭제, 후기 형식, 가격·최저가 표현, 게시량 상한 초과, 다계정, 타 계정 자동 댓글, 검수 완화 등).

{rules}

제약
- 근거에는 리포트에 있는 숫자만 씁니다. 새 숫자를 만들지 않습니다.
- 제안은 정확히 3개. 각 제안은 무엇을 바꿀지(what) / 근거가 된 리포트 숫자(evidence) / 다음 주에 어떻게 테스트할지(test) 세 줄입니다.
- 반드시 JSON 으로만 답합니다: {{"suggestions": [{{"what": "...", "evidence": "...", "test": "..."}}, ...]}}"""


def violation(suggestion: dict) -> str | None:
    text = " ".join(str(suggestion.get(k) or "") for k in ("what", "evidence", "test"))
    for pattern, label in FORBIDDEN_SUGGESTION_PATTERNS:
        if re.search(pattern, text):
            return label
    return None


def filter_suggestions(suggestions: list, report_md: str) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(남은 제안, (버린 제안, 사유) 목록). 규칙 위반 표현이나 리포트에 없는 근거 숫자가 있으면 버린다."""
    allowed = guard.allowed_numbers(report_md)
    kept, dropped = [], []
    for s in suggestions:
        if not isinstance(s, dict) or not all(str(s.get(k) or "").strip() for k in ("what", "evidence", "test")):
            dropped.append((s, "형식 오류"))
            continue
        bad = violation(s)
        if bad:
            dropped.append((s, f"규칙 위반: {bad}"))
            continue
        extra = guard.extract_numbers(s["evidence"]) - allowed
        if extra:
            dropped.append((s, f"리포트에 없는 숫자: {', '.join(sorted(extra))}"))
            continue
        kept.append(s)
    return kept, dropped


def ai_review(report_md: str, context: dict, complete, absolute_rules: str) -> str:
    if context["published_count"] < MIN_POSTS_FOR_REVIEW:
        return f"{REVIEW_HEADER}\n\n{DATA_SHORTAGE}"

    def brief(p):
        m = context["metrics"].get(p.get("threads_media_id"), {})
        return {"type": p["type"], "hook_style": p.get("hook_style"), "text": p["text"],
                "views": m.get("views"), "likes": m.get("likes"), "replies": m.get("replies")}

    user = json.dumps({"report": report_md, "top3": [brief(p) for p in context["top"]],
                       "bottom3": [brief(p) for p in context["bottom"]]}, ensure_ascii=False, indent=1)
    try:
        data = json.loads(complete(REVIEW_PROMPT.format(rules=absolute_rules), user, 0.3))
        suggestions = data.get("suggestions") if isinstance(data, dict) else None
    except Exception as e:
        log.error(f"[주간 AI 리뷰] 실패: {e}")
        return f"{REVIEW_HEADER}\n\nAI 리뷰 생성 실패 — 이번 주 설정 유지 ({e})"
    kept, dropped = filter_suggestions(suggestions if isinstance(suggestions, list) else [], report_md)
    for s, reason in dropped:
        log.warning(f"[주간 AI 리뷰] 제안을 버림 ({reason}): {json.dumps(s, ensure_ascii=False)[:200]}")
    lines = [REVIEW_HEADER, "", "_제안은 자동 적용하지 않습니다. config 변경은 사람이 직접 합니다._", ""]
    for i, s in enumerate(kept[:3], start=1):
        lines += [f"{i}. **무엇을:** {s['what']}", f"   - **근거:** {s['evidence']}", f"   - **테스트:** {s['test']}"]
    if not kept:
        lines.append("남은 제안이 없습니다 — 이번 주 설정 유지")
    if dropped:
        lines += ["", f"_규칙 위반 등으로 버린 제안 {len(dropped)}개 (로그에 기록)_"]
    return "\n".join(lines)


def main() -> int:
    from llm_client import LLMError, make_complete
    from notify import make_notifier
    from reviewer import absolute_rules
    from settings import ConfigError, load_config, load_secrets, provider_of
    from store import open_store

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="주간 리포트 + AI 리뷰")
    ap.add_argument("--dry-run", action="store_true", help="파일만 저장하고 텔레그램으로 보내지 않음")
    args = ap.parse_args()
    try:
        cfg, secrets = load_config(), load_secrets()
        store = open_store(secrets)
        fb = cfg.generator_fallback_model
        complete = make_complete(cfg.generator_model, secrets.api_key_for(provider_of(cfg.generator_model)),
                                 fb, secrets.api_key_for(provider_of(fb)) if fb else None)
    except (ConfigError, LLMError) as e:
        log.error(f"설정 오류: {e}")
        return 1
    now = datetime.now(timezone.utc)
    report, context = build_report(store, cfg, now)
    text = f"{report}\n\n{ai_review(report, context, complete, absolute_rules())}\n"
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"weekly_{rules.kst_date(now)}.md"
    path.write_text(text, encoding="utf-8")
    log.info(f"저장: {path}")
    make_notifier(secrets, dry_run=args.dry_run).send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
