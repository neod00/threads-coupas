"""
답글 초안 (2026-11-14 사용자 결정). 우리 글에 달린 남의 답글 일부에 초안을 만들어 일일 요약에 넣는다.
규칙 6 은 그대로다: 초안만 만들고 스레드에는 아무것도 올리지 않는다.
"""
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import digest
import reply_drafts
from settings import KST, PROJECT_ROOT, load_config
from store import MemoryStore

NOW = datetime(2026, 11, 14, 21, 0, tzinfo=KST)


@pytest.fixture
def casual(cfg):
    return dataclasses.replace(cfg, tone="casual", reply_drafts=True)


def picked_ids(n, prefix="r"):
    """절반 고르기에서 뽑히는 답글 ID n개."""
    out, i = [], 0
    while len(out) < n:
        if reply_drafts._picked(f"{prefix}{i}"):
            out.append(f"{prefix}{i}")
        i += 1
    return out


def reply(rid, text="저는 건조기 없으면 못 살아요", post_id="p1", hours_ago=2, user="kim"):
    return {"post_id": post_id, "reply_id": rid, "username": user, "text": text,
            "permalink": f"https://www.threads.com/@{user}/post/{rid}", "at": (NOW - timedelta(hours=hours_ago)).isoformat()}


def fake_complete(answers=None, calls=None):
    def complete(system, user, temperature):
        ids = [r["id"] for r in json.loads(user)["replies"]]
        if calls is not None:
            calls.append({"system": system, "ids": ids})
        return json.dumps({"drafts": [{"id": i, "text": (answers or {}).get(i, "오 그 정도야? 뭐가 제일 편해")} for i in ids]},
                          ensure_ascii=False)
    return complete


# ── 고르기 ────────────────────────────────────────────────────────────

def test_about_half_of_replies_are_picked_and_the_choice_is_stable():
    ids = [f"r{i}" for i in range(400)]
    chosen = [i for i in ids if reply_drafts._picked(i)]
    assert 150 < len(chosen) < 250
    assert chosen == [i for i in ids if reply_drafts._picked(i)]


@pytest.mark.parametrize("text, reason", [
    ("ㅋㅋ", "내용 없음"),
    ("이거 광고네", "비난·항의"),
    ("사기 아니야?", "비난·항의"),
    ("광고충 꺼져", "비난·항의"),
    ("이거 얼마예요?", "가격·건강·안전 질문"),
    ("아토피 있는 애한테 써도 돼요?", "가격·건강·안전 질문"),
    ("전자파 괜찮나요", "가격·건강·안전 질문"),
])
def test_hostile_and_price_health_safety_replies_are_skipped(text, reason):
    assert reply_drafts.skip_reason(text) == reason


@pytest.mark.parametrize("text", ["저는 건조기 없으면 못 살아요", "사기 전에 고민 많이 했는데 잘 쓰는 중", "2번이요",
                                  "비셀 얼마 안걸려 스팀 쓰고 바닥 뽀송한거 느끼면 딴거 못 써"])
def test_ordinary_replies_are_kept(text):
    assert reply_drafts.skip_reason(text) is None


def test_select_limits_per_post_per_day_and_window():
    ids = picked_ids(12)
    inbox = ([reply(i, post_id="p1") for i in ids[:5]]                       # 글 하나에 3개까지
             + [reply(i, post_id=f"q{n}") for n, i in enumerate(ids[5:10])]  # 하루 6개까지
             + [reply(ids[10], post_id="old", hours_ago=30)]                 # 24시간 지난 답글
             + [reply(ids[11], post_id="bad", text="이거 광고네")])
    chosen = reply_drafts.select(inbox, NOW)
    assert len(chosen) == reply_drafts.MAX_PER_DAY == 6
    assert sum(r["post_id"] == "p1" for r in chosen) == reply_drafts.MAX_PER_POST == 3
    assert not {"old", "bad"} & {r["post_id"] for r in chosen}


def test_unpicked_replies_get_no_draft():
    unpicked = next(f"r{i}" for i in range(100) if not reply_drafts._picked(f"r{i}"))
    assert reply_drafts.select([reply(unpicked)], NOW) == []


