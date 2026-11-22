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


# ══ 댓글 실험 (2026-11-22): 짧은 본문 + 본문 직후 내 첫 답글 (링크 없음) ══════════════════

import publisher  # noqa: E402

REPLY_LINES = "꺼내는 게 번거로울까 봐 걸려.\n자리만 차지할까 봐 망설여져.\n\n있는 사람은 자주 써?"
SIX_SLOTS = tuple(datetime.strptime(t, "%H:%M").time() for t in ("13:00", "15:00", "17:00", "19:00", "21:00", "23:00"))


def reply_llm(reply=REPLY_LINES):
    """댓글 실험 지시가 있으면 짧은 본문 + reply_line, 길이 실험이면 짧은 본문, 아니면 긴 본문."""
    calls = []

    def llm(system, user, temperature):
        calls.append((system, user))
        payload = json.loads(user.split("계산 데이터(JSON):\n", 1)[1].split("\n\n", 1)[0])
        kw = payload["talk_topic"].split(",")[0][:14]
        hooks = [f"{kw} 있는 사람 말해줘", f"{kw}, 살까 말까", f"{kw} vs 그냥 버티기"]
        if "댓글 실험 글입니다" in user:
            body, line = f"{kw} 얘기야.\n따뜻하게 쉬고 싶잖아.\n걸리는 점은 댓글에 적어둘게.\n\n잘 샀다 vs 괜히 샀다", reply
        elif "길이 실험 글입니다" in user:
            body, line = f"{kw} 얘기야.\n{SHORT_LINES}", None
        else:
            body, line = f"{kw} 얘기야.\n{long_lines(kw)}", None
        return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                           "body": body, "reply_line": line, "topic_tag": None}, ensure_ascii=False)
    llm.calls = calls
    return llm


@pytest.fixture
def reply_cfg(talk_cfg):
    return dataclasses.replace(talk_cfg, short_talk_reply=True)


def test_reply_config_needs_short_talk_max_chars():
    assert parse_config(coupang_raw()).short_talk_reply is False
    with pytest.raises(ConfigError, match="short_talk_max_chars"):
        parse_config(coupang_raw() | {"short_talk_reply": True})
    with pytest.raises(ConfigError):
        parse_config(coupang_raw() | {"short_talk_max_chars": 120, "short_talk_reply": "yes"})
    assert parse_config(coupang_raw() | {"short_talk_max_chars": 120, "short_talk_reply": True}).short_talk_reply


def test_sabolkka_runs_the_reply_test_too():
    import yaml
    from settings import PROJECT_ROOT
    raw = yaml.safe_load((PROJECT_ROOT / "accounts/sabolkka/config.yaml").read_text(encoding="utf-8"))
    assert raw["short_talk_reply"] is True


def test_reply_post_has_short_body_and_a_linkless_reply(cfg, tariff):
    llm = reply_llm()
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="안마기, 살까 말까", now=NOW,
                                   max_chars=LIMIT, self_reply=True)
    assert post["status"] == "draft" and len(post["text"]) <= LIMIT
    assert post["reply_text"] == REPLY_LINES
    assert (post["has_link"], post["link_url"], post["variant"]) == (False, None, None)
    assert post["calc_payload"]["experiment"] == "short_reply"
    system, user = llm.calls[0]
    assert "본문 바로 뒤에 내가 다는 첫 답글" in system and "댓글 실험 글입니다" in user


@pytest.mark.parametrize("reply, problem", [
    (None, "비어 있습니다"),
    ("가" * 151, "답글이 깁니다"),
    ("자세한 건 https://example.com 에서", "링크"),
])
def test_bad_reply_is_fed_back_and_held(cfg, tariff, reply, problem):
    llm = reply_llm(reply)
    post = generator.generate_post("talk", [], tariff, cfg, llm, tip_topic="안마기, 살까 말까", now=NOW,
                                   max_chars=LIMIT, self_reply=True)
    assert post["status"] == "held"
    assert any(problem in str(p) for a in post["guard_log"]["attempts"] for p in a["problems"])
    assert len(llm.calls) == generator.MAX_ATTEMPTS


