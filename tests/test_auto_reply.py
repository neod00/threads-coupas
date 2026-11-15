"""
내 글 답글 자동 응대 (2026-11-15 사용자 결정, sabol.kka 만). 규칙 6 그대로: 내 글에 달린 답글에만 단다.
실제로 다는 것은 config reply_auto + REPLY_AUTO=true + DRY_RUN 꺼짐일 때만. 아니면 연습 기록만.
"""
import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import pytest

import auto_reply
import autostop
import digest
import publisher
import reply_drafts
from settings import PROJECT_ROOT, ConfigError, load_config, parse_config
from store import MemoryStore
from test_reply_drafts import NOW, fake_complete, picked_ids, reply

GOOD = "오 그 정도야? 뭐가 제일 편해"


@pytest.fixture
def auto(cfg):
    return dataclasses.replace(cfg, tone="casual", reply_drafts=True, reply_auto=True)


class FakeApi:
    def __init__(self, fail=False):
        self.containers, self.published, self.fail = [], [], fail

    def create_container(self, params):
        if self.fail:
            raise publisher.ThreadsApiError("HTTP 400 boom")
        self.containers.append(dict(params))
        return f"c{len(self.containers)}"

    def container_status(self, container_id):
        return "FINISHED", None

    def publish(self, creation_id):
        self.published.append(creation_id)
        return f"m-{creation_id}"


class NoNotifier:
    def send(self, text):
        pass


def inbox_store(*replies):
    store = MemoryStore()
    store.set_state("reply_inbox", list(replies))
    return store


def run(store, cfg, api, *, live=True, dry_run=False, now=NOW, complete=None):
    return auto_reply.run(store, cfg, api=api, complete=complete or fake_complete(), now=now,
                          live=live, dry_run=dry_run, sleep=lambda _: None)


# ── 스위치 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [("true", True), ("TRUE", True), (" true ", True),
                                             ("", False), ("false", False), ("ture", False), ("1", False)])
def test_live_only_when_exactly_true(value, expected):
    assert auto_reply.live({"REPLY_AUTO": value}) is expected
    assert auto_reply.live({}) is False


def test_only_sabolkka_has_reply_auto_and_it_needs_reply_drafts():
    assert load_config(PROJECT_ROOT / "accounts" / "sabolkka" / "config.yaml").reply_auto is True
    assert load_config(PROJECT_ROOT / "config.yaml").reply_auto is False
    for name in (p.name for p in (PROJECT_ROOT / "accounts").iterdir() if (p / "config.yaml").is_file()):
        assert load_config(PROJECT_ROOT / "accounts" / name / "config.yaml").reply_auto is (name == "sabolkka"), name


def test_reply_auto_without_reply_drafts_is_a_config_error():
    import yaml
    raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ConfigError, match="reply_drafts"):
        parse_config({**raw, "reply_auto": True, "reply_drafts": False})
    with pytest.raises(ConfigError, match="true/false"):
        parse_config({**raw, "reply_auto": "yes", "reply_drafts": True})


