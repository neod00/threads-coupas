"""
게시물 이미지·영상 테스트 (2026-10-30, 규칙 2 개정). Threads API 와 Supabase Storage 는 가짜로 바꾼다.

  - allow_media: false 계정의 이미지 글 → 가드 G9 로 held (쿠팡 계정에 상품 사진이 붙지 않는다)
  - data/media/ 밖 경로·URL·없는 파일·JPEG/PNG/MP4 아닌 파일·크기 초과·21개 → G9
  - 이미지·영상 글에 링크 미리보기(link_in_body) → G9 (Threads 는 link_attachment 를 텍스트 글에만 받는다)
  - 1개 → IMAGE·VIDEO 컨테이너, 2개 이상 → 항목 컨테이너 + CAROUSEL(children), 링크 답글은 그대로 TEXT
  - 영상이 있으면 처리 완료를 더 오래 기다린다
  - 연습 모드는 업로드하지 않고 요청 내용만 남긴다
  - 업로드 실패 → failed (자동 재발행 안 함)
"""
import dataclasses
import logging
import types as pytypes
from datetime import timezone

import pytest

import guard
import media
import publisher
import settings
from test_publisher import DRY, EXAM_OK, NOW, REAL, FakeApi, link_ok, run, setup  # noqa: F401 (setup 은 픽스처)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32
MP4 = b"\0\0\0\x20ftypisom" + b"\0" * 32


@pytest.fixture
def images(tmp_path, monkeypatch):
    """임시 data/media/ 폴더. 파일 이름 → 내용."""
    folder = tmp_path / "media"
    (folder / "app").mkdir(parents=True)
    (folder / "app" / "home.png").write_bytes(PNG)
    (folder / "app" / "edit.jpg").write_bytes(JPG)
    (folder / "app" / "demo.mp4").write_bytes(MP4)
    monkeypatch.setattr(media, "MEDIA_DIR", folder)
    return folder


@pytest.fixture
def img_cfg(cfg):
    return dataclasses.replace(cfg, allow_media=True)