def test_reply_text_goes_through_the_guard(cfg, tariff):
    post = generator.generate_post("talk", [], tariff, cfg, reply_llm("최저가로 사면 좋아"), tip_topic="안마기, 살까 말까",
                                   now=NOW, max_chars=LIMIT, self_reply=True)
    assert post["status"] == "held"
    assert any("답글에 금지어" in str(p) for a in post["guard_log"]["attempts"] for p in a["problems"])


def test_short_post_must_not_have_a_reply():
    check = generator.length_check(LIMIT)
    assert check({"text": "짧은 글", "reply_text": None}) == []
    assert "답글이 없어야" in check({"text": "짧은 글", "reply_text": "답글"})[0]


def test_self_reply_needs_max_chars(cfg, tariff):
    with pytest.raises(ValueError, match="max_chars"):
        generator.generate_post("talk", [], tariff, cfg, reply_llm(), tip_topic="x", now=NOW, self_reply=True)


# ── 하루 세 종류, 자리는 날마다 ──────────────────────────────────────

def _talk_kind(day, hour, kind=None):
    post = _talk(day, hour)
    if kind:
        post["calc_payload"]["experiment"] = kind
    return post


def test_three_kinds_rotate_through_three_talk_slots(reply_cfg):
    cfg = dataclasses.replace(reply_cfg, max_posts_per_day=6, max_link_post_ratio=0.5, slots_kst=SIX_SLOTS)
    assert min(cfg.max_posts_per_day, len(cfg.slots_kst)) - rules.max_link_posts_per_day(cfg) == 3
    seen = []
    for i in range(3):
        day = TOMORROW + timedelta(days=i)
        placed, order = [], []
        for slot in range(3):
            kind = pipeline.talk_experiment(cfg, day, placed)
            order.append(kind)
            placed.append(_talk_kind(day, 19 + 2 * slot, kind))
        assert sorted(order, key=str) == sorted([None, "short", "short_reply"], key=str)     # 날마다 하나씩
        seen.append(tuple(order))
    assert len(set(seen)) == 3                                                              # 자리가 돈다


def test_reply_kind_is_off_without_the_setting(talk_cfg):
    day = TOMORROW
    kinds = {pipeline.talk_experiment(talk_cfg, day, [_talk(day, 19)] * n) for n in range(4)}
    assert "short_reply" not in kinds


# ── 파이프라인 ───────────────────────────────────────────────────────

def test_pipeline_makes_one_short_and_one_reply_post(reply_cfg, tariff, products):
    cfg = dataclasses.replace(reply_cfg, max_posts_per_day=6, max_link_post_ratio=0.5, slots_kst=SIX_SLOTS)
    _, posts = _generate(cfg, tariff, products, reply_llm())
    talks = [p for p in posts if p["type"] == "talk"]
    assert len(talks) == 3 and all(p["status"] == "approved" for p in talks)      # 링크 자리 3개는 비고 대화 자리 3개
    kinds = [p["calc_payload"].get("experiment") for p in talks]
    assert kinds.count("short") == 1 and kinds.count("short_reply") == 1 and kinds.count(None) == 1
    reply_post = next(p for p in talks if p["calc_payload"].get("experiment") == "short_reply")
    assert reply_post["reply_text"] == REPLY_LINES and len(reply_post["text"]) <= LIMIT
    assert all(p["reply_text"] is None for p in talks if p is not reply_post)


def test_pipeline_falls_back_when_the_reply_cannot_be_made(reply_cfg, tariff, products):
    cfg = dataclasses.replace(reply_cfg, max_posts_per_day=6, max_link_post_ratio=0.5, slots_kst=SIX_SLOTS)
    _, posts = _generate(cfg, tariff, products, reply_llm(reply=None))
    talks = [p for p in posts if p["type"] == "talk"]
    assert len(talks) == 3 and all(p["status"] == "approved" for p in talks)      # 자리를 비우지 않는다
    assert not any(p["calc_payload"].get("experiment") == "short_reply" for p in talks)