def test_publish_workflow_passes_the_switch_per_account_and_the_llm_key():
    workflow = (Path(PROJECT_ROOT) / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "REPLY_AUTO: ${{ vars[format('REPLY_AUTO{0}', env.SFX)] }}" in workflow
    assert "GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}" in workflow


# ── 고르기 ────────────────────────────────────────────────────────────

def test_due_waits_20_minutes_and_skips_done_ones():
    a, b, c = picked_ids(3)
    drafts = [{**reply(a, hours_ago=0.1), "draft": GOOD}, {**reply(b, user="lee"), "draft": GOOD},
              {**reply(c, user="park"), "draft": GOOD}]
    log = [{"reply_id": b, "username": "lee", "at": (NOW - timedelta(hours=30)).isoformat()}]
    assert [d["reply_id"] for d in auto_reply.due(drafts, log, NOW)] == [c]


def test_due_one_per_user_per_day_and_per_run_and_per_day_caps():
    ids = picked_ids(8)
    same_user = [{**reply(i, user="kim"), "draft": GOOD} for i in ids[:2]]
    assert len(auto_reply.due(same_user, [], NOW)) == 1
    many = [{**reply(i, user=f"u{n}"), "draft": GOOD} for n, i in enumerate(ids)]
    assert len(auto_reply.due(many, [], NOW)) == auto_reply.MAX_PER_RUN == 2
    full_day = [{"reply_id": f"x{n}", "username": f"v{n}", "at": (NOW - timedelta(hours=1)).isoformat()}
                for n in range(auto_reply.MAX_PER_DAY)]
    assert auto_reply.due(many, full_day, NOW) == []
    yesterday = [{**e, "at": (NOW - timedelta(hours=25)).isoformat()} for e in full_day]
    assert len(auto_reply.due(many, yesterday, NOW)) == 2


def test_user_answered_yesterday_can_be_answered_again():
    a = picked_ids(1)[0]
    log = [{"reply_id": "old", "username": "kim", "at": (NOW - timedelta(hours=25)).isoformat()}]
    assert len(auto_reply.due([{**reply(a), "draft": GOOD}], log, NOW)) == 1


# ── 달기 ──────────────────────────────────────────────────────────────

def test_live_run_replies_to_the_commenter_and_never_twice(auto):
    a, b = picked_ids(2)
    store, api = inbox_store(reply(a), reply(b, user="lee")), FakeApi()
    assert run(store, auto, api) == {"posted": 2, "practice": 0, "failed": 0}
    assert [c["reply_to_id"] for c in api.containers] == [a, b]
    assert all(c == {"media_type": "TEXT", "text": GOOD, "reply_to_id": c["reply_to_id"]} for c in api.containers)
    entries = store.get_state(auto_reply.STATE_KEY)
    assert [e["mode"] for e in entries] == ["posted", "posted"] and entries[0]["media_id"] == "m-c1"
    assert run(store, auto, api, now=NOW + timedelta(hours=1)) == {"posted": 0, "practice": 0, "failed": 0}
    assert len(api.containers) == 2


def test_replies_only_go_to_replies_on_my_posts(auto):
    """규칙 6: 답글 대상은 답글 감시가 모은 내 글의 답글(reply_inbox)뿐이다."""
    ids = picked_ids(4)
    store, api = inbox_store(*[reply(i, user=f"u{n}", post_id=f"p{n}") for n, i in enumerate(ids)]), FakeApi()
    for h in range(4):
        run(store, auto, api, now=NOW + timedelta(hours=h))
    assert {c["reply_to_id"] for c in api.containers} <= set(ids) and len(api.containers) == 4


@pytest.mark.parametrize("live, dry_run", [(False, False), (True, True), (False, True)])
def test_without_both_switches_it_only_records_practice(auto, live, dry_run):
    store, api = inbox_store(reply(picked_ids(1)[0])), FakeApi()
    assert run(store, auto, api, live=live, dry_run=dry_run) == {"posted": 0, "practice": 1, "failed": 0}
    assert api.containers == []
    assert store.get_state(auto_reply.STATE_KEY)[0]["mode"] == "practice"


def test_failure_is_recorded_and_not_retried(auto):
    store = inbox_store(reply(picked_ids(1)[0]))
    assert run(store, auto, FakeApi(fail=True)) == {"posted": 0, "practice": 0, "failed": 1}
    api = FakeApi()
    assert run(store, auto, api, now=NOW + timedelta(hours=1))["posted"] == 0 and api.containers == []
    assert "boom" in store.get_state(auto_reply.STATE_KEY)[0]["error"]


def test_bad_drafts_are_never_posted(auto):
    a = picked_ids(1)[0]
    store, api = inbox_store(reply(a)), FakeApi()
    run(store, auto, api, complete=fake_complete({a: "이거 봐 https://link.coupang.com/a/abc"}))
    assert api.containers == []
    with pytest.raises(ValueError, match="링크"):
        auto_reply.reply_params("이거 봐 https://link.coupang.com/a/abc", a, auto)
    with pytest.raises(ValueError, match="답글 ID"):
        auto_reply.reply_params(GOOD, "", auto)


def test_hostile_and_price_replies_get_no_reply(auto):
    ids = picked_ids(2)
    store, api = inbox_store(reply(ids[0], text="이거 광고네"), reply(ids[1], text="이거 얼마예요?", user="lee")), FakeApi()
    assert run(store, auto, api) == {"posted": 0, "practice": 0, "failed": 0} and api.containers == []


def test_reply_auto_off_does_nothing_and_calls_no_llm(cfg):
    store, api, calls = inbox_store(reply(picked_ids(1)[0])), FakeApi(), []
    off = dataclasses.replace(cfg, reply_drafts=True, reply_auto=False)
    assert run(store, off, api, complete=fake_complete(calls=calls))["posted"] == 0
    assert calls == [] and api.containers == []


# ── 발행 작업에서 ─────────────────────────────────────────────────────

def run_once(store, cfg, api, env, complete=None):
    return publisher.run_once(store, cfg, env=env, api=api, link_checker=publisher.check_link, notifier=NoNotifier(),
                              now=NOW, exam_status=(True, "합격"), sleep=lambda _: None,
                              reply_complete=complete or fake_complete())


def test_publish_run_posts_auto_replies_when_switched_on(auto):
    store, api = inbox_store(reply(picked_ids(1)[0])), FakeApi()
    assert run_once(store, auto, api, {"DRY_RUN": "false", "REPLY_AUTO": "true"})["action"] == "nothing_due"
    assert len(api.containers) == 1


def test_publish_run_in_dry_run_only_practices(auto):
    store, api = inbox_store(reply(picked_ids(1)[0])), FakeApi()
    run_once(store, auto, api, {"DRY_RUN": "true", "REPLY_AUTO": "true"})
    assert api.containers == [] and store.get_state(auto_reply.STATE_KEY)[0]["mode"] == "practice"


def test_no_auto_replies_while_auto_stopped_or_kill_switch(auto):
    store, api = inbox_store(reply(picked_ids(1)[0])), FakeApi()
    autostop.trigger(store, NoNotifier(), "테스트 정지")
    assert run_once(store, auto, api, {"DRY_RUN": "false", "REPLY_AUTO": "true"})["action"] == "auto_stopped"
    store2 = inbox_store(reply(picked_ids(1)[0]))
    env = {"DRY_RUN": "false", "REPLY_AUTO": "true", "AUTO_PUBLISH_DISABLED": "true"}
    assert run_once(store2, auto, api, env)["action"] == "kill_switch"
    assert api.containers == [] and store.get_state(auto_reply.STATE_KEY) is None


def test_auto_reply_crash_does_not_stop_publishing(auto, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("뻥")
    monkeypatch.setattr(auto_reply, "run", boom)
    store = inbox_store()
    assert run_once(store, auto, FakeApi(), {"DRY_RUN": "false", "REPLY_AUTO": "true"})["action"] == "nothing_due"


# ── 일일 요약 ──────────────────────────────────────────────────────────

def test_digest_shows_auto_replies_and_drops_them_from_drafts(auto):
    a, b = picked_ids(2)
    store = inbox_store(reply(a), reply(b, user="lee"))
    run(store, auto, FakeApi(), live=False)                      # 연습으로 둘 다 기록
    drafts = reply_drafts.make_drafts(store, auto, fake_complete(), NOW)
    text = digest.build_digest(store, auto, NOW + timedelta(hours=1), dry_run=False, drafts=drafts)
    assert "자동 답글 (지난 24시간) 단 것 0개, 연습 2개 (REPLY_AUTO 꺼짐" in text
    assert "1) [연습] @kim: 나는 건조기 없으면 못 살아" in text and f"→ {GOOD}" in text
    assert "답글 초안" not in text                                # 이미 기록한 답글은 초안 목록에서 뺀다
    assert "자동 답글" not in digest.build_digest(store, auto, NOW + timedelta(hours=30), dry_run=False)


def test_digest_without_reply_auto_has_no_auto_section(cfg):
    store = MemoryStore()
    store.set_state(auto_reply.STATE_KEY, [{"reply_id": "r1", "username": "kim", "draft": GOOD,
                                            "at": NOW.isoformat(), "mode": "posted"}])
    assert "자동 답글" not in digest.build_digest(store, dataclasses.replace(cfg, reply_drafts=True), NOW, dry_run=False)


def test_polite_commenter_gets_a_polite_auto_reply(auto):
    a, b = picked_ids(2)
    store, api = inbox_store(reply(a, text="결국 유선 큰 걸로 갔어요 ㅠ"), reply(b, user="lee")), FakeApi()
    polite_draft = "밤새 못 버티는 게 제일 아쉽죠. 짧게 쓰기엔 딱인데요."
    run(store, auto, api, complete=fake_complete({a: polite_draft}))
    assert {c["reply_to_id"]: c["text"] for c in api.containers} == {a: polite_draft, b: GOOD}


def test_banmal_draft_to_a_polite_commenter_is_not_posted(auto):
    a = picked_ids(1)[0]
    store, api = inbox_store(reply(a, text="결국 유선 큰 걸로 갔어요 ㅠ")), FakeApi()
    run(store, auto, api, complete=fake_complete({a: "밤새 못 버티는 게 제일 아쉽지. 짧게 쓰기엔 딱인데."}))
    assert api.containers == []
    with pytest.raises(ValueError, match="반말"):
        auto_reply.reply_params("밤새 못 버티는 게 제일 아쉽지.", a, auto, polite=True)
