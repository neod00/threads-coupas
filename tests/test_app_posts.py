"""
앱 홍보 계정(content: app, PLAN 1-2) 테스트. LLM·검수자는 가짜, 앱 정보·가드·슬롯·저장소는 실제 코드로 돈다.

  - apps.yaml info 형식 검사 (필수 항목, 링크는 내 앱 주소만, id 겹침)
  - 하루 배분: 링크 글 1개(앱 소개가 먼저, 그다음 기능) + 링크 없는 글(고친 것이 있으면 하루 하나, 나머지 질문)
  - 앱 소개(launch)는 앱마다 한 번, 기능 글은 소개 뒤에만
  - 고친 것(devlog)은 info.updates 에 적힌 것만, 한 번씩만
  - 이미지·영상: 소개 = launch_media, 기능 = 그 기능 캡처, 기능 글 5개 중 1개는 시연 영상
  - 링크는 apps.yaml 에 등록한 내 앱 주소만 통과
  - 생성: 답글 첫 줄은 config 의 disclosure_text("내가 만든 앱이야."), product_ids 에 앱 id, media 저장
"""
import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

import app_posts
import exam
import generator
import guard
import pipeline
import rules
import settings
from notify import CollectingNotifier
from settings import KST
from store import MemoryStore
from test_pipeline import review

NOW = datetime(2026, 11, 2, 20, 0, tzinfo=KST)
TOMORROW = NOW.date() + timedelta(days=1)
CASES = json.loads(exam.APP_CASES_PATH.read_text(encoding="utf-8"))
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
MP4 = b"\0\0\0\x20ftypisom" + b"\0" * 32


@pytest.fixture
def app_cfg():
    # 실제 openbrain 설정 (content: app). 운영은 일부 유형만 켜 둘 수 있다 (2026-10-31 ask 끔). 테스트는 모든 유형을 켜고 규칙을 본다.
    return dataclasses.replace(settings.load_config(), post_types=settings.APP_POST_TYPES)


@pytest.fixture
def raw_app():
    return copy.deepcopy(CASES["apps"][0])


@pytest.fixture
def apps(raw_app):
    return (app_posts.parse_app(raw_app),)


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    """시험지 앱(demo-pdf)의 캡처·영상 목록. edit-text 캡처·영상, search 캡처."""
    folder = tmp_path / "media" / "demo-pdf"
    folder.mkdir(parents=True)
    entries = {}
    for item_id, kind in (("edit-text", "png"), ("search", "png"), ("edit-text-demo", "mp4")):
        (folder / f"{item_id}.{kind}").write_bytes(MP4 if kind == "mp4" else PNG)
        entries[item_id] = {"file": f"demo-pdf/{item_id}.{kind}", "kind": "video" if kind == "mp4" else "image",
                            "feature": item_id}
    (folder / "media.json").write_text(json.dumps(entries), encoding="utf-8")
    import media
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path / "media")
    return tmp_path / "media"


def with_media(raw_app, **kw):
    raw = copy.deepcopy(raw_app)
    raw["info"]["launch_media"] = ["edit-text-demo", "edit-text", "search"]
    for f in raw["info"]["features"]:
        if f["id"] == "edit-text":
            f.update(media="edit-text", clip="edit-text-demo")
        if f["id"] == "redact":
            f.update(media="search")
    raw["info"].update(kw)
    return (app_posts.parse_app(raw),)


def used_post(post_type, key, media=(), app="demo-pdf"):
    return {"type": post_type, "product_ids": [app], "calc_payload": {"subject": key}, "media": list(media),
            "text": key, "status": "published"}


# ── apps.yaml ─────────────────────────────────────────────────────────────

def test_real_apps_yaml_has_pdfmonster_info():
    apps = app_posts.load_apps()
    pdf = next(a for a in apps if a.id == "pdfmonster")
    assert pdf.link == "https://pdfmonster.xyz/" and "무료" in pdf.facts and len(pdf.features) >= 5
    index = app_posts.media_index("pdfmonster")
    assert all(i in index for i in pdf.launch_media)           # 소개 글 이미지·영상이 실제로 있다
    assert all(f.media in index for f in pdf.features if f.media) and all(f.clip in index for f in pdf.features if f.clip)


@pytest.mark.parametrize("change, words", [
    ({"link": "https://other.example/"}, "앱 주소"),
    ({"link": "http://pdfmonster.xyz/"}, "앱 주소"),
    ({"facts": []}, "facts"),
    ({"features": []}, "기능"),
    ({"price": "무료"}, "모르는 키"),
    ({"updates": [{"id": "a", "date": "어제", "note": "고침"}]}, "date"),
    ({"updates": [{"id": "a", "date": "2026-10-01", "note": "x"}, {"id": "a", "date": "2026-10-02", "note": "y"}]}, "겹치지"),
])
def test_bad_app_info(raw_app, change, words):
    raw_app["info"].update(change)
    with pytest.raises(app_posts.AppInfoError, match=words):
        app_posts.parse_app(raw_app)


