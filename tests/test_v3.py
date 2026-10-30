"""
2026-10-10 변경 (PLAN v3) 테스트.
  - 링크와 대가성 문구는 첫 답글에만 둔다 (규칙 3 개정)
  - 소개(pick) 글: 전기요금 계산 없는 상품 소개, 소비전력이 없는 상품
  - 하루 구성: 링크 글의 첫 자리는 항상 pick
"""
import json
from datetime import date, datetime, timezone

import pytest

import generator
import guard
import pipeline
import publisher
from products import Product, load_products, parse_products
from settings import ConfigError

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
PICK = Product("k1", "C사 강아지 LED 목걸이", "pet", None, None, "https://link.coupang.com/a/kkk111",
               "강아지 목에 거는 LED 라이트, 충전식, 방수")
REPLY_LINE = "제품은 여기에서 볼 수 있어요."
PICK_BODY = "밤에 산책하면\n잘 안 보일 때 있죠?\n\n목에 거는 LED 라이트가 있어요.\n충전식이고 방수예요.\n\n밤 산책 자주 나가세요?"


def pick_llm(body=PICK_BODY, reply_line=REPLY_LINE):
    hooks = [{"style": "question", "text": "강아지 밤 산책, 뭐 채우고 나가세요?"},
             {"style": "result", "text": "강아지 목에 거는 LED 라이트가 있어요"},
             {"style": "compare", "text": "밤 산책, 목줄만 vs 불빛까지"}]
    calls = []

    def llm(system, user, temperature):
        calls.append({"system": system, "user": user})
        return json.dumps({"hook_candidates": hooks, "body": body, "reply_line": reply_line, "topic_tag": "강아지산책"},
                          ensure_ascii=False)
    llm.calls = calls
    return llm


# ── 소개(pick) 글 ────────────────────────────────────────────────────────

def test_pick_post_has_link_and_disclosure_only_in_reply(cfg, tariff):
    llm = pick_llm()
    post = generator.generate_post("pick", [PICK], tariff, cfg, llm, now=NOW)
    assert post["status"] == "draft" and post["type"] == "pick"
    assert post["text"].split("\n")[0] == "강아지 밤 산책, 뭐 채우고 나가세요?"
    assert cfg.disclosure_text not in post["text"] and "http" not in post["text"]
    assert post["reply_text"] == f"{cfg.disclosure_text}\n\n{REPLY_LINE}"
    assert (post["has_link"], post["link_url"], post["variant"]) == (True, PICK.link, "link_in_reply")
    assert post["calc_payload"] == {"product": {"name": PICK.name, "category": "pet", "note": PICK.note}}
    assert "유형: pick" in llm.calls[0]["user"] and "써 본 후기가 아닙니다" in llm.calls[0]["user"]


def test_pick_numbers_must_come_from_name_or_note(cfg, tariff):
    post = generator.generate_post("pick", [PICK], tariff, cfg, pick_llm(body=PICK_BODY.replace("충전식이고", "8시간 가는 충전식이고")),
                                   now=NOW, max_attempts=1)
    assert post["status"] == "held"
    assert any(p.startswith("[G3]") for a in post["guard_log"]["attempts"] for p in a["problems"])


def test_pick_needs_no_assumption_text(cfg, tariff):
    post = generator.generate_post("pick", [PICK], tariff, cfg, pick_llm(), now=NOW)
    assert not any(p.startswith("[G6]") for p in guard.check_post(post, cfg))


def test_pick_experience_words_blocked_by_guard(cfg, tariff):
    post = generator.generate_post("pick", [PICK], tariff, cfg, pick_llm(body="막상 써보니 좋아요.\n\n밤 산책 자주 나가세요?"),
                                   now=NOW, max_attempts=1)
    assert post["status"] == "held"
    assert any(p.startswith("[G4]") for a in post["guard_log"]["attempts"] for p in a["problems"])


# ── 발행: 본문에는 링크·문구 없음, 답글에 문구 + 링크 첨부 (규칙 3) ─────────────────

def _approved_pick(cfg, tariff):
    post = generator.generate_post("pick", [PICK], tariff, cfg, pick_llm(), now=NOW)
    post.update(status="approved", threads_media_id="111")
    return post


def test_container_params_put_link_on_reply_only(cfg, tariff):
    post = _approved_pick(cfg, tariff)
    body = publisher.container_params(post)
    assert "link_attachment" not in body and cfg.disclosure_text not in body["text"]
    reply = publisher.container_params(post, reply_to_id="111")
    assert reply["link_attachment"] == PICK.link and reply["reply_to_id"] == "111"
    assert reply["text"].split("\n")[0] == cfg.disclosure_text


def test_publish_refuses_link_reply_without_disclosure(cfg, tariff):
    post = _approved_pick(cfg, tariff)
    post["reply_text"] = REPLY_LINE                      # 대가성 문구가 빠진 답글
    assert any("답글 첫 줄" in p for p in guard.check_post(post, cfg))
    post["reply_text"] = None                            # 답글 자체가 없음
    assert any("답글이 없습니다" in p for p in guard.check_post(post, cfg))


# ── 상품 파일 ────────────────────────────────────────────────────────────

def _row(**kw):
    row = {"product_id": "x1", "name": "이름", "category": "pet", "power_w": "", "default_hours": "",
           "link": "https://link.coupang.com/a/abc", "note": "메모"}
    row.update(kw)
    return row