def reply_post(make_post, cfg, **kw):
    """링크는 답글에 둔 글 (이미지 글의 정상 형태)."""
    return make_post(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n자세한 건 링크에서요.", **kw)


class FakeHost:
    def __init__(self, fail=False):
        self.uploaded, self.fail = [], fail

    def public_url(self, name):
        if self.fail:
            raise media.MediaHostError(f"이미지 업로드 실패 ({name}): 연결 끊김")
        self.uploaded.append(name)
        return f"https://cdn.example/{name}"


def run_img(s, cfg, host, *, env=REAL, api=None):
    """test_publisher.run 에 media_host 를 더한 것."""
    api = api or FakeApi()
    result = publisher.run_once(s.store, cfg, env=env, api=api, link_checker=link_ok, notifier=s.notifier,
                                now=NOW.astimezone(timezone.utc), exam_status=EXAM_OK, sleep=lambda _: None,
                                media_host=host)
    return result, api


# ── 가드 G9 ────────────────────────────────────────────────────────────────

def test_post_without_images_has_no_g9(cfg, make_post):
    assert not [p for p in guard.check_post(make_post(), dataclasses.replace(cfg, allow_media=False)) if "[G9]" in p]


def test_account_without_allow_media_rejects_images(cfg, make_post, images):
    post = reply_post(make_post, cfg, media=["app/home.png"])
    problems = guard.check_post(post, dataclasses.replace(cfg, allow_media=False))
    assert any("[G9]" in p and "allow_media" in p for p in problems)


def test_valid_images_pass(img_cfg, make_post, images):
    post = reply_post(make_post, img_cfg, media=["app/home.png", "app/edit.jpg", "app/demo.mp4"])
    assert guard.check_post(post, img_cfg) == []


@pytest.mark.parametrize("name, words", [
    ("../config.yaml", "폴더 안"),
    ("https://example.com/a.png", "폴더 안"),
    ("/etc/passwd.png", "폴더 안"),
    ("app/missing.png", "없습니다"),
    ("app/notes.gif", "JPEG·PNG·MP4"),
    ("app/clip.mov", "JPEG·PNG·MP4"),
])
def test_bad_image_names(img_cfg, make_post, images, name, words):
    problems = media.media_problems(reply_post(make_post, img_cfg, media=[name]), img_cfg)
    assert problems and all("[G9]" in p for p in problems) and words in problems[0]


def test_fake_png_and_oversize(img_cfg, make_post, images):
    (images / "app" / "fake.png").write_bytes(JPG)
    (images / "app" / "fake.mp4").write_bytes(PNG)
    with (images / "app" / "big.png").open("wb") as f:
        f.write(PNG)
        f.truncate(media.MAX_BYTES["image/png"] + 1)
    with (images / "app" / "big.mp4").open("wb") as f:
        f.write(MP4)
        f.truncate(media.MAX_BYTES["video/mp4"] + 1)
    names = ["app/fake.png", "app/fake.mp4", "app/big.png", "app/big.mp4"]
    problems = media.media_problems(reply_post(make_post, img_cfg, media=names), img_cfg)
    assert sum("형식이 다릅니다" in p for p in problems) == 2
    assert any("8MB" in p for p in problems) and any("50MB" in p for p in problems)


def test_too_many_and_duplicate_images(img_cfg, make_post, images):
    problems = media.media_problems(reply_post(make_post, img_cfg, media=["app/home.png"] * 21), img_cfg)
    assert any("최대 20개" in p for p in problems) and any("두 번" in p for p in problems)


def test_image_post_cannot_use_link_preview(img_cfg, make_post, images):
    post = make_post(media=["app/home.png"])          # 기본 글은 link_in_body
    assert any("링크는 답글" in p for p in media.media_problems(post, img_cfg))


def test_sabolkka_config_keeps_images_off():
    import yaml
    raw = yaml.safe_load((settings.PROJECT_ROOT / "accounts" / "sabolkka" / "config.yaml").read_text(encoding="utf-8"))
    assert settings.parse_config(raw).allow_media is False


def test_allow_media_must_be_bool():
    import yaml
    raw = yaml.safe_load(settings.CONFIG_PATH.read_text(encoding="utf-8"))
    raw["allow_media"] = "yes"
    with pytest.raises(settings.ConfigError, match="allow_media"):
        settings.parse_config(raw)


# ── 요청 형태 ──────────────────────────────────────────────────────────────

def test_single_image_request(make_post, cfg):
    post = reply_post(make_post, cfg, topic_tag="앱개발일기")
    params = publisher.container_params(post, items=[("IMAGE", "https://cdn.example/a.png")])
    assert params == {"media_type": "IMAGE", "image_url": "https://cdn.example/a.png", "text": post["text"],
                      "topic_tag": "앱개발일기"}


def test_single_video_request(make_post, cfg):
    post = reply_post(make_post, cfg)
    params = publisher.container_params(post, items=[("VIDEO", "https://cdn.example/a.mp4")])
    assert params == {"media_type": "VIDEO", "video_url": "https://cdn.example/a.mp4", "text": post["text"]}


def test_kind_of():
    assert [media.kind_of(n) for n in ("a/b.png", "a/b.JPG", "a/b.mp4")] == ["IMAGE", "IMAGE", "VIDEO"]


def test_carousel_needs_children(make_post, cfg):
    post = reply_post(make_post, cfg)
    with pytest.raises(publisher.PublishRefused, match="캐러셀"):
        publisher.container_params(post, items=[("IMAGE", "u1"), ("IMAGE", "u2")])


def test_reply_stays_text(make_post, cfg):
    post = reply_post(make_post, cfg, threads_media_id="mine")
    params = publisher.container_params(post, reply_to_id="mine", items=[("VIDEO", "u1")])
    assert params["media_type"] == "TEXT" and "video_url" not in params


# ── 발행 흐름 ──────────────────────────────────────────────────────────────

def test_publishes_single_image_then_text_reply(img_cfg, setup, cfg, images):
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/home.png"])
    host = FakeHost()
    result, api = run_img(setup, img_cfg, host)
    assert result["action"] == "published" and host.uploaded == ["app/home.png"]
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates[0] == {"media_type": "IMAGE", "image_url": "https://cdn.example/app/home.png", "text": post["text"]}
    assert creates[1]["media_type"] == "TEXT" and creates[1]["link_attachment"] == post["link_url"]


def test_publishes_carousel(img_cfg, setup, cfg, images):
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.",
                     media=["app/home.png", "app/edit.jpg"])
    result, api = run_img(setup, img_cfg, FakeHost())
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates[:3] == [
        {"media_type": "IMAGE", "image_url": "https://cdn.example/app/home.png", "is_carousel_item": "true"},
        {"media_type": "IMAGE", "image_url": "https://cdn.example/app/edit.jpg", "is_carousel_item": "true"},
        {"media_type": "CAROUSEL", "children": "c1,c2", "text": post["text"]},
    ]
    # 이미지 컨테이너 둘 다 FINISHED 를 확인한 뒤 캐러셀을 만들고, 발행은 캐러셀만
    order = [c for c in api.calls if c[0] != "create" or c[1]["media_type"] == "CAROUSEL"]
    assert order[:3] == [("status", "c1"), ("status", "c2"), ("create", creates[2])]
    assert [c for c in api.calls if c[0] == "publish"][0] == ("publish", "c3")
    assert setup.store.get_post(post["id"])["threads_media_id"] == "m-c3"
    assert result["action"] == "published"


