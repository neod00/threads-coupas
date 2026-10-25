"""
2026-10-16 변경 (PLAN v4) 테스트.
  A. 참여형(talk): 링크·상품 없는 질문 글
  B. 시즌 모음(bundle): 테마 하나에 상품 3개, 검색 결과 링크 1개를 첫 답글에
  D. 일일 요약에 '답해 줄 답글'과 '오늘 둘러볼 검색어' (자동 답글 아님 — 규칙 6)
  E. 진짜 후기: 사용자 경험 메모가 있는 상품만 경험 표현 허용
"""
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import bundles
import digest
import generator
import guard
import pipeline
import watch_replies
from notify import CollectingNotifier
from products import Product, parse_products
from settings import KST, ConfigError
from store import MemoryStore

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
RAW_BUNDLE = {"id": "t_house", "theme": "집들이 선물", "keyword": "집들이 선물", "link": "https://link.coupang.com/a/bbb111",
              "season": None, "items": [{"name": "F사 원목 냄비받침", "note": "트리 모양 원목 냄비받침"},
                                        {"name": "G사 퍼퓸바 세트", "note": "우디 향 퍼퓸바 3종 세트"},
                                        {"name": "H사 호텔 수건 세트", "note": "호텔식 수건 5종 세트"}]}
BUNDLE = bundles.parse_bundles([RAW_BUNDLE])[0]


def fake_llm(hooks, body, reply_line="관련 상품은 여기서 더 볼 수 있어요."):
    calls = []

    def llm(system, user, temperature):
        calls.append({"system": system, "user": user})
        return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                           "body": body, "reply_line": reply_line, "topic_tag": None}, ensure_ascii=False)
    llm.calls = calls
    return llm


# ── A. 참여형 ────────────────────────────────────────────────────────────

TALK_BODY = "겨울 밤 이불 속 온기 담당이요.\n\n1번 전기장판\n2번 온수매트\n3번 그냥 이불 두 겹\n\n답글로 번호만 남겨 주세요."


def test_talk_post_has_no_link_and_allows_choice_numbers(cfg, tariff):
    llm = fake_llm(["전기장판 vs 온수매트, 하나만 고르면?"] * 3, TALK_BODY)
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="전기장판 vs 온수매트, 하나만 고른다면", now=NOW)
    assert post["status"] == "draft" and post["type"] == "talk"
    assert (post["has_link"], post["link_url"], post["reply_text"], post["variant"]) == (False, None, None, None)
    assert post["product_ids"] == [] and post["calc_payload"]["talk_topic"].startswith("전기장판")
    assert "유형: talk" in llm.calls[0]["user"]


def test_talk_other_numbers_blocked(cfg, tariff):
    llm = fake_llm(["전기장판 vs 온수매트, 하나만 고르면?"] * 3, TALK_BODY.replace("1번 전기장판", "7번 전기장판"))
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="x", now=NOW, max_attempts=1)
    assert post["status"] == "held"


def test_talk_topics_are_unique_and_clean(cfg):
    assert len(generator.TALK_TOPICS) >= 30 and len(set(generator.TALK_TOPICS)) == len(generator.TALK_TOPICS)
    for t in generator.TALK_TOPICS:
        assert not [w for w in cfg.banned_words if w in t], t


# ── B. 시즌 모음 ─────────────────────────────────────────────────────────

BUNDLE_BODY = ("빈손으로 가기엔 좀 그럴 때 있죠.\n\n1. F사 원목 냄비받침\n트리 모양 원목 냄비받침이에요.\n\n"
               "2. G사 퍼퓸바 세트\n우디 향 퍼퓸바 3종 세트예요.\n\n3. H사 호텔 수건 세트\n호텔식 수건 5종 세트예요.\n\n셋 중 몇 번이 끌려요?")


def test_bundle_post_links_search_result_in_reply(cfg, tariff):
    post = generator.generate_post("bundle", [], tariff, cfg, fake_llm(["집들이 선물, 뭐 들고 갈지 고민되죠?"] * 3, BUNDLE_BODY),
                                   now=NOW, bundle=BUNDLE)
    assert post["status"] == "draft"
    assert (post["link_url"], post["variant"], post["product_ids"]) == (BUNDLE.link, "link_in_reply", [])
    assert post["reply_text"].split("\n")[0] == cfg.disclosure_text and "http" not in post["text"]
    assert post["calc_payload"]["bundle_id"] == "t_house" and len(post["calc_payload"]["items"]) == 3
    assert generator.subject_key(post) == "bundle|t_house"


def test_bundle_requires_theme(cfg, tariff):
    with pytest.raises(ValueError):
        generator.generate_post("bundle", [], tariff, cfg, fake_llm(["x"] * 3, "y"), now=NOW)


