"""
내 앱 화면 자동 캡처·시연 영상 테스트 (capture.py). 브라우저·ffmpeg 는 가짜로 바꾼다 (실제 접속 없음).

  - apps.yaml 형식 검사 (https 주소, 겹치지 않는 id, 단계 종류·값, 영상 문구·끝 화면)
  - 앱 밖 주소로 이동하려 하면 그 화면은 실패, 저장하지 않음 (규칙 2)
  - 예시 파일은 data/app_samples/<앱>/ 안의 파일만
  - 그림이 그대로면 파일·captured_at 을 바꾸지 않음 (매주 캡처해도 바뀐 화면만 커밋)
  - 영상은 파일이 없거나 같은 앱 화면이 바뀌었거나 --clips 일 때만 다시 만든다
  - 영상 녹화: 준비 단계는 빼고, 자막 → 단계(사람 속도 입력) → 끝 화면 순서, 프레임 길이는 시각으로 맞춤
  - 자막·끝 화면 문구 금지어 검사
  - 찍은 그림·영상은 가드 G9 를 통과
"""
import dataclasses
import json
from datetime import datetime, timezone

import pytest

import capture
import media
from capture import CaptureError

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
NOW = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)


def app_raw(**shot):
    shot = {"id": "home", "feature": "첫 화면", "steps": [{"goto": "/"}], **shot}
    return {"apps": [{"id": "demo", "name": "Demo", "url": "https://demo.example/", "shots": [shot]}]}


# ── apps.yaml ─────────────────────────────────────────────────────────────

def test_real_apps_yaml_parses():
    apps = capture.load_apps(capture.APPS_PATH)
    pdf = next(a for a in apps if a.id == "pdfmonster")
    assert pdf.hosts == {"pdfmonster.xyz"} and {s.id for s in pdf.shots} >= {"home", "editor", "edit-text", "search"}
    for shot in pdf.shots:
        for step in shot.steps:
            if "upload" in step:
                assert (capture.SAMPLES_DIR / "pdfmonster" / step["upload"]["file"]).is_file()


@pytest.mark.parametrize("raw, words", [
    ({"apps": [{"id": "demo", "name": "D", "url": "http://demo.example/", "shots": []}]}, "https"),
    ({"apps": [{"id": "Demo!", "name": "D", "url": "https://demo.example/", "shots": []}]}, "id"),
    (app_raw(steps=[{"goto": "https://other.example/"}]), "앱 안 경로"),
    (app_raw(steps=[{"eval": "alert(1)"}]), "단계는"),
    (app_raw(steps=[{"wait": 99_999}]), "wait"),
    (app_raw(steps=[{"upload": {"selector": "#f"}}]), "upload"),
    (app_raw(steps=[{"press": {"key": "Backspace", "times": 0}}]), "press"),
    (app_raw(feature=" "), "feature"),
    (app_raw(steps=[]), "steps"),
    ({"apps": [], "extra": 1}, "apps"),
])
def test_bad_apps_yaml(raw, words):
    with pytest.raises(CaptureError, match=words):
        capture.parse_apps(raw)


def test_duplicate_app_ids_rejected():
    raw = app_raw()
    raw["apps"].append(dict(raw["apps"][0]))
    with pytest.raises(CaptureError, match="겹치지"):
        capture.parse_apps(raw)


# ── 앱 밖으로 이동 금지 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("url, ok", [
    ("https://demo.example/edit?x=1", True),
    ("about:blank", True),
    ("https://other.example/", False),
    ("http://demo.example/", False),
    ("https://demo.example.evil.com/", False),
])
def test_host_allowed(url, ok):
    assert capture.host_allowed(url, {"demo.example"}) is ok


# ── 가짜 브라우저 ─────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, url, frame, navigation=True):
        self.url, self.frame, self._nav = url, frame, navigation

    def is_navigation_request(self):
        return self._nav


class FakeRoute:
    def __init__(self):
        self.result = None

    def abort(self):
        self.result = "abort"

    def continue_(self):
        self.result = "continue"


class FakeKeyboard:
    def __init__(self, log):
        self.log = log

    def type(self, text, delay=0):
        self.log.append(("type", text) if not delay else ("type", text, delay))

    def press(self, key, delay=0):
        self.log.append(("press", key) if not delay else ("press", key, delay))