def test_app_without_info_is_skipped():
    assert app_posts.parse_app({"id": "x", "name": "X", "url": "https://x.example/", "shots": []}) is None


# ── 링크 ──────────────────────────────────────────────────────────────────

def test_link_checker_allows_only_my_app(apps):
    check = app_posts.make_link_checker(apps)
    assert check("https://pdfmonster.xyz/merge-pdf/") is None
    assert "내 앱 주소" in check("https://link.coupang.com/a/abc")
    assert "https" in check("http://pdfmonster.xyz/")


# ── 주제·하루 배분 ───────────────────────────────────────────────────────────

def test_plan_day_launch_first_then_feature(app_cfg, apps):
    assert app_posts.plan_day([], app_cfg, TOMORROW, apps, []) == ["launch", "devlog", "ask"]
    launched = [used_post("launch", "launch|demo-pdf")]
    assert app_posts.plan_day([], app_cfg, TOMORROW, apps, launched) == ["feature", "devlog", "ask"]


def test_real_config_introduces_app_directly(apps):
    # 2026-10-31: 질문으로 빌드업하는 글(ask)은 쓰지 않는다. 고친 것이 없으면 링크 글 하나만.
    cfg = settings.load_config()
    assert "ask" not in cfg.post_types
    assert app_posts.plan_day([], cfg, TOMORROW, apps, []) == ["launch", "devlog"]
    raw_none = dataclasses.replace(apps[0], updates=())
    launched = [used_post("launch", "launch|demo-pdf")]
    assert app_posts.plan_day([], cfg, TOMORROW, (raw_none,), launched) == ["feature"]


def test_plan_day_without_updates_uses_only_questions(app_cfg, raw_app):
    raw_app["info"]["updates"] = []
    apps = (app_posts.parse_app(raw_app),)
    assert app_posts.plan_day([], app_cfg, TOMORROW, apps, [used_post("launch", "launch|demo-pdf")]) == \
        ["feature", "ask", "ask"]


def test_plan_day_respects_occupied_slots(app_cfg, apps):
    slot = datetime.combine(TOMORROW, datetime.min.time(), tzinfo=KST).replace(hour=8, minute=10)
    occupied = [{"scheduled_at": slot.astimezone(timezone.utc).isoformat(), "has_link": True, "status": "approved"}]
    assert app_posts.plan_day(occupied, app_cfg, TOMORROW, apps, []) == ["devlog", "ask"]   # 링크 자리는 이미 찼다


def test_launch_once_per_app_and_features_after_launch(apps):
    assert [s["key"] for s in app_posts.subjects(apps, "launch", [])] == ["launch|demo-pdf"]
    assert app_posts.subjects(apps, "feature", []) == []
    used = [used_post("launch", "launch|demo-pdf")]
    assert app_posts.subjects(apps, "launch", used) == []
    assert len(app_posts.subjects(apps, "feature", used)) == 5


def test_devlog_uses_each_update_once(apps):
    first = app_posts.pick_subject(apps, "devlog", TOMORROW, [])
    second = app_posts.pick_subject(apps, "devlog", TOMORROW, [used_post("devlog", first["key"])])
    assert {first["update"].id, second["update"].id} == {"page-number", "hwp-font"}
    done = [used_post("devlog", "devlog|demo-pdf|page-number"), used_post("devlog", "devlog|demo-pdf|hwp-font")]
    assert app_posts.pick_subject(apps, "devlog", TOMORROW, done) is None


def test_feature_rotates_least_recently_used(apps):
    used = [used_post("launch", "launch|demo-pdf")] + [
        used_post("feature", f"feature|demo-pdf|{f}") for f in ("edit-text", "redact", "sign", "merge-split")]
    assert app_posts.pick_subject(apps, "feature", TOMORROW, used)["feature"].id == "compress"


def test_payload_holds_only_app_facts(apps):
    subject = app_posts.pick_subject(apps, "feature", TOMORROW, [used_post("launch", "launch|demo-pdf")])
    payload = app_posts.build_payload(subject, "feature")
    assert payload["subject"] == subject["key"] and payload["feature"]["name"] == subject["feature"].name
    assert "최대 60MB PDF 까지 열 수 있다" in payload["app"]["facts"]
    ask = app_posts.build_payload(app_posts.pick_subject(apps, "ask", TOMORROW, []), "ask")
    assert ask["choice_numbers"] == [1, 2, 3] and ask["question_topic"] in apps[0].questions