def test_bundle_season_window():
    def b(season):
        return dataclasses.replace(BUNDLE, season=season)
    assert b(("10-25", "11-11")).in_season(date(2026, 11, 11))
    assert not b(("10-25", "11-11")).in_season(date(2026, 11, 12))
    assert b(("10-15", "02-28")).in_season(date(2027, 1, 10))          # 해를 넘기는 기간
    assert not b(("10-15", "02-28")).in_season(date(2026, 6, 1))
    assert b(None).in_season(date(2026, 6, 1))


def test_bundle_file_validation():
    for bad in ([dict(RAW_BUNDLE, items=RAW_BUNDLE["items"][:2])],                                # 상품 3개가 아님
                [dict(RAW_BUNDLE, link="https://example.com/x")],                                    # 파트너스 링크 아님
                [dict(RAW_BUNDLE, season=["11-31", "12-01"])],                                      # 없는 날짜
                [dict(RAW_BUNDLE, items=[{"name": "a", "note": "써보니 좋음"}] * 3)],                 # 경험 표현
                [dict(RAW_BUNDLE, items=[{"name": "a", "note": "아이 방에 둬도 안전"}] * 3)]):          # 안전 주장
        with pytest.raises(ConfigError):
            bundles.parse_bundles(bad)


def test_real_bundles_file():
    real = bundles.load_bundles()
    assert len(real) >= 6 and len({b.bundle_id for b in real}) == len(real)
    assert all(len(b.items) == 3 for b in real)
    # 이 프로젝트 기간(10월 중순~겨울)에 늘 하나 이상은 시즌이다
    for d in (date(2026, 10, 20), date(2026, 11, 5), date(2026, 12, 10), date(2027, 1, 15)):
        assert any(b.in_season(d) for b in real), d


def test_plan_day_bundle_takes_first_link_slot(cfg):
    v4 = dataclasses.replace(cfg, post_types=("pick", "bundle", "talk"))
    plan = pipeline.plan_day([], v4, date(2026, 10, 20), True, has_picks=True, has_calc=True, has_bundle=True)
    assert plan == ["bundle", "pick", "talk", "talk"]
    plan = pipeline.plan_day([], v4, date(2026, 10, 20), True, has_picks=True, has_calc=True, has_bundle=False)
    assert plan == ["pick", "pick", "talk", "talk"]


def test_bundle_every_other_day():
    house = dataclasses.replace(BUNDLE, season=None)
    recent = [{"type": "bundle", "created_at": (NOW - timedelta(hours=24)).isoformat()}]
    old = [{"type": "bundle", "created_at": (NOW - timedelta(hours=48)).isoformat()}]
    assert pipeline.bundle_available([house], [], date(2026, 11, 3), NOW)
    assert not pipeline.bundle_available([house], recent, date(2026, 11, 3), NOW)
    assert pipeline.bundle_available([house], old, date(2026, 11, 3), NOW)
    assert not pipeline.bundle_available([dataclasses.replace(BUNDLE, season=("12-01", "12-31"))], [], date(2026, 11, 3), NOW)


def test_run_generate_makes_bundle_pick_and_talks(cfg, tariff):
    v4 = dataclasses.replace(cfg, post_types=("pick", "bundle", "talk"))
    picks = [Product(f"k{i}", f"상품{i}", "pet", None, None, f"https://link.coupang.com/a/k{i}", "강아지 장난감") for i in range(3)]

    def llm(system, user, temperature):
        kind = user.split("\n", 1)[0]
        if "talk" in kind:
            hooks, body = ["전기장판 vs 온수매트, 하나만 고르면?"] * 3, TALK_BODY
            topic = json.loads(user.split("계산 데이터(JSON):\n", 1)[1].split("\n\n최근에 올린 글", 1)[0])["talk_topic"]
            # 주제마다 다른 훅·글 (같으면 코드 R9 가 반복으로 막는다)
            hooks, body = [topic[:28]] * 3, f"오늘의 질문은 {topic} 입니다.\n\n답글로 알려 주세요."
        elif "bundle" in kind:
            hooks, body = ["집들이 선물, 뭐 들고 갈지 고민되죠?"] * 3, BUNDLE_BODY
        else:
            hooks, body = ["강아지 장난감 하나 볼래요?"] * 3, "강아지 장난감이에요.\n\n집에 장난감 몇 개 있어요?"
        return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                           "body": body, "reply_line": "여기서 볼 수 있어요.", "topic_tag": None}, ensure_ascii=False)

    review = lambda system, user: ("end_turn", json.dumps({c: {"verdict": "pass", "reason": "", "hook_only": False}
                                                           for c in __import__("reviewer").ITEM_CODES}))
    store = MemoryStore()
    result = pipeline.run_generate(store, v4, tariff, picks, complete=llm, review_call=review, notifier=CollectingNotifier(),
                                   now=NOW, env={}, bundles=[BUNDLE])
    types = [p["type"] for p in result["posts"]]
    assert types == ["bundle", "pick", "talk", "talk"]
    assert all(p["status"] == "approved" for p in result["posts"]), [p["guard_log"] for p in result["posts"]]
    talks = [p for p in result["posts"] if p["type"] == "talk"]
    assert talks[0]["calc_payload"]["talk_topic"] != talks[1]["calc_payload"]["talk_topic"]


