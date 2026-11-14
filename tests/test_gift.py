"""
선물지니 글 gift (2026-11-14 사용자 결정, PLAN 1-4).
sabol.kka 가 수·토요일에 링크 글 한 자리로, 계정 주인이 직접 만든 선물 추천 앱을 소개한다.
답글 첫 줄은 쿠팡 대가성 문구, 둘째 줄은 "내가 만든 앱" 문장이고, 링크는 gift_app.yaml 의 앱 주소만 통과한다.
"""
import dataclasses
import json
from datetime import date, datetime, timezone

import pytest

import generator
import gift_app
import guard
import pipeline
import publisher
import settings
from conftest import coupang_config
from settings import PROJECT_ROOT, ConfigError

SABOLKKA_GIFT = PROJECT_ROOT / "accounts" / "sabolkka" / "data" / "gift_app.yaml"
WED, THU, SAT = date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 21)


@pytest.fixture
def app():
    return gift_app.load_gift_app(SABOLKKA_GIFT)


@pytest.fixture
def six():
    return dataclasses.replace(coupang_config(), post_types=("pick", "talk", "gift"))


# ── 글감 파일 ──────────────────────────────────────────────────────────

def test_sabolkka_gift_file_loads_and_points_to_the_app(app):
    assert app.name == "선물지니" and app.url == "https://couplegift.netlify.app/" and app.host == "couplegift.netlify.app"
    assert app.facts and all(len(f) <= gift_app.MAX_FACT_CHARS for f in app.facts)
    assert any(o.season is None for o in app.occasions)


def test_seasonal_themes_only_in_their_window(app):
    mid_nov = gift_app.themes_for(app, date(2026, 11, 15))
    assert "수능 끝난 수험생 선물" in mid_nov and "크리스마스 선물" not in mid_nov
    december = gift_app.themes_for(app, date(2026, 12, 5))
    assert {"수능 끝난 수험생 선물", "크리스마스 선물"} <= set(december)
    assert "친구 생일 선물" in mid_nov and "친구 생일 선물" in gift_app.themes_for(app, date(2027, 7, 1))


def test_season_can_cross_new_year():
    o = gift_app.Occasion("연말연시", ((12, 20), (1, 10)))
    assert o.in_season(date(2026, 12, 31)) and o.in_season(date(2027, 1, 5)) and not o.in_season(date(2027, 2, 1))


@pytest.mark.parametrize("raw, message", [
    ({"app": {"name": "x", "url": "http://a.b/", "one_liner": "y", "facts": ["f"]}, "occasions": [{"theme": "t"}]}, "https"),
    ({"app": {"name": "x", "url": "https://a.b/", "one_liner": "y", "facts": []}, "occasions": [{"theme": "t"}]}, "facts"),
    ({"app": {"name": "x", "url": "https://a.b/", "one_liner": "y", "facts": ["f"]},
      "occasions": [{"theme": "t", "season": ["11-01", "13-01"]}]}, "MM-DD"),
    ({"app": {"name": "x", "url": "https://a.b/", "one_liner": "y", "facts": ["f"]},
      "occasions": [{"theme": "t", "season": ["11-01", "12-01"]}]}, "언제나"),
])
def test_bad_gift_file_is_refused(raw, message):
    with pytest.raises(ConfigError, match=message):
        gift_app.parse_gift_app(raw)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="gift_app.yaml"):
        gift_app.load_gift_app(tmp_path / "gift_app.yaml")


def test_gift_is_a_coupang_post_type_and_sabolkka_has_it_on():
    assert "gift" in settings.COUPANG_POST_TYPES and "gift" in generator.POST_TYPES
    assert "gift" in guard.LINK_POST_TYPES and "gift" not in generator.NO_LINK_TYPES
    sabolkka = settings.load_config(PROJECT_ROOT / "accounts" / "sabolkka" / "config.yaml")
    assert "gift" in sabolkka.post_types


# ── 하루 구성: 수·토요일에 링크 글 한 자리 ─────────────────────────────────

def test_gift_takes_one_link_slot_on_wednesday_and_saturday(six):
    for day in (WED, SAT):
        assert pipeline.plan_day([], six, day, False, has_picks=True, has_calc=False, has_gift=True) == \
            ["gift", "pick", "pick", "talk", "talk", "talk"]
    assert pipeline.plan_day([], six, THU, False, has_picks=True, has_calc=False, has_gift=True) == \
        ["pick", "pick", "pick", "talk", "talk", "talk"]


