"""
길이 실험 (2026-11-22 사용자 결정): 하루 대화(talk) 글 하나만 120자 이하로 만들고 calc_payload.experiment 에 표시한다.
  - 11월 글 76개에서 짧은 글의 조회가 높았다 (121~160자 중앙 94, 161~200자 41). 120자 이하는 써 본 적이 없다.
  - 실험 글의 자리는 날마다 돈다. 끝내 글자 수를 못 맞추면 보통 길이로 다시 만든다 (자리를 비우지 않는다).
"""
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import generator
import pipeline
import reviewer
import rules
from conftest import coupang_raw
from notify import CollectingNotifier
from settings import KST, ConfigError, parse_config
from store import MemoryStore

NOW = datetime(2026, 11, 2, 20, 0, tzinfo=KST)
TOMORROW = NOW.date() + timedelta(days=1)
LIMIT = 120


def long_lines(kw):
    """주제마다 다른 긴 본문 (같은 본문이면 코드 R9 반복 판정에 걸린다)."""
    return (f"{kw} 두고 따뜻하게 쉬고 싶잖아.\n\n{kw} 꺼내는 게 번거로울까 봐\n{kw} 자리만 차지할까 봐\n"
            f"{kw} 결국 안 쓸까 봐 망설여져.\n\n{kw} 있으면 자주 꺼내 써?\n\n잘 샀다 vs 괜히 샀다, 솔직하게")


SHORT_LINES = "따뜻하게 쉬고 싶잖아.\n자리만 차지할까 봐 망설여져.\n\n잘 샀다 vs 괜히 샀다"


def talk_llm(obey=True):
    """주제마다 다른 대화 글을 쓴다. 길이 실험 지시가 있으면 짧게 (obey=False 면 지시를 무시하고 길게)."""
    calls = []

    def llm(system, user, temperature):
        calls.append(user)
        payload = json.loads(user.split("계산 데이터(JSON):\n", 1)[1].split("\n\n", 1)[0])
        kw = payload["talk_topic"].split(",")[0][:14]
        hooks = [f"{kw} 있는 사람 말해줘", f"{kw}, 살까 말까", f"{kw} vs 그냥 버티기"]
        body = SHORT_LINES if (obey and "길이 실험 글입니다" in user) else long_lines(kw)
        return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                           "body": f"{kw} 얘기야.\n{body}", "reply_line": None, "topic_tag": None}, ensure_ascii=False)
    llm.calls = calls
    return llm


def approve(system, user):
    return "end_turn", json.dumps({c: {"verdict": "pass", "reason": "", "hook_only": False} for c in reviewer.ITEM_CODES})


@pytest.fixture
def talk_cfg(cfg):
    return dataclasses.replace(cfg, post_types=("talk",), short_talk_max_chars=LIMIT)


# ── 설정 ─────────────────────────────────────────────────────────────

def test_config_is_off_by_default_and_parses_both_keys():
    assert (parse_config(coupang_raw()).short_talk_max_chars, parse_config(coupang_raw()).short_talk_until) == (None, None)
    cfg = parse_config(coupang_raw() | {"short_talk_max_chars": 120, "short_talk_until": "2026-12-07"})
    assert (cfg.short_talk_max_chars, cfg.short_talk_until) == (120, date(2026, 12, 7))


@pytest.mark.parametrize("bad", [{"short_talk_max_chars": 59}, {"short_talk_max_chars": 9999}, {"short_talk_max_chars": True},
                                 {"short_talk_max_chars": "120"}, {"short_talk_until": "내일"}])
def test_config_rejects_bad_values(bad):
    with pytest.raises(ConfigError):
        parse_config(coupang_raw() | bad)


def test_sabolkka_test_runs_two_weeks():
    import yaml
    from settings import PROJECT_ROOT
    raw = yaml.safe_load((PROJECT_ROOT / "accounts/sabolkka/config.yaml").read_text(encoding="utf-8"))
    assert raw["short_talk_max_chars"] == 120 and raw["short_talk_until"] == "2026-12-07"


# ── 생성 ─────────────────────────────────────────────────────────────

def test_short_talk_post_is_within_limit_and_marked(cfg, tariff):
    llm = talk_llm()
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="안마기, 살까 말까", now=NOW, max_chars=LIMIT)
    assert post["status"] == "draft" and len(post["text"]) <= LIMIT
    assert post["calc_payload"] == {"talk_topic": "안마기, 살까 말까", "choice_numbers": [1, 2, 3], "experiment": "short"}
    assert f"글 전체를 {LIMIT}자 이하로" in llm.calls[0]
    assert "experiment" not in llm.calls[0]           # 표시는 생성 뒤에 붙는다 (LLM 이 보는 데이터는 그대로)


def test_normal_talk_post_has_no_mark_and_no_length_rule(cfg, tariff):
    llm = talk_llm()
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="안마기, 살까 말까", now=NOW)
    assert post["status"] == "draft" and len(post["text"]) > LIMIT
    assert "experiment" not in post["calc_payload"] and "길이 실험" not in llm.calls[0]