# ── 이미지·영상 ─────────────────────────────────────────────────────────────

def test_launch_gets_demo_video_and_screens(app_cfg, raw_app, media_dir):
    apps = with_media(raw_app)
    subject = app_posts.pick_subject(apps, "launch", TOMORROW, [])
    assert app_posts.pick_media(subject, "launch", [], app_cfg) == [
        "demo-pdf/edit-text-demo.mp4", "demo-pdf/edit-text.png", "demo-pdf/search.png"]


def test_feature_video_every_fifth(app_cfg, raw_app, media_dir):
    apps = with_media(raw_app)
    edit = next(s for s in app_posts.subjects(apps, "feature", [used_post("launch", "launch|demo-pdf")])
                if s["feature"].id == "edit-text")
    used = [used_post("launch", "launch|demo-pdf")]
    assert app_posts.pick_media(edit, "feature", used, app_cfg) == ["demo-pdf/edit-text.png"]
    used += [used_post("feature", f"feature|demo-pdf|f{i}") for i in range(4)]     # 영상 없이 기능 글 4개
    assert app_posts.pick_media(edit, "feature", used, app_cfg) == ["demo-pdf/edit-text-demo.mp4"]
    used.append(used_post("feature", "feature|demo-pdf|v", media=["demo-pdf/edit-text-demo.mp4"]))
    assert app_posts.pick_media(edit, "feature", used, app_cfg) == ["demo-pdf/edit-text.png"]


def test_feature_without_screen_has_no_media_and_media_off_account(app_cfg, raw_app, media_dir):
    apps = with_media(raw_app)
    sign = next(s for s in app_posts.subjects(apps, "feature", [used_post("launch", "launch|demo-pdf")])
                if s["feature"].id == "sign")
    assert app_posts.pick_media(sign, "feature", [], app_cfg) == []
    launch = app_posts.pick_subject(apps, "launch", TOMORROW, [])
    assert app_posts.pick_media(launch, "launch", [], dataclasses.replace(app_cfg, allow_media=False)) == []


# ── 생성 ──────────────────────────────────────────────────────────────────

def app_llm(system, user, temperature):
    """앱 데이터(JSON)를 읽어 가드를 통과하는 초안을 만든다 (사실은 데이터에서만)."""
    payload = json.loads(user.split("앱 데이터(JSON):\n", 1)[1].split("\n\n이전 초안", 1)[0].split("\n\n최근에 올린 글", 1)[0])
    name, app = payload["app"]["name"], payload["app"]
    if "feature" in payload:            # 유형마다 다른 글 (같은 글이면 코드 R9 반복 판정에 걸린다)
        topic = payload["feature"]["name"]
        body = f"{topic} 기능이에요.\n{payload['feature']['desc']}.\n\n제가 만든 {name} 에 있어요.\n\n자주 쓰세요?"
    elif "update" in payload:
        topic = "이번에 고친 것"
        body = f"{payload['update']['note']}.\n\n다음엔 뭐가 있으면 좋을까요?"
    elif "question_topic" in payload:
        topic = payload["question_topic"][:14]
        body = "만들고 있는 앱에 참고하려고요.\n\n1번 설치형\n2번 온라인\n3번 출력\n\n답글로 번호만 남겨 주세요."
    else:
        topic = "PDF 고치기"
        body = f"{app['problem']}.\n\n그래서 {name} 를 만들었어요.\n{app['how']}.\n\n써 보시고 알려 주세요."
    hooks = [f"{topic}, 어떻게 하세요?", f"{topic} 이야기", f"{topic} 비교"]
    return json.dumps({"hook_candidates": [{"style": s, "text": t} for s, t in zip(("question", "result", "compare"), hooks)],
                       "body": body, "reply_line": "여기서 바로 써 볼 수 있어요.", "topic_tag": "앱개발일기"},
                      ensure_ascii=False)


def run(app_cfg, apps, store=None, **kw):
    store = store or MemoryStore()
    result = pipeline.run_generate(store, app_cfg, None, [], complete=app_llm, review_call=review(),
                                   notifier=CollectingNotifier(), now=NOW, env={}, apps=apps, **kw)
    return result, store