# ── 발행: 본문 직후 내 첫 답글 (기다리지 않는다, 링크 없음) ─────────────

class Api:
    def __init__(self, fail_reply=False):
        self.calls, self.n, self.fail_reply = [], 0, fail_reply

    def create_container(self, params):
        if self.fail_reply and "reply_to_id" in params:
            self.fail_reply = False
            raise publisher.ThreadsApiError("답글 실패")
        self.calls.append(params)
        self.n += 1
        return f"c{self.n}"

    def container_status(self, cid):
        return "FINISHED", None

    def publish(self, cid):
        return f"m-{cid}"


def _reply_post(scheduled):
    return {"id": "r1", "type": "talk", "status": "approved", "product_ids": [],
            "text": "안마기 있는 사람 말해줘\n\n따뜻하게 쉬고 싶잖아.\n걸리는 점은 댓글에 적어둘게.\n\n잘 샀다 vs 괜히 샀다",
            "reply_text": REPLY_LINES, "has_link": False, "link_url": None, "variant": None, "topic_tag": None,
            "hook_style": "question", "media": [],
            "calc_payload": {"talk_topic": "안마기, 살까 말까", "choice_numbers": [1, 2, 3], "experiment": "short_reply"},
            "scheduled_at": scheduled.astimezone(timezone.utc).isoformat(), "published_at": None,
            "threads_media_id": None, "threads_reply_id": None, "created_at": scheduled.isoformat()}


def _publish(cfg, store, api, now):
    return publisher.run_once(store, cfg, env={"DRY_RUN": "false"}, api=api, link_checker=lambda u: None,
                              notifier=CollectingNotifier(), now=now.astimezone(timezone.utc),
                              exam_status=(True, "합격"), sleep=lambda _: None)


def test_reply_is_posted_right_after_the_body_without_link(cfg):
    cfg = dataclasses.replace(cfg, reply_delay_hours=1.0)          # 링크 답글은 1시간 뒤지만 댓글 실험 답글은 바로
    when = datetime(2026, 11, 3, 8, 10, tzinfo=KST)
    store, api = MemoryStore(), Api()
    store.insert_post(_reply_post(when))
    result = _publish(cfg, store, api, when + timedelta(minutes=5))
    assert result["action"] == "published" and result["reply"]
    body, reply = api.calls
    assert "reply_to_id" not in body and reply["reply_to_id"] == result["threads_media_id"]
    assert reply["text"] == REPLY_LINES and "link_attachment" not in reply
    assert store.get_post("r1")["threads_reply_id"] == result["reply"]


def test_failed_reply_is_retried_next_run(cfg):
    when = datetime(2026, 11, 3, 8, 10, tzinfo=KST)
    store, api = MemoryStore(), Api(fail_reply=True)
    store.insert_post(_reply_post(when))
    first = _publish(cfg, store, api, when + timedelta(minutes=5))
    assert first["action"] == "published" and first["reply"] is None
    assert store.get_post("r1")["threads_reply_id"] is None
    _publish(cfg, store, api, when + timedelta(minutes=35))
    assert store.get_post("r1")["threads_reply_id"]


def test_normal_talk_posts_never_get_a_reply():
    post = _reply_post(datetime(2026, 11, 3, 8, 10, tzinfo=KST))
    assert publisher.is_self_reply(post)
    assert not publisher.is_self_reply(dict(post, calc_payload={"talk_topic": "x"}))
    assert not publisher.is_self_reply(dict(post, reply_text=None))
    assert not publisher.is_self_reply(dict(post, has_link=True))
    assert not publisher.is_self_reply(dict(post, type="pick"))