# ── 초안 검사 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, problem", [
    ("", "빈 초안"),
    ("이거 봐 https://link.coupang.com/a/abc", "링크"),
    ("@kim 그치", "멘션·해시태그"),
    ("맞아 진짜 편하지 😊", "이모지"),
    ("나도 써봤는데 좋더라", "금지어"),
    ("가격 생각하면 고민되지", "금지어"),
    ("그렇군요 저도 궁금해요", "존댓말"),
    ("가" * 81, "초과"),
    ("한 줄\n두 줄\n세 줄", "초과"),
])
def test_draft_problems(casual, text, problem):
    assert any(problem in p for p in reply_drafts.draft_problems(text, casual))


def test_a_plain_casual_draft_passes(casual):
    assert reply_drafts.draft_problems("오 그 정도야? 뭐가 제일 편해", casual) == []


# ── 만들기 ────────────────────────────────────────────────────────────

def test_make_drafts_drops_bad_ones_and_never_calls_twice(casual, make_post):
    store = MemoryStore()
    post = make_post(status="published", text="건조기, 살까 말까\n\n빨래 널 자리 없는 사람?")
    store.insert_post(post)
    good, bad = picked_ids(2)
    store.set_state("reply_inbox", [reply(good, post_id=post["id"]), reply(bad, post_id=post["id"], user="lee")])
    calls = []
    complete = fake_complete({bad: "나도 써봤는데 좋더라"}, calls)
    drafts = reply_drafts.make_drafts(store, casual, complete, NOW)
    assert [d["reply_id"] for d in drafts] == [good] and drafts[0]["draft"] == "오 그 정도야? 뭐가 제일 편해"
    assert "반말" in calls[0]["system"] and set(calls[0]["ids"]) == {good, bad}
    # 다시 돌려도 LLM 을 또 부르지 않는다 (버린 초안도 기억한다)
    assert reply_drafts.make_drafts(store, casual, complete, NOW) == drafts and len(calls) == 1


def test_llm_failure_gives_no_drafts_and_is_retried_next_time(casual):
    store = MemoryStore()
    rid = picked_ids(1)[0]
    store.set_state("reply_inbox", [reply(rid)])

    def boom(system, user, temperature):
        raise RuntimeError("503")

    assert reply_drafts.make_drafts(store, casual, boom, NOW) == []
    assert len(reply_drafts.make_drafts(store, casual, fake_complete(), NOW)) == 1


def test_no_candidates_means_no_llm_call(casual):
    store = MemoryStore()
    store.set_state("reply_inbox", [reply(picked_ids(1)[0], text="이거 광고네")])
    calls = []
    assert reply_drafts.make_drafts(store, casual, fake_complete(calls=calls), NOW) == [] and calls == []


def test_module_cannot_post_to_threads():
    """규칙 6: 초안만 만든다. 발행 코드(publisher, 스레드 API)를 쓰지 않는다."""
    source = (PROJECT_ROOT / "reply_drafts.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("import publisher", "from publisher", "threads_publish", "reply_to_id", "requests", "ThreadsApi"):
        assert word not in code, word


# ── 일일 요약 ──────────────────────────────────────────────────────────

def test_digest_shows_drafts_only_when_given(casual):
    store = MemoryStore()
    drafts = [{**reply("r1"), "draft": "오 그 정도야? 뭐가 제일 편해"}]
    text = digest.build_digest(store, casual, NOW, dry_run=True, drafts=drafts)
    assert "답글 초안 1개 — 자동으로 달지 않아요" in text
    assert "1) @kim: 저는 건조기 없으면 못 살아요" in text and "→ 오 그 정도야? 뭐가 제일 편해" in text
    assert "https://www.threads.com/@kim/post/r1" in text
    assert "답글 초안" not in digest.build_digest(store, casual, NOW, dry_run=True)


def test_only_sabolkka_has_reply_drafts_on_and_digest_workflow_has_the_llm_key():
    assert load_config(PROJECT_ROOT / "accounts" / "sabolkka" / "config.yaml").reply_drafts is True
    assert load_config(PROJECT_ROOT / "config.yaml").reply_drafts is False
    workflow = (Path(PROJECT_ROOT) / ".github" / "workflows" / "digest.yml").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}" in workflow