def test_generates_launch_devlog_ask_with_disclosure_reply(app_cfg, raw_app, media_dir):
    apps = with_media(raw_app)
    result, store = run(app_cfg, apps)
    posts = {p["type"]: p for p in result["posts"]}
    assert set(posts) == {"launch", "devlog", "ask"} and all(p["status"] == "approved" for p in posts.values())
    launch = posts["launch"]
    assert launch["reply_text"].splitlines()[0] == app_cfg.disclosure_text == "내가 만든 앱이야."
    assert launch["link_url"] == "https://pdfmonster.xyz/"
    assert launch["variant"] == "link_in_reply" and launch["product_ids"] == ["demo-pdf"]
    assert launch["media"] == ["demo-pdf/edit-text-demo.mp4", "demo-pdf/edit-text.png", "demo-pdf/search.png"]
    assert posts["ask"]["has_link"] is False and posts["ask"]["reply_text"] is None and posts["ask"]["media"] == []
    assert all(guard.check_post(p, app_cfg) == [] for p in posts.values())
    times = sorted(rules.as_dt(p["scheduled_at"]).astimezone(KST).strftime("%H:%M") for p in posts.values())
    assert times == ["08:10", "12:30", "20:30"]
    assert app_posts.make_link_checker(apps)(launch["link_url"]) is None


def test_next_day_makes_feature_not_another_launch(app_cfg, raw_app, media_dir):
    apps = with_media(raw_app)
    _, store = run(app_cfg, apps)
    for p in store.list_posts(["approved"]):        # 하루가 지나 모두 발행됐다고 친다
        store.update_post(p["id"], {"status": "published", "published_at": p["scheduled_at"]})
    later = NOW + timedelta(days=1)
    result = pipeline.run_generate(store, app_cfg, None, [], complete=app_llm, review_call=review(),
                                   notifier=CollectingNotifier(), now=later, env={}, apps=apps)
    types = [p["type"] for p in result["posts"]]
    assert types[0] == "feature" and "launch" not in types


def test_coupang_types_and_app_types_do_not_mix(app_cfg, cfg):
    with pytest.raises(ValueError, match="app_subject"):
        generator.generate_post("feature", [], None, app_cfg, app_llm)
    assert app_cfg.content == "app" and cfg.content == "coupang"


def test_app_reviewer_prompt_is_used_for_app_account(app_cfg, cfg):
    import reviewer
    assert "1인 개발자의 앱 홍보 글" in reviewer.system_prompt(app_cfg)
    assert "쿠팡 파트너스 심사자" in reviewer.system_prompt(cfg)
    assert exam.cases_path_for(app_cfg) == exam.APP_CASES_PATH and exam.cases_path_for(cfg) == exam.CASES_PATH


# ── 시험지 ─────────────────────────────────────────────────────────────────

def test_app_exam_case_counts_and_items():
    block = [c for c in CASES["cases"] if c["expect"] == "block"]
    normal = [c for c in CASES["cases"] if c["expect"] == "pass"]
    assert len(block) == 20 and len(normal) == 10
    covered = {v for c in block for v in c["violations"]}
    assert {"R2", "R3", "R4", "R5", "R6", "R7", "R10", "R11", "R12"} <= covered
    assert {c["type"] for c in normal} == {"launch", "feature", "devlog", "ask"}


def test_app_exam_normal_cases_pass_guard_and_most_problems_need_the_reviewer(app_cfg):
    caught = []
    for case in CASES["cases"]:
        post, rows, _ = exam.build_case(case, CASES, app_cfg)
        problems = guard.check_post(post, app_cfg)
        if case["expect"] == "pass":
            assert problems == [], (case["id"], problems)
        elif problems:
            caught.append(case["id"])
    assert len(caught) <= 5, caught            # 문제 글 대부분은 가드를 빠져나가 검수자 실력을 본다


def test_app_prompts_are_casual_and_carry_real_stories(app_cfg, apps):
    # 2026-10-30 사용자 결정: 반말. 인기 글 분석(첫 줄 고백·질문·반전, 짧은 줄)이 생성 프롬프트에 들어 있다
    system = generator.system_prompt(app_cfg, "launch", "link_in_reply")
    assert app_cfg.tone == "casual" and "반말" in system and "첫 줄이 글의 전부" in system
    assert "여기서 바로 써 볼 수 있어" in system                        # 링크 답글 안내도 반말
    subject = app_posts.pick_subject(apps, "launch", TOMORROW, [])
    assert app_posts.build_payload(subject, "launch")["app"]["stories"] == list(apps[0].stories) != []


def test_app_reviewer_tone_rule_is_its_own(app_cfg, cfg):
    # 앱 검수 R11 은 쿠팡 계정과 따로다 (쿠팡 계정의 tone_rule 을 바꾸면 sabol.kka 시험이 무효가 된다)
    import reviewer
    app_prompt = reviewer.system_prompt(app_cfg)
    assert "반말·음슴체·해요체 모두 허용" in app_prompt and "{tone_rule}" not in reviewer.APP_PROMPT_PATH.read_text(encoding="utf-8")