class FakePage:
    """goto 하면 등록된 route 처리기를 거친다. redirect 로 다른 주소로 넘어가는 사이트를 흉내 낸다."""

    def __init__(self, redirect=None):
        self.log, self.main_frame, self.handler, self.redirect = [], object(), None, redirect
        self.keyboard = FakeKeyboard(self.log)

    def route(self, pattern, handler):
        self.handler = handler

    def goto(self, url, wait_until=None):
        for target in [url] + ([self.redirect] if self.redirect else []):
            route = FakeRoute()
            self.handler(route, FakeRequest(target, self.main_frame))
            self.log.append(("goto", target, route.result))

    def click(self, selector, timeout=None):
        self.log.append(("click", selector))

    def set_input_files(self, selector, path, timeout=None):
        self.log.append(("upload", selector, path))

    def fill(self, selector, text, timeout=None):
        self.log.append(("fill", selector, text))

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.log.append(("wait_for", selector))

    def screenshot(self, type=None):
        return PNG

    def evaluate(self, script, arg=None):
        names = {capture.CAPTION_JS: "caption", capture.ZOOM_JS: "zoom", capture.UNZOOM_JS: "unzoom",
                 capture.END_JS: "end", capture.TITLE_JS: "title", capture.TITLE_OUT_JS: "title_out",
                 capture.TAP_JS: "tap"}
        self.log.append((names.get(script, "js"), arg))


class FakeCdp:
    """화면 전송을 흉내 낸다: 시작하면 프레임 하나를 보낸다."""

    def __init__(self, log):
        self.log, self.handler = log, None

    def on(self, event, handler):
        self.handler = handler

    def send(self, method, params=None):
        self.log.append(("cdp", method))
        if method == "Page.startScreencast":
            import base64
            import time
            self.handler({"metadata": {"timestamp": time.time()}, "data": base64.b64encode(b"jpeg").decode(),
                          "sessionId": 1})


class FakeBrowser:
    def __init__(self, redirect=None):
        self.pages, self.redirect = [], redirect

    def new_context(self, **kw):
        browser = self

        class Context:
            def new_page(self):
                page = FakePage(browser.redirect)
                browser.pages.append(page)
                return page

            def new_cdp_session(self, page):
                return FakeCdp(page.log)

            def close(self):
                pass

        return Context()


@pytest.fixture
def dirs(tmp_path):
    samples = tmp_path / "samples"
    (samples / "demo").mkdir(parents=True)
    (samples / "demo" / "sample.pdf").write_bytes(b"%PDF-1.4")
    return tmp_path / "media", samples


MP4 = b"\0\0\0\x20ftypisom" + b"\0" * 32


@pytest.fixture
def fake_encode(monkeypatch):
    """ffmpeg 대신 MP4 머리만 쓴다. 받은 조각(프레임, 초)을 기록한다."""
    calls = []

    def encode(pieces, out, ffmpeg):
        calls.append(pieces)
        out.write_bytes(MP4)

    monkeypatch.setattr(capture, "encode_mp4", encode)
    return calls


def clip_raw(**clip):
    raw = app_raw()
    raw["apps"][0]["clips"] = [{"id": "demo-clip", "feature": "시연", "caption": "PDF 글자, 그 자리에서 고쳐요",
                                "end": {"title": "Demo", "sub": "무료 · 가입 없음"},
                                "setup": [{"goto": "/"}, {"upload": {"selector": "#f", "file": "sample.pdf"}}],
                                "steps": [{"zoom": {"selector": "#line", "scale": 1.8}}, {"click": "#line"},
                                          {"press": {"key": "Backspace", "times": 2}}, {"type": "25"}],
                                **clip}]
    return raw


def test_runs_steps_and_saves_png(dirs):
    images, samples = dirs
    apps = capture.parse_apps(app_raw(steps=[
        {"goto": "/editor"}, {"upload": {"selector": "#f", "file": "sample.pdf"}}, {"click": "#b"},
        {"press": {"key": "Backspace", "times": 3}}, {"type": "2,500,000원"}, {"wait": 500}]))
    browser = FakeBrowser()
    result = capture.capture(apps, browser=browser, media_dir=images, samples_dir=samples, now=NOW)
    assert result == {"saved": ["demo/home"], "unchanged": [], "failed": {}}
    log = browser.pages[0].log
    assert log[0] == ("goto", "https://demo.example/editor", "continue")
    assert log[1] == ("upload", "#f", str((samples / "demo" / "sample.pdf").resolve()))
    assert log.count(("press", "Backspace")) == 3 and ("type", "2,500,000원") in log
    assert (images / "demo" / "home.png").read_bytes() == PNG
    manifest = json.loads((images / "demo" / "media.json").read_text(encoding="utf-8"))
    assert manifest == {"home": {"file": "demo/home.png", "kind": "image", "feature": "첫 화면",
                                 "captured_at": "2026-11-02T21:00:00+00:00"}}