def test_no_gift_without_the_file_or_the_type(six):
    assert "gift" not in pipeline.plan_day([], six, WED, False, has_picks=True, has_calc=False, has_gift=False)
    no_type = dataclasses.replace(six, post_types=("pick", "talk"))
    assert "gift" not in pipeline.plan_day([], no_type, WED, False, has_picks=True, has_calc=False, has_gift=True)


def test_gift_goes_after_a_bundle(six):
    with_bundle = dataclasses.replace(six, post_types=("pick", "bundle", "talk", "gift"))
    assert pipeline.plan_day([], with_bundle, SAT, False, has_picks=True, has_calc=False, has_bundle=True,
                             has_gift=True)[:3] == ["bundle", "gift", "pick"]


def test_least_used_theme_is_picked(app):
    day = date(2026, 11, 18)
    first = generator.pick_subject([], "gift", day, gift=app)
    used = [{"type": "gift", "calc_payload": {"occasion": first["tip_topic"]}, "product_ids": []}]
    second = generator.pick_subject([], "gift", day, used, gift=app)
    assert first["key"].startswith("gift|") and second["tip_topic"] != first["tip_topic"]
    assert generator.subject_key(used[0]) == f"gift|{first['tip_topic']}"


# ── 생성·가드 ──────────────────────────────────────────────────────────

DRAFT = {"hook_candidates": [{"style": "question", "text": "수능 끝난 동생 선물, 뭐가 좋을까"},
                             {"style": "result", "text": "수능 끝난 동생 선물 고민 중"},
                             {"style": "compare", "text": "용돈 vs 선물, 수능 끝난 동생"}],
         "body": "막상 고르려면 막막하잖아\n\n그래서 선물 고르는 앱을 하나 만들어 봤어\n나이랑 성격, 예산을 고르면 AI 가 골라 줘\n\n너는 뭐 받았어?",
         "reply_line": "궁금하면 여기서 해 볼 수 있어.", "topic_tag": "선물"}


def test_generated_gift_post_has_owner_line_and_app_link(cfg, tariff, app):
    post = generator.generate_post("gift", [], tariff, cfg, lambda s, u, t: json.dumps(DRAFT, ensure_ascii=False),
                                   tip_topic="수능 끝난 수험생 선물", gift=app, now=datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert post["status"] == "draft" and post["type"] == "gift" and post["product_ids"] == []
    assert post["link_url"] == app.url and post["variant"] == "link_in_reply"
    lines = post["reply_text"].split("\n")
    assert lines[0] == cfg.disclosure_text and lines[1] == gift_app.OWNER_LINE and lines[-1] == DRAFT["reply_line"]
    assert post["calc_payload"]["occasion"] == "수능 끝난 수험생 선물"
    assert post["calc_payload"]["app"]["maker"] == "이 계정 주인이 직접 만든 앱"


def test_gift_requires_the_app_and_only_gift_takes_it(cfg, tariff, app):
    with pytest.raises(ValueError):
        generator.generate_post("gift", [], tariff, cfg, lambda *a: "{}")
    with pytest.raises(ValueError):
        generator.generate_post("talk", [], tariff, cfg, lambda *a: "{}", gift=app)


def test_guard_needs_the_owner_line_on_gift_replies(cfg, tariff, app):
    post = generator.generate_post("gift", [], tariff, cfg, lambda s, u, t: json.dumps(DRAFT, ensure_ascii=False),
                                   tip_topic="친구 생일 선물", gift=app)
    checked = dict(post)
    assert guard.check_post(checked, cfg) == []
    checked["reply_text"] = f"{cfg.disclosure_text}\n\n궁금하면 여기서 해 볼 수 있어."
    assert any("둘째 줄" in p for p in guard.check_post(checked, cfg))


# ── 발행 직전 링크 확인 ────────────────────────────────────────────────

def test_link_checker_takes_the_app_and_coupang_but_nothing_else(app):
    check = publisher.make_gift_link_checker(app)
    assert check("https://couplegift.netlify.app/") is None
    assert check("https://link.coupang.com/a/abc123") is None
    assert check("https://example.com/") is not None
    assert check("http://couplegift.netlify.app/") is not None