def test_publishes_video_and_waits_longer(img_cfg, setup, cfg, images):
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/demo.mp4"])
    api = FakeApi(status="IN_PROGRESS")
    result, _ = run_img(setup, img_cfg, FakeHost(), api=api)
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates[0] == {"media_type": "VIDEO", "video_url": "https://cdn.example/app/demo.mp4", "text": post["text"]}
    # 처리가 안 끝나면 영상은 VIDEO_MAX_POLLS 번까지 확인한다 (이미지는 MAX_POLLS)
    assert sum(c[0] == "status" for c in api.calls) == publisher.VIDEO_MAX_POLLS and result["action"] == "failed"


def test_carousel_mixes_image_and_video(img_cfg, setup, cfg, images):
    setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.",
              media=["app/demo.mp4", "app/home.png"])
    result, api = run_img(setup, img_cfg, FakeHost())
    creates = [c[1] for c in api.calls if c[0] == "create"]
    assert creates[0] == {"media_type": "VIDEO", "video_url": "https://cdn.example/app/demo.mp4", "is_carousel_item": "true"}
    assert creates[1]["media_type"] == "IMAGE" and creates[2]["media_type"] == "CAROUSEL"
    assert result["action"] == "published"


def test_image_post_held_when_account_disallows(cfg, setup, images):
    setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/home.png"])
    host = FakeHost()
    result, api = run_img(setup, dataclasses.replace(cfg, allow_media=False), host)
    assert result["action"] == "held" and api.calls == [] and host.uploaded == []


def test_upload_failure_marks_failed(img_cfg, setup, cfg, images):
    post = setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/home.png"])
    result, api = run_img(setup, img_cfg, FakeHost(fail=True))
    assert result["action"] == "failed" and api.calls == []
    assert setup.store.get_post(post["id"])["status"] == "failed"


def test_image_post_without_host_is_refused(img_cfg, setup, cfg, images):
    setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/home.png"])
    result, api = run(setup, img_cfg)
    assert result["action"] == "refused" and api.calls == []


def test_dry_run_logs_without_upload(img_cfg, setup, cfg, images, caplog):
    setup.add(variant="link_in_reply", reply_text=f"{cfg.disclosure_text}\n\n링크예요.", media=["app/home.png"])
    api = publisher.DryRunApi("123")
    with caplog.at_level(logging.INFO, logger="threads_coupas"):
        run_img(setup, img_cfg, media.DryRunMediaHost(), env=DRY, api=api)
    first = [r["params"] for r in api.requests if r["path"].endswith("/threads")][0]
    assert first["media_type"] == "IMAGE" and first["image_url"] == "<업로드할 파일 app/home.png>"


# ── Supabase 업로드 ─────────────────────────────────────────────────────────

class FakeStorage:
    def __init__(self, buckets=()):
        self.buckets, self.created, self.uploads = list(buckets), [], []

    def list_buckets(self):
        return [pytypes.SimpleNamespace(name=b) for b in self.buckets]

    def create_bucket(self, name, options=None):
        self.created.append((name, options))
        self.buckets.append(name)

    def from_(self, bucket):
        storage = self

        class Bucket:
            def upload(self, path, data, options):
                storage.uploads.append((bucket, path, data, options))

            def get_public_url(self, path):
                return f"https://proj.supabase.co/storage/v1/object/public/{bucket}/{path}"

        return Bucket()


def test_supabase_host_creates_public_bucket_and_uploads_by_hash(images):
    storage = FakeStorage()
    host = media.SupabaseMediaHost("u", "k", media_dir=images, storage=storage)
    url1 = host.public_url("app/home.png")
    url2 = host.public_url("app/edit.jpg")
    assert storage.created == [(media.BUCKET, {"public": True})]          # 버킷은 한 번만 만든다
    bucket, path, data, options = storage.uploads[0]
    assert bucket == media.BUCKET and data == PNG and options == {"content-type": "image/png", "upsert": "true"}
    assert path.startswith("openbrain/") and path.endswith(".png") and url1.endswith(path)
    assert storage.uploads[1][3]["content-type"] == "image/jpeg" and url2 != url1
    host.public_url("app/demo.mp4")
    assert storage.uploads[2][1].endswith(".mp4") and storage.uploads[2][3]["content-type"] == "video/mp4"


def test_supabase_host_skips_existing_bucket_and_wraps_errors(images):
    storage = FakeStorage(buckets=[media.BUCKET])
    host = media.SupabaseMediaHost("u", "k", media_dir=images, storage=storage)
    host.public_url("app/home.png")
    assert storage.created == []
    with pytest.raises(media.MediaHostError):
        host.public_url("../outside.png")