# ── D. 사람이 하는 소통 ───────────────────────────────────────────────────

class ConvApi:
    def __init__(self, replies):
        self.replies = replies

    def conversation(self, media_id):
        return self.replies


def test_reply_inbox_collects_others_replies_but_digest_omits_them(cfg):
    store = MemoryStore()
    published = {"id": "p1", "type": "pick", "status": "published", "threads_media_id": "m1", "text": "훅\n\n본문",
                 "published_at": (NOW - timedelta(hours=3)).isoformat(), "created_at": (NOW - timedelta(hours=20)).isoformat()}
    store.insert_post(published)
    api = ConvApi([{"id": "r1", "text": "저는 2번이요", "username": "kim", "permalink": "https://www.threads.com/@kim/post/abc",
                    "timestamp": (NOW - timedelta(hours=1)).isoformat()},
                   {"id": "r2", "text": "[광고] ...", "username": "openbrain7777", "is_reply_owned_by_me": True}])
    watch_replies.watch(store, api, CollectingNotifier(), NOW)
    inbox = store.get_state(watch_replies.INBOX_KEY)
    assert [r["reply_id"] for r in inbox] == ["r1"]                  # 내 답글은 빼고, 자동으로 답하지 않는다
    text = digest.build_digest(store, cfg, NOW, dry_run=False)
    assert "답해 줄 답글" not in text and "@kim" not in text          # 답글 목록은 텔레그램에 넣지 않는다 (2026-10-25)
    assert "오늘 둘러볼 검색어:" in text


def test_browse_keywords_rotate_daily(cfg):
    store = MemoryStore()
    a = digest.build_digest(store, cfg, NOW, dry_run=False)
    b = digest.build_digest(store, cfg, NOW + timedelta(days=1), dry_run=False)
    line = lambda t: next(l for l in t.splitlines() if l.startswith("오늘 둘러볼 검색어"))
    assert line(a) != line(b)


# ── E. 진짜 후기 ─────────────────────────────────────────────────────────

def _row(**kw):
    row = {"product_id": "x1", "name": "E사 전기요", "category": "living", "power_w": "", "default_hours": "",
           "link": "https://link.coupang.com/a/abc", "note": "물세탁 되는 전기요", "experience": ""}
    row.update(kw)
    return row


def test_experience_column_is_optional_and_checked():
    assert parse_products([_row()])[0].experience == ""
    p = parse_products([_row(experience="두 번째 겨울째 씀. 2단으로 켜고 잠")])[0]
    assert p.experience.startswith("두 번째")
    with pytest.raises(ConfigError):
        parse_products([_row(experience="아이 방에 둬도 안전함")])            # 안전 주장은 경험 메모에도 금지
    with pytest.raises(ConfigError):
        parse_products([_row(experience="가" * 201)])


def test_experience_words_allowed_only_with_memo(cfg, tariff):
    body = "써보니 2단으로 켜고 자면 새벽에 안 깨요.\n\n몇 단으로 켜세요?"
    plain = Product("x1", "E사 전기요", "living", None, None, "https://link.coupang.com/a/abc", "물세탁 되는 전기요")
    real = dataclasses.replace(plain, experience="2단으로 켜고 자면 새벽에 안 깸")
    llm = lambda: fake_llm(["전기요 몇 단으로 켜고 자요?"] * 3, body)
    without = generator.generate_post("pick", [plain], tariff, cfg, llm(), now=NOW, max_attempts=1)
    assert without["status"] == "held"
    assert any("[G4]" in p and "써보니" in p for a in without["guard_log"]["attempts"] for p in a["problems"])
    with_memo = generator.generate_post("pick", [real], tariff, cfg, llm(), now=NOW)
    assert with_memo["status"] == "draft"
    assert with_memo["calc_payload"]["product"]["experience"] == real.experience
    # 경험 메모가 있어도 다른 금지어는 그대로 막는다
    post = dict(with_memo, text=with_memo["text"] + "\n무조건 추천")
    assert any(p.startswith("[G4]") and "무조건" in p for p in guard.check_post(post, cfg))
