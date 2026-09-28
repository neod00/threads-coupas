"""
Step 9 주간 리포트 테스트.

8장 체크리스트 (주간 리포트)
  - 훅 유형별 조회수 중앙값과 검수 통계가 표시됨
  - AI 리뷰: 규칙 위반 제안(예: "링크와 문구를 댓글로 옮기세요")을 주입하면 필터됨
  - AI 리뷰: 발행 게시물 10개 미만이면 "데이터 부족" 문구만 출력
"""
import dataclasses
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

import weekly_report
from settings import KST
from store import MemoryStore

NOW = datetime(2026, 11, 15, 21, 0, tzinfo=KST)          # 일요일 21:00


def seed(make_post, n_published=12):
    """이번 주 발행 글 n 개 (유형·훅 섞음) + 지표, 보류 글, 쿠팡 데이터."""
    store = MemoryStore()
    types = ["calc", "compare", "tip"]
    hooks = ["question", "result", "compare"]
    for i in range(n_published):
        t = types[i % 3]
        media = f"m{i}"
        published = (NOW - timedelta(days=i % 6, hours=5)).astimezone(timezone.utc).isoformat()
        store.insert_post(make_post(
            type=t, hook_style=hooks[i % 3], status="published", threads_media_id=media, published_at=published,
            variant=None if t == "tip" else "link_in_body", has_link=t != "tip",
            created_at=(NOW - timedelta(days=i % 6, hours=20)).astimezone(timezone.utc).isoformat(),
        ))
        # question 훅: 100·400·700·1000 → 중앙값 550 / result: 200·500·800·1100 → 650 / compare: 300·600·900·1200 → 750
        store.insert_metric({"threads_media_id": media, "hours_since_publish": 24, "collected_at": "x",
                             "views": 1, "likes": 0, "replies": 0})
        store.insert_metric({"threads_media_id": media, "hours_since_publish": 72, "collected_at": "y",
                             "views": 100 * (i + 1), "likes": 5, "replies": 1})
    store.insert_post(make_post(
        status="held", created_at=(NOW - timedelta(days=1)).astimezone(timezone.utc).isoformat(),
        guard_log={"attempts": [{"attempt": 1, "problems": ["[G3] 본문에 입력·계산 데이터에 없는 숫자: 7"]},
                                {"attempt": 2, "problems": ["[G7] 훅이 깁니다: 34자"]}]},
        review_result={"status": "uncertain", "items": {"R3": {"verdict": "uncertain", "reason": "애매", "hook_only": False}}},
    ))
    store.upsert_coupang([{"date": (NOW.date() - timedelta(days=d)).isoformat(), "clicks": 4, "orders": 0, "commission": 0}
                          for d in range(7)])
    return store


# ── 8장: 훅 유형별 중앙값 + 검수 통계 ─────────────────────────────────────────

def test_report_shows_hook_medians_and_review_stats(cfg, make_post):
    report, context = weekly_report.build_report(seed(make_post), cfg, NOW)
    hook_section = report.split("## 훅 유형별")[1].split("##")[0]
    assert "| question | 4 | 550 |" in hook_section            # 72시간 값 기준 (24시간 값 1 은 쓰지 않음)
    assert "| result | 4 | 650 |" in hook_section
    assert "| compare | 4 | 750 |" in hook_section
    stats = report.split("## 검수 통계")[1]
    assert "생성 13개" in stats and "승인 12개" in stats and "held 1개" in stats
    assert "G3 1회" in stats and "G7 1회" in stats
    assert "R3 1회" in stats
    assert "[G7] 훅이 깁니다" in stats                           # 주요 보류 사유
    assert "## 유형별" in report and "## 링크 위치(variant)별" in report
    assert "| 2026-11-15 |" in report and "| 4 | 0 | 0원 |" in report     # 일별 발행 대비 쿠팡
    assert context["published_count"] == 12


# ── 8장: AI 리뷰 — 규칙 위반 제안 필터 ─────────────────────────────────────────

GOOD = {"what": "훅을 결과형 위주로 바꾸기", "evidence": "훅 유형별 조회수 중앙값 result 650, question 550",
        "test": "다음 주 result 훅 비중을 늘려 중앙값 비교"}
BAD = {"what": "링크와 문구를 댓글로 옮기세요", "evidence": "링크 글 조회수 중앙값 550", "test": "답글에만 링크"}