def test_too_long_draft_is_fed_back_then_held(cfg, tariff):
    llm = talk_llm(obey=False)
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="안마기, 살까 말까", now=NOW, max_chars=LIMIT)
    assert post["status"] == "held" and generator.too_long_only(post)
    assert len(llm.calls) == generator.MAX_ATTEMPTS and "[길이] 글이 깁니다" in llm.calls[1]
    assert post["review_attempts"] == 0               # 글자 수에서 걸리면 검수자를 부르지 않는다


def test_too_long_only_is_false_for_other_holds():
    assert not generator.too_long_only({"guard_log": {"attempts": [{"problems": ["[R3] uncertain: 안전 암시"]}]}})
    assert not generator.too_long_only({"guard_log": {"attempts": [{"problems": ["[길이] 글이 깁니다: 130자", "[G4] 경험"]}]}})
    assert not generator.too_long_only({"guard_log": {"attempts": []}})


def test_only_korean_talk_posts_can_be_short(cfg, tariff, products):
    with pytest.raises(ValueError, match="talk"):
        generator.generate_post("pick", [products[0]], tariff, cfg, talk_llm(), now=NOW, max_chars=LIMIT)


# ── 그날 한 개, 자리는 날마다 ────────────────────────────────────────

def _talk(day, hour, short=False):
    when = datetime(day.year, day.month, day.day, hour, 0, tzinfo=KST).astimezone(timezone.utc).isoformat()
    return {"type": "talk", "status": "approved", "has_link": False, "scheduled_at": when,
            "calc_payload": {"talk_topic": "x", **({"experiment": "short"} if short else {})}}


def test_test_post_position_rotates_by_day(talk_cfg):
    cfg = talk_cfg
    slots = min(cfg.max_posts_per_day, len(cfg.slots_kst)) - rules.max_link_posts_per_day(cfg)
    assert slots >= 2
    days = [TOMORROW + timedelta(days=i) for i in range(slots * 2)]
    picks = [next(n for n in range(slots) if pipeline.short_talk_due(cfg, d, [_talk(d, 19 + i) for i in range(n)])) for d in days]
    assert sorted(set(picks)) == list(range(slots)) and picks[:slots] == picks[slots:]


def test_only_one_test_post_a_day_and_not_after_until(talk_cfg):
    day = next(TOMORROW + timedelta(days=i) for i in range(7) if (TOMORROW + timedelta(days=i)).toordinal() % 2 == 1)
    assert pipeline.short_talk_due(talk_cfg, day, [_talk(day, 19)])
    assert not pipeline.short_talk_due(talk_cfg, day, [_talk(day, 19, short=True)])
    assert not pipeline.short_talk_due(talk_cfg, day, [])                       # 아직 차례가 아니다
    assert not pipeline.short_talk_due(dataclasses.replace(talk_cfg, short_talk_until=day - timedelta(days=1)), day, [_talk(day, 19)])
    assert pipeline.short_talk_due(dataclasses.replace(talk_cfg, short_talk_until=day), day, [_talk(day, 19)])
    assert not pipeline.short_talk_due(dataclasses.replace(talk_cfg, short_talk_max_chars=None), day, [_talk(day, 19)])


# ── 파이프라인 ───────────────────────────────────────────────────────

def _generate(cfg, tariff, products, llm, now=NOW):
    store = MemoryStore()
    result = pipeline.run_generate(store, cfg, tariff, products, complete=llm, review_call=approve,
                                   notifier=CollectingNotifier(), now=now, env={})
    return store, result["posts"]


def test_pipeline_makes_exactly_one_short_talk_post(talk_cfg, tariff, products):
    store, posts = _generate(talk_cfg, tariff, products, talk_llm())
    talks = [p for p in posts if p["type"] == "talk"]
    assert len(talks) >= 2 and all(p["status"] == "approved" for p in talks)
    short = [p for p in talks if p["calc_payload"].get("experiment") == "short"]
    assert len(short) == 1 and len(short[0]["text"]) <= LIMIT
    assert all(len(p["text"]) > LIMIT for p in talks if p not in short)
    assert store.get_post(short[0]["id"])["calc_payload"]["experiment"] == "short"        # DB 에 표시가 남는다


def test_pipeline_falls_back_to_a_normal_post_when_the_limit_cannot_be_met(talk_cfg, tariff, products):
    _, posts = _generate(talk_cfg, tariff, products, talk_llm(obey=False))
    talks = [p for p in posts if p["type"] == "talk"]
    assert len(talks) >= 2 and all(p["status"] == "approved" for p in talks)             # 자리를 비우지 않는다
    assert not any(p["calc_payload"].get("experiment") for p in talks)


def test_pipeline_without_the_setting_makes_no_short_post(talk_cfg, tariff, products):
    cfg = dataclasses.replace(talk_cfg, short_talk_max_chars=None)
    _, posts = _generate(cfg, tariff, products, talk_llm())
    assert posts and not any(p["calc_payload"].get("experiment") for p in posts)