def test_navigation_outside_app_fails_and_saves_nothing(dirs):
    images, samples = dirs
    apps = capture.parse_apps(app_raw())
    browser = FakeBrowser(redirect="https://ads.example/landing")
    result = capture.capture(apps, browser=browser, media_dir=images, samples_dir=samples, now=NOW)
    assert "앱 밖 주소" in result["failed"]["demo/home"] and result["saved"] == []
    assert browser.pages[0].log[-1] == ("goto", "https://ads.example/landing", "abort")
    assert not (images / "demo" / "home.png").exists()


def test_upload_outside_samples_folder_fails(dirs):
    images, samples = dirs
    apps = capture.parse_apps(app_raw(steps=[{"goto": "/"}, {"upload": {"selector": "#f", "file": "../../secret.pdf"}}]))
    result = capture.capture(apps, browser=FakeBrowser(), media_dir=images, samples_dir=samples, now=NOW)
    assert "app_samples" in result["failed"]["demo/home"]


def test_one_failed_shot_does_not_stop_others(dirs):
    images, samples = dirs
    raw = app_raw(steps=[{"goto": "/"}, {"upload": {"selector": "#f", "file": "missing.pdf"}}])
    raw["apps"][0]["shots"].append({"id": "ok", "feature": "되는 화면", "steps": [{"goto": "/"}]})
    result = capture.capture(capture.parse_apps(raw), browser=FakeBrowser(), media_dir=images, samples_dir=samples,
                             now=NOW)
    assert list(result["failed"]) == ["demo/home"] and result["saved"] == ["demo/ok"]


def test_unchanged_image_keeps_file_and_time(dirs):
    images, samples = dirs
    apps = capture.parse_apps(app_raw())
    capture.capture(apps, browser=FakeBrowser(), media_dir=images, samples_dir=samples, now=NOW)
    later = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)
    result = capture.capture(apps, browser=FakeBrowser(), media_dir=images, samples_dir=samples, now=later)
    assert result["unchanged"] == ["demo/home"]
    manifest = json.loads((images / "demo" / "media.json").read_text(encoding="utf-8"))
    assert manifest["home"]["captured_at"] == "2026-11-02T21:00:00+00:00"


def test_save_rejects_non_png(dirs):
    images, _ = dirs
    app = capture.parse_apps(app_raw())[0]
    with pytest.raises(CaptureError, match="PNG"):
        capture.save_shot(app, app.shots[0], b"GIF89a", images, NOW)