def test_products_without_power_are_pick_only():
    p = parse_products([_row()])[0]
    assert p.power_w is None and p.default_hours is None and not p.has_power
    assert parse_products([_row(power_w="900", default_hours="4", category="heater")])[0].has_power


def test_power_and_hours_must_come_together():
    with pytest.raises(ConfigError):
        parse_products([_row(power_w="900")])
    with pytest.raises(ConfigError):
        parse_products([_row(default_hours="4")])


def test_real_products_file_has_picks_and_calc_products():
    products = load_products()
    picks = [p for p in products if not p.has_power]
    assert len(picks) >= 10 and len(products) - len(picks) >= 5
    # 소개 글 상품은 pick 주제로만, 계산 상품은 calc 주제로만 나온다
    assert {s["picked"][0].product_id for s in generator.subjects(products, "pick")} == {p.product_id for p in picks}
    assert all(s["picked"][0].has_power for s in generator.subjects(products, "calc"))
    assert all(p.has_power for s in generator.subjects(products, "compare") for p in s["picked"])


def test_pick_subjects_rotate_without_repeats(tariff):
    picks = [Product(f"k{i}", f"상품{i}", "pet", None, None, f"https://link.coupang.com/a/k{i}", "메모") for i in range(4)]
    used, seen = [], []
    for i in range(4):
        s = generator.pick_subject(picks, "pick", date(2026, 10, 11), used, offset=i)
        seen.append(s["key"])
        used.append({"type": "pick", "product_ids": [s["picked"][0].product_id], "calc_payload": {}})
        assert generator.subject_key(used[-1]) == s["key"]
    assert len(set(seen)) == 4


# ── 하루 구성 ────────────────────────────────────────────────────────────

def test_plan_day_puts_pick_first(cfg):
    days = [date(2026, 10, 11 + i) for i in range(3)]
    plans = [pipeline.plan_day([], cfg, d, True, has_picks=True, has_calc=True) for d in days]
    for plan in plans:
        assert len(plan) == 4 and plan[0] == "pick" and plan[2:] == ["tip", "tip"]     # 링크 글 2개(60% 이하) + tip 2개
    assert sorted(plan[1] for plan in plans) == ["calc", "compare", "pick"]          # 둘째 링크 자리는 날마다 돈다


def test_plan_day_without_picks_keeps_old_mix(cfg):
    assert sorted(pipeline.plan_day([], cfg, date(2026, 10, 11), True)) == ["calc", "compare", "tip", "tip"]


def test_plan_day_with_only_picks(cfg):
    assert pipeline.plan_day([], cfg, date(2026, 10, 11), False, has_picks=True, has_calc=False) == ["pick", "pick", "tip", "tip"]


# ── 2026-10-16: 글 유형 끄기 (config.yaml post_types) ──────────────────────────────

def test_post_types_limit_plan(cfg):
    import dataclasses
    picks_only = dataclasses.replace(cfg, post_types=("pick",))
    for d in range(11, 14):
        assert pipeline.plan_day([], picks_only, date(2026, 10, d), True, has_picks=True, has_calc=True) == ["pick", "pick"]
    # 소개 상품이 없는데 pick 만 켜져 있으면 아무것도 만들지 않는다
    assert pipeline.plan_day([], picks_only, date(2026, 10, 11), True, has_picks=False, has_calc=True) == []
    no_tip = dataclasses.replace(cfg, post_types=("pick", "calc"))
    assert "tip" not in pipeline.plan_day([], no_tip, date(2026, 10, 11), False, has_picks=True, has_calc=True)


def test_project_config_is_app_account():
    # 2026-10-30: openbrain 은 앱 홍보 계정 (쿠팡 글을 만들지 않는다)
    from settings import APP_POST_TYPES, load_config
    cfg = load_config()
    assert cfg.content == "app" and cfg.post_types == APP_POST_TYPES


def test_post_types_validation():
    import copy
    import yaml
    from conftest import coupang_raw
    from settings import APP_POST_TYPES, PROJECT_ROOT, parse_config
    raw = coupang_raw()
    for bad in ([], ["review"], "pick", ["launch"]):          # 쿠팡 계정은 앱 글 유형을 쓸 수 없다
        broken = copy.deepcopy(raw)
        broken["post_types"] = bad
        with pytest.raises(ConfigError):
            parse_config(broken)
    del raw["post_types"]
    assert parse_config(raw).post_types == ("calc", "compare", "pick", "tip")     # 없으면 전부
    # 앱 계정(openbrain): 앱 글 유형만, 링크 글(launch·feature) 하나는 있어야 한다, 없으면 앱 글 전부
    app = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    for bad in (["pick"], ["devlog", "ask"]):
        with pytest.raises(ConfigError):
            parse_config({**app, "post_types": bad})
    del app["post_types"]
    assert parse_config(app).post_types == APP_POST_TYPES
    with pytest.raises(ConfigError, match="만든 앱"):
        parse_config({**app, "disclosure_text": "[광고] 앱 소개"})


def test_post_type_names_match_generator():
    import settings
    assert settings.COUPANG_POST_TYPES == generator.POST_TYPES
    assert settings.APP_POST_TYPES == generator.APP_TYPES
    assert settings.POST_TYPE_NAMES == generator.POST_TYPES + generator.APP_TYPES