def fake_llm(suggestions):
    calls = []

    def complete(system, user, temperature):
        calls.append({"system": system, "user": user})
        return json.dumps({"suggestions": suggestions}, ensure_ascii=False)

    complete.calls = calls
    return complete


def test_rule_breaking_suggestion_filtered(cfg, make_post, caplog):
    report, context = weekly_report.build_report(seed(make_post), cfg, NOW)
    llm = fake_llm([BAD, GOOD])
    with caplog.at_level(logging.WARNING, logger="threads_coupas"):
        section = weekly_report.ai_review(report, context, llm, "절대 규칙 전문")
    assert "댓글로 옮기세요" not in section
    assert "훅을 결과형 위주로 바꾸기" in section
    assert "버린 제안 1개" in section
    assert "링크·문구를 댓글로 옮기기" in caplog.text               # 로그에 남긴다
    assert "절대 규칙 전문" in llm.calls[0]["system"]
    assert "자동 적용하지 않습니다" in section


@pytest.mark.parametrize("what, label", [
    ("대가성 문구를 삭제해 첫 줄을 훅으로", "대가성 문구"),
    ("후기 형식으로 바꾸기", "후기"),
    ("최저가 비교를 넣기", "가격"),
    ("검수를 생략해 발행량 늘리기", "검수"),
    ("하루 8개로 늘리기", "게시량"),
    ("부계정을 하나 더 운영", "다계정"),
    ("다른 계정 글에 자동 댓글", "자동"),
])
def test_each_forbidden_suggestion_kind(what, label):
    kept, dropped = weekly_report.filter_suggestions([{**GOOD, "what": what}], "리포트 550 650")
    assert kept == [] and label in dropped[0][1]


def test_evidence_numbers_must_come_from_report():
    kept, dropped = weekly_report.filter_suggestions(
        [{**GOOD, "evidence": "result 훅 조회수 중앙값 9999"}], "훅 유형별 result 650")
    assert kept == [] and "리포트에 없는 숫자: 9999" in dropped[0][1]


def test_allowed_daily_count_suggestion_kept():
    kept, _ = weekly_report.filter_suggestions([{**GOOD, "what": "하루 3개로 줄여 품질 비교"}], "550 650")
    assert kept


# ── 8장: 10개 미만이면 데이터 부족만 ──────────────────────────────────────────────

def test_under_10_posts_prints_only_data_shortage(cfg, make_post):
    report, context = weekly_report.build_report(seed(make_post, n_published=9), cfg, NOW)
    llm = fake_llm([GOOD])
    section = weekly_report.ai_review(report, context, llm, "규칙")
    assert section == f"{weekly_report.REVIEW_HEADER}\n\n데이터 부족 — 이번 주 설정 유지"
    assert llm.calls == []                                    # LLM 을 부르지 않는다


def test_llm_failure_keeps_settings(cfg, make_post):
    report, context = weekly_report.build_report(seed(make_post), cfg, NOW)

    def broken(*a):
        raise RuntimeError("503")

    assert "이번 주 설정 유지" in weekly_report.ai_review(report, context, broken, "규칙")


# ── 0장 판정 ────────────────────────────────────────────────────────────

def _verdict(cfg, start, views, clicks=0, orders=0, commission=0):
    metrics = {f"m{i}": {"views": v} for i, v in enumerate(views)}
    coupang = [{"clicks": clicks, "orders": orders, "commission": commission}]
    cfg = dataclasses.replace(cfg, test_start_date=start)
    return weekly_report.verdict(cfg, start, date(2026, 11, 15), list(metrics), metrics, coupang)


@pytest.mark.parametrize("start, views, clicks, orders, commission, expected", [
    (None, [], 0, 0, 0, "판정 보류"),
    (date(2026, 11, 12), [500], 50, 0, 0, "판정 전"),                  # 1주차
    (date(2026, 11, 5), [300, 400], 30, 0, 0, "계속 — 2주차"),        # 2주차, 기준 충족
    (date(2026, 11, 5), [100, 200], 30, 0, 0, "조정"),                 # 조회수 미달
    (date(2026, 10, 20), [100], 10, 5, 0, "계속 — 4주차"),             # 주문 5건
    (date(2026, 10, 20), [100], 10, 0, 30000, "계속 — 4주차"),         # 수수료 3만 원
    (date(2026, 10, 20), [100], 10, 4, 29000, "종료"),
])
def test_verdict(cfg, start, views, clicks, orders, commission, expected):
    assert _verdict(cfg, start, views, clicks, orders, commission).startswith(expected)