def test_captured_images_pass_guard(cfg, make_post, dirs, monkeypatch):
    images, samples = dirs
    capture.capture(capture.parse_apps(app_raw()), browser=FakeBrowser(), media_dir=images, samples_dir=samples,
                    now=NOW)
    monkeypatch.setattr(media, "MEDIA_DIR", images)
    post = make_post(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크", media=["demo/home.png"])
    assert media.media_problems(post, dataclasses.replace(cfg, allow_media=True)) == []


def test_real_captures_pass_guard(cfg, make_post):
    """저장소에 있는 실제 캡처·영상(data/media/<앱>/media.json)도 G9 를 통과한다."""
    manifests = list(media.MEDIA_DIR.glob("*/media.json"))
    if not manifests:
        pytest.skip("아직 캡처한 화면이 없습니다")
    names = [e["file"] for m in manifests for e in json.loads(m.read_text(encoding="utf-8")).values()]
    post = make_post(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크", media=names[:20])
    assert media.media_problems(post, dataclasses.replace(cfg, allow_media=True)) == []


# ── 시연 영상 ──────────────────────────────────────────────────────────────

def test_real_apps_yaml_has_pdfmonster_clip():
    pdf = next(a for a in capture.load_apps(capture.APPS_PATH) if a.id == "pdfmonster")
    clip = next(c for c in pdf.clips if c.id == "edit-text-demo")
    assert clip.end_title == "PdfMonster" and clip.end_cta and any("zoom" in s for s in clip.steps)
    # 훅으로 시작하고, 장면마다 자막이 바뀐다
    assert "title" in clip.steps[0] and sum("caption" in s for s in clip.steps) >= 3


@pytest.mark.parametrize("change, words", [
    ({"caption": "가" * 41}, "40자"),
    ({"end": {"title": "Demo"}}, "end"),
    ({"end": {"title": "Demo", "sub": "x", "color": "purple"}}, "color"),
    ({"steps": [{"zoom": {"selector": "#a", "scale": 5}}]}, "zoom"),
    ({"steps": [{"zoom": "#a"}]}, "zoom"),
    ({"id": "home"}, "겹치지"),                  # 화면 id 와 같은 폴더에 저장되므로 겹치면 안 된다
    ({"music": "a.mp3"}, "영상 항목"),
])
def test_bad_clips(change, words):
    with pytest.raises(CaptureError, match=words):
        capture.parse_apps(clip_raw(**change))


@pytest.mark.parametrize("steps, words", [
    ([{"caption": "가" * 41}], "40자"),
    ([{"title": {"sub": "x"}}], "title"),
    ([{"title": {"text": "훅", "color": "#fff"}}], "title"),
    ([{"unzoom": "yes"}], "unzoom"),
])
def test_bad_scene_steps(steps, words):
    with pytest.raises(CaptureError, match=words):
        capture.parse_apps(clip_raw(steps=steps))


@pytest.mark.parametrize("kind_step", [{"caption": "자막"}, {"zoom": {"selector": "#a", "scale": 2}},
                                       {"title": {"text": "훅"}}, {"unzoom": True}])
def test_video_only_steps_rejected_in_shots_and_setup(kind_step):
    with pytest.raises(CaptureError, match="영상"):
        capture.parse_apps(app_raw(steps=[{"goto": "/"}, kind_step]))
    with pytest.raises(CaptureError, match="영상"):
        capture.parse_apps(clip_raw(setup=[{"goto": "/"}, kind_step]))


def test_scenes_hook_first_and_captions_change(dirs, fake_encode):
    images, samples = dirs
    raw = clip_raw(steps=[{"title": {"text": "PDF 글자 하나\n고치려고?", "sub": "바로 돼요"}}, {"caption": "장면 1"},
                          {"zoom": {"selector": "#line", "scale": 2}}, {"unzoom": True}, {"caption": "장면 2"},
                          {"title": {"text": "중간 훅"}}],
                   end={"title": "Demo", "sub": "무료", "cta": "링크는 댓글에", "color": "#112233"})
    raw["apps"][0]["clips"][0].pop("caption")
    browser = FakeBrowser()
    result = capture.capture(capture.parse_apps(raw), browser=browser, media_dir=images, samples_dir=samples, now=NOW,
                             ffmpeg="ffmpeg")
    assert result["failed"] == {}
    log = browser.pages[1].log
    kinds = [e[0] for e in log]
    # 첫 훅은 녹화 전에 다 나타나 있어야 첫 프레임(미리보기 그림)이 훅이 된다
    assert log[kinds.index("title")] == ("title", ["PDF 글자 하나\n고치려고?", "바로 돼요", "#112233"])
    assert kinds.index("title") < kinds.index("cdp") < kinds.index("title_out")
    assert [e[1] for e in log if e[0] == "caption"] == ["장면 1", "장면 2"]
    assert kinds.index("zoom") < kinds.index("unzoom") and kinds.count("title") == 2
    assert ("end", ["Demo", "무료", "링크는 댓글에", "#112233"]) in log


def test_banned_words_in_scene_texts():
    apps = capture.parse_apps(clip_raw(steps=[{"caption": "역대 최저가"}, {"title": {"text": "무료", "sub": "할인 중"}}],
                                       end={"title": "Demo", "sub": "무료", "cta": "지금 할인"}))
    problems = capture.banned_text_problems(apps, ("최저가", "할인"))
    assert len(problems) == 3 and all(p.startswith("demo/demo-clip: 금지어") for p in problems)


def test_banned_words_in_clip_text():
    apps = capture.parse_apps(clip_raw(caption="오늘만 최저가로 고쳐요"))
    assert capture.banned_text_problems(apps, ("최저가",)) == ["demo/demo-clip: 금지어 최저가 — \"오늘만 최저가로 고쳐요\""]
    assert capture.banned_text_problems(capture.parse_apps(clip_raw()), ("최저가",)) == []


def test_records_clip_in_order(dirs, fake_encode):
    images, samples = dirs
    browser = FakeBrowser()
    result = capture.capture(capture.parse_apps(clip_raw()), browser=browser, media_dir=images, samples_dir=samples,
                             now=NOW, ffmpeg="ffmpeg")
    assert result["saved"] == ["demo/home", "demo/demo-clip"] and result["failed"] == {}
    log = browser.pages[1].log
    kinds = [e[0] for e in log]
    # 준비(이동·업로드) → 녹화 시작 → 자막 → 확대·누르기·사람 속도 입력 → 끝 화면 → 녹화 끝
    assert kinds.index("upload") < kinds.index("cdp") < kinds.index("caption") < kinds.index("zoom") \
        < kinds.index("end") and log[-1] == ("cdp", "Page.stopScreencast")
    assert ("type", "25", capture.TYPE_DELAY_MS) in log and log.count(("press", "Backspace", capture.PRESS_DELAY_MS)) == 2
    # 영상에서 누르기는 누를 곳 표시가 먼저 퍼진다
    assert log.index(("tap", "#line")) < log.index(("click", "#line"))
    assert ("end", ["Demo", "무료 · 가입 없음", "", "#6d3ff0"]) in log
    assert (images / "demo" / "demo-clip.mp4").read_bytes() == MP4 and len(fake_encode) == 1
    manifest = json.loads((images / "demo" / "media.json").read_text(encoding="utf-8"))
    assert manifest["demo-clip"] == {"file": "demo/demo-clip.mp4", "kind": "video", "feature": "시연",
                                     "captured_at": "2026-11-02T21:00:00+00:00"}


def test_clip_only_rerecorded_when_needed(dirs, fake_encode):
    images, samples = dirs
    apps = capture.parse_apps(clip_raw())
    run = lambda **kw: capture.capture(apps, browser=FakeBrowser(), media_dir=images, samples_dir=samples, now=NOW,
                                       ffmpeg="ffmpeg", **kw)
    run()
    assert len(fake_encode) == 1
    result = run()                                   # 화면이 그대로고 영상이 있으면 다시 만들지 않는다
    assert len(fake_encode) == 1 and "demo/demo-clip" in result["unchanged"]
    run(clips=True)                                  # --clips 면 다시
    assert len(fake_encode) == 2
    (images / "demo" / "home.png").write_bytes(PNG + b"old")     # 화면이 바뀌면 영상도 다시
    run()
    assert len(fake_encode) == 3
    run(clips=False)
    assert len(fake_encode) == 3


def test_clip_navigation_outside_app_fails(dirs, fake_encode):
    images, samples = dirs
    result = capture.capture(capture.parse_apps(clip_raw()), browser=FakeBrowser(redirect="https://ads.example/"),
                             media_dir=images, samples_dir=samples, now=NOW, ffmpeg="ffmpeg")
    assert "앱 밖 주소" in result["failed"]["demo/demo-clip"] and not (images / "demo" / "demo-clip.mp4").exists()


def test_clip_without_ffmpeg_fails(dirs, monkeypatch):
    images, samples = dirs
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    result = capture.capture(capture.parse_apps(clip_raw()), browser=FakeBrowser(), media_dir=images,
                             samples_dir=samples, now=NOW)
    assert "ffmpeg" in result["failed"]["demo/demo-clip"]


def test_concat_list_uses_timestamps():
    frames = [(9.0, b"old"), (10.5, b"a"), (10.0, b"b"), (13.0, b"late")]
    pieces = capture.concat_list(frames, start=10.2, end=12.0)
    # 시작 전 마지막 프레임(b, 10.0)을 시작 화면으로, 끝 뒤 프레임은 버린다
    assert pieces == [(b"b", pytest.approx(0.3)), (b"a", pytest.approx(1.5))]
    with pytest.raises(CaptureError):
        capture.concat_list([], 1.0, 2.0)


def test_real_clip_is_threads_ready():
    """저장소의 실제 영상이 Threads 조건(H.264, yuv420p, 세로, 30fps, 30초 이하)을 지키는지. ffprobe 가 있을 때만."""
    import shutil
    import subprocess
    clips = list(media.MEDIA_DIR.glob("*/*.mp4"))
    if not clips or not shutil.which("ffprobe"):
        pytest.skip("영상 또는 ffprobe 가 없습니다")
    for clip in clips:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=codec_name,pix_fmt,width,height,r_frame_rate",
                              "-of", "json", str(clip)], capture_output=True, text=True, check=True).stdout
        info = json.loads(out)
        stream = info["streams"][0]
        assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p", clip
        assert stream["height"] > stream["width"] and stream["width"] <= 1920 and stream["r_frame_rate"] == "30/1"
        assert 0 < float(info["format"]["duration"]) <= capture.MAX_CLIP_SECONDS
        assert len(info["streams"]) == 1                                  # 소리 없음
