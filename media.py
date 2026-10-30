"""
게시물 이미지·영상 (2026-10-30 사용자 결정, 규칙 2 개정). 앱 홍보 계정이 자기 앱 캡처와 시연 영상을 붙이려고 만들었다.

규칙 2 는 그대로 "남의 이미지·영상 금지"다. 그래서 다음만 쓸 수 있다 (가드 G9 가 생성·발행 때 모두 검사).
  - config.yaml 의 allow_media: true 인 계정만 (쿠팡 계정은 false — 상품 사진을 붙일 길이 없다)
  - posts.media 의 각 항목은 이 계정의 data/media/ 폴더 안 파일 이름. 사용자가 넣었거나 capture.py 가
    내 앱 주소에서만 찍은 파일이다. URL 이나 폴더 밖 경로는 받지 않는다 (코드가 남의 이미지를 가져올 길을 두지 않는다)
  - 이미지 JPEG·PNG 8MB 이하, 영상 MP4 50MB 이하(Supabase 무료 한도), 글 하나에 최대 20개
    (THREADS_API_NOTES.md, 2026-10-30 Meta 문서 확인)
  - 이미지·영상 글에는 링크 미리보기(link_attachment)가 붙지 않으므로 링크는 답글에만 (link_in_reply)

Threads 는 공개 주소에서 파일을 받아 간다. 발행기가 발행 직전에 파일을 Supabase Storage 의 공개 버킷에 올리고
그 주소를 image_url·video_url 로 넘긴다. 객체 이름은 내용 해시라 같은 파일은 다시 올라가지 않고, 내용을 바꾸면 주소도 바뀐다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from settings import ACCOUNT, ACCOUNT_DIR, Config

MEDIA_DIR = ACCOUNT_DIR / "data" / "media"
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}
MAX_BYTES = {"image/jpeg": 8 * 1024 * 1024, "image/png": 8 * 1024 * 1024, "video/mp4": 50 * 1024 * 1024}
MAX_ITEMS = 20
BUCKET = "post-images"          # 2026-10-30 처음 만든 이름 그대로 (영상도 여기에 둔다)


class MediaHostError(Exception):
    """파일을 공개 주소에 올리지 못했다."""


def kind_of(name: str) -> str:
    """Threads media_type: IMAGE 또는 VIDEO."""
    return "VIDEO" if CONTENT_TYPES.get(Path(name).suffix.lower(), "").startswith("video/") else "IMAGE"


def _magic_ok(head: bytes, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    return head[4:8] == b"ftyp"     # MP4


def resolve(name: str, base_dir: Path | None = None) -> Path | None:
    """base_dir(기본 data/media/) 안의 파일 경로. 폴더 밖을 가리키거나 URL·절대 경로면 None."""
    base = (base_dir or MEDIA_DIR).resolve()
    if not isinstance(name, str) or not name.strip() or "://" in name or Path(name).is_absolute():
        return None
    path = (base / name).resolve()
    return path if path.is_relative_to(base) and path != base else None


def media_problems(post: dict, cfg: Config, media_dir: Path | None = None) -> list[str]:
    """[G9] 문제 목록. 이미지·영상이 없는 글은 빈 목록."""
    items = post.get("media") or []
    if not items:
        return []
    if not cfg.allow_media:
        return ["[G9] 이 계정은 이미지·영상을 붙일 수 없습니다 (규칙 2, config.yaml allow_media: false)"]
    problems = []
    if len(items) > MAX_ITEMS:
        problems.append(f"[G9] 이미지·영상이 {len(items)}개입니다 (최대 {MAX_ITEMS}개)")
    if len(set(items)) != len(items):
        problems.append("[G9] 같은 파일이 두 번 들어 있습니다")
    if post.get("link_url") and post.get("variant") != "link_in_reply":
        problems.append("[G9] 이미지·영상 글에는 링크 미리보기가 붙지 않습니다. 링크는 답글에 두세요 (link_in_reply)")
    for name in items:
        path = resolve(name, media_dir)
        if path is None:
            problems.append(f"[G9] 계정의 data/media/ 폴더 안 파일 이름만 쓸 수 있습니다: {name!r}")
            continue
        content_type = CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            problems.append(f"[G9] JPEG·PNG·MP4 만 쓸 수 있습니다: {name}")
        elif not path.is_file():
            problems.append(f"[G9] 파일이 없습니다: {name}")
        elif path.stat().st_size > MAX_BYTES[content_type]:
            problems.append(f"[G9] 파일이 {MAX_BYTES[content_type] // (1024 * 1024)}MB 보다 큽니다: {name}")
        else:
            with path.open("rb") as f:
                if not _magic_ok(f.read(12), content_type):
                    problems.append(f"[G9] 확장자와 실제 파일 형식이 다릅니다: {name}")
    return problems


def object_name(path: Path, data: bytes) -> str:
    """버킷 안 이름: <계정>/<내용 해시 16자리><확장자>. 계정마다 폴더를 나눈다 (규칙 1)."""
    return f"{ACCOUNT or 'openbrain'}/{hashlib.sha256(data).hexdigest()[:16]}{path.suffix.lower()}"


class SupabaseMediaHost:
    """같은 Supabase 프로젝트의 공개 버킷에 올리고 공개 주소를 돌려준다. 버킷이 없으면 만든다."""

    def __init__(self, url: str, service_role_key: str, media_dir: Path | None = None, storage=None):
        if storage is None:
            from supabase import create_client
            storage = create_client(url, service_role_key).storage
        self.storage = storage
        self.media_dir = media_dir
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if BUCKET not in {b.name for b in self.storage.list_buckets()}:
            self.storage.create_bucket(BUCKET, options={"public": True})
        self._bucket_ready = True

    def public_url(self, name: str) -> str:
        path = resolve(name, self.media_dir)
        if path is None or not path.is_file():
            raise MediaHostError(f"파일이 없습니다: {name!r}")
        data = path.read_bytes()
        obj = object_name(path, data)
        try:
            self._ensure_bucket()
            self.storage.from_(BUCKET).upload(obj, data, {"content-type": CONTENT_TYPES[path.suffix.lower()],
                                                          "upsert": "true"})
            return self.storage.from_(BUCKET).get_public_url(obj)
        except Exception as e:      # supabase 오류 종류가 버전마다 달라 한데 묶는다
            raise MediaHostError(f"업로드 실패 ({name}): {e}") from None


class DryRunMediaHost:
    """연습 모드: 올리지 않고 자리 표시 주소만 돌려준다."""

    def public_url(self, name: str) -> str:
        return f"<업로드할 파일 {name}>"
