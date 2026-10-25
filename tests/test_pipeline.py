"""
Step 7 생성 파이프라인 테스트 (generate.yml 이 부르는 pipeline.run_generate).
LLM·검수자는 가짜지만 코드 가드·슬롯 규칙·자동 정지·저장소는 실제 코드로 돈다.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import autostop
import guard
import pipeline
import publisher
import reviewer
import rules
from notify import CollectingNotifier
from settings import KST
from store import MemoryStore

NOW = datetime(2026, 11, 2, 20, 0, tzinfo=KST)       # generate.yml 실행 시각
TOMORROW = NOW.date() + timedelta(days=1)


def smart_llm(system, user, temperature):
    """계산 데이터(JSON)를 읽어 그 값으로 가드를 통과하는 초안을 만든다."""
    payload = json.loads(user.split("계산 데이터(JSON):\n", 1)[1].split("\n\n이전 초안", 1)[0].split("\n\n최근에 올린 글", 1)[0])
    if "tip_topic" in payload:
        # 주제마다 다른 글을 쓴다 (같은 글이면 코드 R9 반복 판정에 걸린다)
        topic = payload["tip_topic"]
        hooks = [topic[:24] + "?", topic[:24], topic[:22] + " 비교"]
        body = f"{topic}.\n\n{topic}을 오늘 확인해 보세요."
    elif "a" in payload:
        a, b = payload["a"], payload["b"]
        hooks = [f"두 제품 한 달 {a['monthly_kwh']}kWh 차이는?", f"한 달 {a['monthly_kwh']}kWh와 {b['monthly_kwh']}kWh",
                 f"두 제품 한 달 차이 {payload['cost_diff_label']}"]
        body = (f"{a['name']}은 한 달 {a['monthly_kwh']}kWh, {b['name']}은 {b['monthly_kwh']}kWh입니다.\n"
                f"{payload['assumption_text']}입니다.\n\n요금만 보면 소비전력이 낮은 쪽이 부담이 적습니다.")
    else:
        kwh = payload["monthly_kwh"]
        hooks = [f"한 달 {kwh}kWh면 얼마일까요?", f"하루 {payload['hours_per_day']}시간이면 한 달 {kwh}kWh",
                 f"한 달 {kwh}kWh, 줄이면 달라질까요?"]
        body = (f"{payload['assumption_text']}으로 약 {payload['est_added_cost_label']}이 추가로 나옵니다.\n\n"
                "사용시간을 줄이면 요금도 그만큼 줄어듭니다.")
    return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                       "body": body, "reply_line": "소비전력 표기는 상품 페이지에서 확인할 수 있습니다.", "topic_tag": "전기요금"},
                      ensure_ascii=False)


def review(verdict="pass"):
    items = {c: {"verdict": "pass", "reason": "", "hook_only": False} for c in reviewer.ITEM_CODES}
    if verdict != "pass":
        items["R3"] = {"verdict": verdict, "reason": "애매함", "hook_only": False}
    return lambda system, user: ("end_turn", json.dumps(items))


@pytest.fixture
def env():
    return MemoryStore(), CollectingNotifier()


def generate(env, cfg, tariff, products, review_call=None, **kw):
    store, notifier = env
    return pipeline.run_generate(store, cfg, tariff, products, complete=smart_llm,
                                 review_call=review_call or review(), notifier=notifier, now=NOW, env={}, **kw)


def test_fills_tomorrow_with_rules(env, cfg, tariff, products):
    store, _ = env
    result = generate(env, cfg, tariff, products)
    posts = result["posts"]
    assert result["action"] == "generated" and len(posts) == 4
    assert sorted(p["type"] for p in posts) == ["calc", "compare", "tip", "tip"]     # 링크 글 2개(60% 이하) + tip 2개
    assert all(p["status"] == "approved" for p in posts)
    times = sorted(rules.as_dt(p["scheduled_at"]).astimezone(KST) for p in posts)
    assert [t.strftime("%m-%d %H:%M") for t in times] == ["11-03 08:10", "11-03 12:30", "11-03 18:40", "11-03 21:30"]
    assert all(guard.check_post(p, cfg) == [] for p in posts)
    assert len(store.posts) == 4
    # 저장된 글이 규칙 5 를 어기지 않는다
    for p in posts:
        others = [o for o in posts if o["id"] != p["id"]]
        assert rules.slot_problems(rules.as_dt(p["scheduled_at"]), p["has_link"], others, cfg) == []


def test_never_schedules_today(env, cfg, tariff, products):
    # 20:00 에 돌아도 오늘 21:30 슬롯에는 넣지 않는다
    posts = generate(env, cfg, tariff, products)["posts"]
    assert all(rules.kst_date(rules.as_dt(p["scheduled_at"])) == TOMORROW for p in posts)


def test_existing_link_post_reduces_quota(env, cfg, tariff, products, make_post):
    store, _ = env
    store.insert_post(make_post(scheduled_at=datetime(2026, 11, 3, 8, 10, tzinfo=KST).isoformat()))
    posts = generate(env, cfg, tariff, products)["posts"]
    assert len(posts) == 3
    assert sum(p["has_link"] for p in posts) == 1


def test_full_day_generates_nothing(env, cfg, tariff, products, make_post):
    store, _ = env
    for hm, link in (("08:10", True), ("12:30", True), ("18:40", False), ("21:30", False)):
        h, m = map(int, hm.split(":"))
        store.insert_post(make_post(has_link=link, scheduled_at=datetime(2026, 11, 3, h, m, tzinfo=KST).isoformat()))
    assert generate(env, cfg, tariff, products)["action"] == "no_free_slot"


def test_recent_products_avoided(env, cfg, tariff, products, make_post):
    store, _ = env
    store.insert_post(make_post(status="published", product_ids=["h1"], published_at=(NOW - timedelta(days=1)).isoformat(),
                                created_at=(NOW - timedelta(days=1)).astimezone(timezone.utc).isoformat()))
    calc = next(p for p in generate(env, cfg, tariff, products)["posts"] if p["type"] == "calc")
    assert calc["product_ids"] != ["h1"]


def test_uncertain_everything_held_and_auto_stop(env, cfg, tariff, products):
    store, notifier = env
    posts = generate(env, cfg, tariff, products, review_call=review("uncertain"))["posts"]
    assert all(p["status"] == "held" for p in posts) and len(store.posts) == 4
    assert autostop.is_stopped(store)
    assert any("held 비율 100%" in m for m in notifier.sent)


def test_reviewer_format_errors_three_in_a_row_stop(env, cfg, tariff, products):
    store, notifier = env
    generate(env, cfg, tariff, products, review_call=lambda s, u: ("end_turn", "형식 오류"))
    assert autostop.is_stopped(store)
    assert any("형식 오류 3회 연속" in m for m in notifier.sent)


def test_dry_run_saves_nothing(env, cfg, tariff, products):
    store, _ = env
    result = generate(env, cfg, tariff, products, save=False)
    assert len(result["posts"]) == 4 and store.posts == {} and store.events == []


def test_kill_switch_and_auto_stop_skip_generation(env, cfg, tariff, products):
    store, notifier = env
    called = []
    result = pipeline.run_generate(store, cfg, tariff, products, complete=lambda *a: called.append(1),
                                   review_call=review(), notifier=notifier, now=NOW, env={"AUTO_PUBLISH_DISABLED": "true"})
    assert result["action"] == "kill_switch" and called == []
    autostop.trigger(store, notifier, "테스트")
    assert generate(env, cfg, tariff, products)["action"] == "auto_stopped"


def test_generated_posts_flow_into_publisher_dry_run(env, cfg, tariff, products, caplog):
    # 생성 → 다음 날 08:15 발행기(연습 모드)가 08:10 글을 골라 요청 로그를 남긴다
    store, notifier = env
    generate(env, cfg, tariff, products)
    api = publisher.DryRunApi("123")
    result = publisher.run_once(store, cfg, env={}, api=api, link_checker=publisher.check_link, notifier=notifier,
                                now=datetime(2026, 11, 3, 8, 15, tzinfo=KST).astimezone(timezone.utc),
                                exam_status=(True, "합격"), sleep=lambda _: None)
    assert result["action"] == "dry_run"
    first = min(store.posts.values(), key=lambda p: p["scheduled_at"])
    assert result["post_id"] == first["id"]
    assert api.requests[0]["params"]["text"] == first["text"]


def test_second_day_uses_different_subjects(env, cfg, tariff, products):
    """2026-10-04: 같은 상품·같은 숫자의 글이 다음 날 또 나와 검수 R9 로 막히고 자동 정지됐다."""
    store, _ = env
    import generator
    first = generate(env, cfg, tariff, products)["posts"]
    for p in first:                                     # 첫날 글이 모두 발행됐다고 본다
        store.update_post(p["id"], {"status": "published"})
    second = pipeline.run_generate(store, cfg, tariff, products, complete=smart_llm, review_call=review(),
                                   notifier=env[1], now=NOW + timedelta(days=1), env={})["posts"]
    assert len(second) == 4
    keys1, keys2 = ({generator.subject_key(p) for p in posts} for posts in (first, second))
    assert None not in keys1 | keys2 and not keys1 & keys2


def test_generator_and_reviewer_see_used_posts(env, cfg, tariff, products):
    store, _ = env
    seen = {"gen": [], "rev": []}

    def llm(system, user, temperature):
        seen["gen"].append(user)
        return smart_llm(system, user, temperature)

    def rev(system, user):
        seen["rev"].append(json.loads(user)["recent_posts"])
        return review()(system, user)

    pipeline.run_generate(store, cfg, tariff, products, complete=llm, review_call=rev, notifier=env[1], now=NOW, env={})
    # 같은 실행에서 먼저 승인된 글도 다음 글의 반복 판정에 들어간다 (tip 2개가 서로 비교된다)
    assert seen["rev"][0] == [] and len(seen["rev"][-1]) == 3
    assert "최근에 올린 글" in seen["gen"][-1]             # 둘째 tip 은 첫째 tip 을 보고 쓴다


# ── 생성 모델 혼잡(503) 회복 (2026-10-25) ─────────────────────────────────

class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def flaky(fail_calls):
    """처음 fail_calls 번은 503, 그 뒤로는 정상 응답."""
    n = {"i": 0}

    def complete(system, user, temperature):
        n["i"] += 1
        if n["i"] <= fail_calls:
            raise Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return smart_llm(system, user, temperature)
    return complete


def test_llm_outage_posts_are_regenerated_after_a_wait(env, cfg, tariff, products):
    store, notifier = env
    clock = Clock()
    # 첫 글의 시도 3번이 모두 503 → held(LLM 실패), 나머지는 정상
    result = pipeline.run_generate(store, cfg, tariff, products, complete=flaky(3), review_call=review(),
                                   notifier=notifier, now=NOW, env={}, sleep=clock.sleep, clock=clock)
    posts = result["posts"]
    assert clock.sleeps == [300]                                       # 5분 기다렸다 한 번 더
    assert sum(pipeline.llm_failed(p) for p in posts) == 1
    approved = [p for p in posts if p["status"] == "approved"]
    assert len(approved) == 4                                          # 빈 슬롯이 다시 채워졌다
    assert len(store.posts) == 5                                       # 실패 기록도 남는다


def test_llm_outage_gives_up_within_time_budget(env, cfg, tariff, products):
    store, notifier = env
    clock = Clock()
    result = pipeline.run_generate(store, cfg, tariff, products, complete=flaky(10_000), review_call=review(),
                                   notifier=notifier, now=NOW, env={}, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [300, 600]                                  # 두 번까지만
    assert all(pipeline.llm_failed(p) for p in result["posts"])
    assert not autostop.is_stopped(store)                              # API 오류 held 는 자동 정지 비율에서 빠진다 (규칙 10)


def test_no_retry_when_time_budget_is_used_up(env, cfg, tariff, products):
    store, notifier = env
    clock = Clock()
    clock.t = -1e9                                                     # 시작 시각
    slow = flaky(10_000)

    def complete(*a):
        clock.t += pipeline.GENERATE_TIME_BUDGET                       # 한 번 호출에 예산을 다 씀
        return slow(*a)
    clock.t = 0
    pipeline.run_generate(store, cfg, tariff, products, complete=complete, review_call=review(),
                          notifier=notifier, now=NOW, env={}, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == []


def test_rule_problems_are_not_retried(env, cfg, tariff, products):
    store, notifier = env
    clock = Clock()
    result = pipeline.run_generate(store, cfg, tariff, products, complete=smart_llm, review_call=review("uncertain"),
                                   notifier=notifier, now=NOW, env={}, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [] and not any(pipeline.llm_failed(p) for p in result["posts"])
