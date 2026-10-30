"""
내 앱 화면 자동 캡처·시연 영상 (2026-10-30 사용자 결정). data/apps.yaml 의 앱마다
  - shots: 정해 둔 화면을 휴대폰 크기로 찍어 data/media/<앱 id>/<화면 id>.png
  - clips: 정해 둔 조작을 녹화하고 자막·확대·끝 화면을 얹은 짧은 세로 영상 data/media/<앱 id>/<영상 id>.mp4
를 만든다. 발행기는 이 파일을 게시물 이미지·영상으로 쓴다 (media.py, 가드 G9).

규칙 2 (남의 이미지·영상 금지):
  - allow_media: true 계정만 찍는다
  - 앱 주소의 호스트(와 allowed_hosts)로만 이동한다. 다른 사이트로 넘어가려 하면 그 화면은 실패로 두고 저장하지 않는다
  - 업로드할 예시 파일은 data/app_samples/<앱 id>/ 안의 파일만 (진짜 개인정보가 든 파일은 넣지 않는다)
  - 자막·끝 화면 글은 apps.yaml 에 사람이 적은 문구 그대로 (금지어 검사, 40자 이하)

영상은 Chrome 화면 전송(CDP screencast, 2배 해상도 JPEG 프레임)을 받아 ffmpeg 으로 H.264 MP4(30fps, 소리 없음)로 만든다.
Playwright 기본 녹화는 1배 해상도라 글자가 흐려서 쓰지 않는다 (2026-10-30 시험).

목록 data/media/<앱 id>/media.json: {id: {file, kind, feature, captured_at}}. 화면 그림이 그대로면 captured_at 도 그대로 둔다
(매주 캡처해도 바뀐 화면만 커밋되게). 영상은 녹화할 때마다 바이트가 달라지므로 파일이 없거나, 같은 앱 화면이 바뀌었거나,
--clips 로 시킬 때만 다시 만든다.

실행:
  python capture.py                         # 모든 앱
  python capture.py --app pdfmonster        # 앱 하나
  python capture.py --app pdfmonster --only edit-text-demo --clips
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

import media
from settings import ACCOUNT_DIR

log = logging.getLogger("threads_coupas")

APPS_PATH = ACCOUNT_DIR / "data" / "apps.yaml"
SAMPLES_DIR = ACCOUNT_DIR / "data" / "app_samples"
MANIFEST = "media.json"
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
STEP_KINDS = ("goto", "click", "upload", "fill", "type", "press", "wait", "wait_for", "zoom", "unzoom", "caption",
              "title")
VIDEO_ONLY_KINDS = ("zoom", "unzoom", "caption", "title")       # 화면 캡처(shots)에서는 쓰지 않는다
VIEWPORT = {"width": 390, "height": 844}
SCALE = 2
STEP_TIMEOUT_MS = 20_000
MAX_WAIT_MS = 15_000
MAX_CLIP_SECONDS = 30
MAX_TEXT_CHARS = 40
TYPE_DELAY_MS = 120             # 영상에서 글자가 하나씩 써지는 속도
PRESS_DELAY_MS = 50
END_CARD_MS = 2200
TITLE_MS = 1700                 # 첫 화면 훅 문구를 보여 주는 시간 (그 뒤 0.4초 동안 사라진다)
TAP_LEAD_MS = 260               # 누르는 표시가 퍼지기 시작하고 실제로 누르기까지
FONT = '"Noto Sans CJK KR","Noto Sans KR","Malgun Gothic",sans-serif'    # JS 작은따옴표 문자열 안에 들어간다

# 자막: 아래쪽 어두운 띠. 페이지(body) 밖 html 에 붙여 확대(body transform)의 영향을 받지 않는다.
# 이미 있으면 위로 사라졌다가 새 문구로 아래에서 올라온다 (장면 전환)
CAPTION_JS = """(text) => {
  let el = document.getElementById('__tc_caption');
  const show = () => requestAnimationFrame(() => requestAnimationFrame(() => {
    el.style.opacity = 1; el.style.transform = 'none'; }));
  if (!el) {
    el = document.createElement('div');
    el.id = '__tc_caption';
    el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:116px;z-index:2147483646;padding:14px 18px;'
      + 'border-radius:16px;background:rgba(17,17,27,.9);color:#fff;font:800 21px/1.35 __FONT__;text-align:center;word-break:keep-all;'
      + 'box-shadow:0 8px 24px rgba(0,0,0,.25);opacity:0;transform:translateY(14px) scale(.96);'
      + 'transition:opacity .35s,transform .35s cubic-bezier(.2,.8,.2,1.2);pointer-events:none';
    el.textContent = text;
    document.documentElement.appendChild(el);
    show();
    return;
  }
  el.style.opacity = 0; el.style.transform = 'translateY(-10px) scale(.96)';
  setTimeout(() => { el.textContent = text; el.style.transition = 'none'; el.style.transform = 'translateY(14px) scale(.96)';
    requestAnimationFrame(() => { el.style.transition = 'opacity .35s,transform .35s cubic-bezier(.2,.8,.2,1.2)'; show(); });
  }, 220);
}""".replace("__FONT__", FONT)

# 첫 화면 훅: 화면 전체를 덮는 큰 문구. 튀어나오듯 커졌다가 TITLE_MS 뒤 위로 사라지며 앱 화면이 드러난다
TITLE_JS = """([text, sub, color]) => {
  const el = document.createElement('div');
  el.id = '__tc_title';
  el.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;'
    + 'justify-content:center;gap:18px;padding:28px;text-align:center;word-break:keep-all;color:#fff;font-family:__FONT__;'
    + 'background:linear-gradient(160deg,' + color + ',#1b1033);transition:opacity .4s,transform .4s';
  const t = document.createElement('div'); t.textContent = text;
  t.style.cssText = 'font-size:31px;font-weight:900;line-height:1.3;white-space:pre-line;word-break:keep-all;transform:scale(.8);'
    + 'opacity:0;transition:transform .45s cubic-bezier(.2,.9,.3,1.4),opacity .3s';
  el.append(t);
  if (sub) { const s = document.createElement('div'); s.textContent = sub;
    s.style.cssText = 'font-size:20px;font-weight:700;opacity:0;transform:translateY(10px);transition:opacity .4s .35s,transform .4s .35s';
    el.append(s); }
  document.documentElement.appendChild(el);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    t.style.transform = 'none'; t.style.opacity = 1;
    if (sub) { el.lastChild.style.opacity = .9; el.lastChild.style.transform = 'none'; } }));
}""".replace("__FONT__", FONT)

TITLE_OUT_JS = """() => {
  const el = document.getElementById('__tc_title');
  if (!el) return;
  el.style.opacity = 0; el.style.transform = 'translateY(-40px)';
  setTimeout(() => el.remove(), 450);
}"""

# 누르는 표시: 누를 곳에서 동그라미가 퍼진다 (확대 중이어도 화면 좌표 기준)
TAP_JS = """(sel) => {
  const r = document.querySelector(sel).getBoundingClientRect();
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:2147483645;width:56px;height:56px;margin:-28px 0 0 -28px;border-radius:50%;'
    + 'background:rgba(109,63,240,.35);border:3px solid rgba(109,63,240,.9);pointer-events:none;'
    + 'left:' + (r.left + Math.min(r.width / 2, 60)) + 'px;top:' + (r.top + r.height / 2) + 'px;'
    + 'transform:scale(.3);opacity:1;transition:transform .55s cubic-bezier(.2,.8,.2,1),opacity .55s';
  document.documentElement.appendChild(d);
  requestAnimationFrame(() => requestAnimationFrame(() => { d.style.transform = 'scale(1.5)'; d.style.opacity = 0; }));
  setTimeout(() => d.remove(), 700);
}"""

UNZOOM_JS = """() => {
  const b = document.body;
  b.style.transition = 'transform 1s cubic-bezier(.2,.7,.2,1)';
  b.style.transform = 'none';
}"""

# 확대: 대상이 화면 가운데 위쪽에 오게 옮기고 키운다. 빈 여백이 보이지 않게 이동 범위를 막는다
ZOOM_JS = """([sel, s]) => {
  const r = document.querySelector(sel).getBoundingClientRect();
  const W = innerWidth, H = innerHeight, cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const tx = Math.min(0, Math.max(W - s * W, W / 2 - s * cx));
  const ty = Math.min(0, Math.max(H - s * H, H / 2.6 - s * cy));
  const b = document.body;
  document.documentElement.style.overflow = 'hidden';
  b.style.overflow = 'hidden';
  b.style.transformOrigin = '0 0';
  b.style.transition = 'transform 1.2s cubic-bezier(.2,.7,.2,1)';
  b.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + s + ')';
}"""

# 끝 화면: 앱 이름이 튀어나오고, 설명과 행동 유도 문구(cta)가 차례로 올라온다
END_JS = """([title, sub, cta, color]) => {
  const cap = document.getElementById('__tc_caption'); if (cap) cap.style.opacity = 0;
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;'
    + 'justify-content:center;gap:16px;padding:24px;text-align:center;word-break:keep-all;color:#fff;opacity:0;transition:opacity .4s;'
    + 'font-family:__FONT__;background:linear-gradient(160deg,' + color + ',#1b1033)';
  const parts = [[title, 'font-size:46px;font-weight:900', 0], [sub, 'font-size:21px;font-weight:700;opacity:.92', .25]];
  if (cta) parts.push([cta, 'margin-top:14px;padding:12px 22px;border-radius:999px;background:#fff;color:' + color
    + ';font-size:20px;font-weight:800', .5]);
  const nodes = parts.map(([text, css, delay]) => { const n = document.createElement('div'); n.textContent = text;
    n.style.cssText = css + ';opacity:0;transform:translateY(16px) scale(.92);transition:opacity .4s ' + delay
      + 's,transform .45s cubic-bezier(.2,.9,.3,1.3) ' + delay + 's'; el.append(n); return n; });
  document.documentElement.appendChild(el);
  requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = 1;
    nodes.forEach(n => { n.style.opacity = n === nodes[1] ? .92 : 1; n.style.transform = 'none'; }); }));
}""".replace("__FONT__", FONT)


class CaptureError(Exception):
    """설정 오류이거나 화면·영상 하나를 만들지 못했다."""


@dataclass(frozen=True)
class Shot:
    id: str
    feature: str
    steps: tuple[dict, ...]


@dataclass(frozen=True)
class Clip:
    id: str
    feature: str
    caption: str
    end_title: str
    end_sub: str
    end_color: str
    setup: tuple[dict, ...]
    steps: tuple[dict, ...]
    end_cta: str = ""


@dataclass(frozen=True)
class App:
    id: str
    name: str
    url: str
    hosts: frozenset[str]
    shots: tuple[Shot, ...]
    clips: tuple[Clip, ...] = ()


# ── 설정 읽기 ───────────────────────────────────────────────────────────────

def _check_step(step, where: str) -> dict:
    if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in STEP_KINDS:
        raise CaptureError(f"{where}: 단계는 {', '.join(STEP_KINDS)} 중 하나여야 합니다: {step!r}")
    kind, value = next(iter(step.items()))
    if kind == "upload":
        if not isinstance(value, dict) or set(value) != {"selector", "file"}:
            raise CaptureError(f"{where}: upload 는 {{selector, file}} 이어야 합니다: {value!r}")
    elif kind == "fill":
        if not isinstance(value, dict) or set(value) != {"selector", "text"}:
            raise CaptureError(f"{where}: fill 은 {{selector, text}} 이어야 합니다: {value!r}")
    elif kind == "caption":
        _text(value, f"{where} caption")
    elif kind == "title":
        if not isinstance(value, dict) or "text" not in value or set(value) - {"text", "sub"}:
            raise CaptureError(f"{where}: title 은 {{text (와 선택 sub)}} 이어야 합니다: {value!r}")
        _text(value["text"], f"{where} title.text")
        if "sub" in value:
            _text(value["sub"], f"{where} title.sub")
    elif kind == "unzoom":
        if value is not True:
            raise CaptureError(f"{where}: unzoom 은 'unzoom: true' 로 씁니다: {value!r}")
    elif kind == "zoom":
        scale = value.get("scale") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {"selector", "scale"} or isinstance(scale, bool)
                or not isinstance(scale, int | float) or not 1 < scale <= 3):
            raise CaptureError(f"{where}: zoom 은 {{selector, scale(1~3)}} 이어야 합니다: {value!r}")
    elif kind == "press":
        ok = (isinstance(value, str) and bool(value)) or (
            isinstance(value, dict) and set(value) == {"key", "times"} and isinstance(value["key"], str)
            and isinstance(value["times"], int) and not isinstance(value["times"], bool) and 1 <= value["times"] <= 50)
        if not ok:
            raise CaptureError(f"{where}: press 는 키 이름 또는 {{key, times(1~50)}} 이어야 합니다: {value!r}")
    elif kind == "wait":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_WAIT_MS:
            raise CaptureError(f"{where}: wait 는 1~{MAX_WAIT_MS} 밀리초여야 합니다: {value!r}")
    elif kind == "goto":
        if not isinstance(value, str) or not value.startswith("/"):
            raise CaptureError(f"{where}: goto 는 / 로 시작하는 앱 안 경로여야 합니다: {value!r}")
    elif not isinstance(value, str) or not value:
        raise CaptureError(f"{where}: {kind} 값이 비어 있습니다")
    return {kind: value}


def _steps(raw, where: str, *, video: bool = False) -> tuple[dict, ...]:
    """video=False(화면 캡처·영상 준비 단계)면 확대·자막·훅 같은 영상 전용 단계를 받지 않는다."""
    if not isinstance(raw, list) or not raw:
        raise CaptureError(f"{where}: steps 가 비어 있습니다")
    steps = tuple(_check_step(st, where) for st in raw)
    misplaced = [k for st in steps for k in st if k in VIDEO_ONLY_KINDS and not video]
    if misplaced:
        raise CaptureError(f"{where}: {', '.join(misplaced)} 는 영상(clips)의 steps 에서만 씁니다")
    return steps


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TEXT_CHARS:
        raise CaptureError(f"{where}: 1~{MAX_TEXT_CHARS}자 문구여야 합니다: {value!r}")
    return value.strip()


def _item_id(raw_id, seen: set, where: str) -> str:
    if not isinstance(raw_id, str) or not ID_RE.fullmatch(raw_id) or raw_id in seen:
        raise CaptureError(f"{where}: id 는 겹치지 않는 소문자·숫자·하이픈이어야 합니다: {raw_id!r}")
    seen.add(raw_id)
    return raw_id


def _feature(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureError(f"{where}: feature(어떤 기능 화면인지)가 비어 있습니다")
    return value.strip()


def parse_apps(raw) -> tuple[App, ...]:
    if not isinstance(raw, dict) or set(raw) != {"apps"} or not isinstance(raw["apps"], list):
        raise CaptureError("apps.yaml 은 apps: 목록 하나만 있어야 합니다")
    apps, seen = [], set()
    for item in raw["apps"]:
        if not isinstance(item, dict) or not {"id", "name", "url", "shots"} <= set(item) \
                or set(item) - {"id", "name", "url", "shots", "clips", "allowed_hosts", "info"}:
            raise CaptureError(f"앱 항목은 id, name, url, shots (와 선택 clips, allowed_hosts, info) 만 가집니다: {item!r}")
        app_id = _item_id(item["id"], seen, "앱")
        url = urlparse(str(item["url"]))
        if url.scheme != "https" or not url.hostname:
            raise CaptureError(f"{app_id}: url 은 https 주소여야 합니다: {item['url']!r}")
        hosts = {url.hostname.lower(), *(h.lower() for h in item.get("allowed_hosts") or [])}
        ids: set = set()            # 화면·영상 id 는 같은 폴더에 저장되므로 함께 겹치지 않아야 한다
        shots = []
        for s in item["shots"] or []:
            if not isinstance(s, dict) or set(s) != {"id", "feature", "steps"}:
                raise CaptureError(f"{app_id}: 화면 항목은 id, feature, steps 를 가집니다: {s!r}")
            where = f"{app_id}/{s['id']}"
            shots.append(Shot(_item_id(s["id"], ids, app_id), _feature(s["feature"], where), _steps(s["steps"], where)))
        if not shots:
            raise CaptureError(f"{app_id}: 찍을 화면(shots)이 없습니다")
        clips = []
        for c in item.get("clips") or []:
            keys = {"id", "feature", "caption", "end", "setup", "steps"}
            if not isinstance(c, dict) or not {"id", "feature", "steps", "end"} <= set(c) or set(c) - keys:
                raise CaptureError(f"{app_id}: 영상 항목은 id, feature, end, steps (와 선택 caption, setup) 를 가집니다: {c!r}")
            where = f"{app_id}/{c['id']}"
            end = c["end"]
            if not isinstance(end, dict) or not {"title", "sub"} <= set(end) \
                    or set(end) - {"title", "sub", "cta", "color"}:
                raise CaptureError(f"{where}: end 는 {{title, sub (와 선택 cta, color)}} 이어야 합니다: {end!r}")
            color = end.get("color", "#6d3ff0")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise CaptureError(f"{where}: end.color 는 #RRGGBB 여야 합니다: {color!r}")
            clips.append(Clip(_item_id(c["id"], ids, app_id), _feature(c["feature"], where),
                              _text(c["caption"], f"{where} caption") if c.get("caption") is not None else "",
                              _text(end["title"], f"{where} end.title"), _text(end["sub"], f"{where} end.sub"), color,
                              _steps(c["setup"], f"{where} setup") if c.get("setup") is not None else (),
                              _steps(c["steps"], where, video=True),
                              _text(end["cta"], f"{where} end.cta") if end.get("cta") is not None else ""))
        apps.append(App(app_id, str(item["name"]), item["url"], frozenset(hosts), tuple(shots), tuple(clips)))
    return tuple(apps)


def load_apps(path: Path | None = None) -> tuple[App, ...]:
    return parse_apps(yaml.safe_load((path or APPS_PATH).read_text(encoding="utf-8")))


def clip_texts(clip: Clip) -> list[str]:
    """영상에 들어가는 모든 문구: 처음 자막, 장면 자막, 훅, 끝 화면."""
    texts = [clip.caption, clip.end_title, clip.end_sub, clip.end_cta]
    for step in clip.steps:
        if "caption" in step:
            texts.append(step["caption"])
        elif "title" in step:
            texts += [step["title"]["text"], step["title"].get("sub", "")]
    return [t for t in texts if t]


def banned_text_problems(apps, banned_words) -> list[str]:
    """영상 문구의 금지어 (가드 G4 와 같은 목록)."""
    problems = []
    for app in apps:
        for clip in app.clips:
            for text in clip_texts(clip):
                found = [w for w in banned_words if w in text]
                if found:
                    problems.append(f"{app.id}/{clip.id}: 금지어 {', '.join(found)} — \"{text}\"")
    return problems


# ── 조작 ───────────────────────────────────────────────────────────────────

def host_allowed(url: str, hosts) -> bool:
    """페이지 이동이 허용되는 주소인지. 앱 호스트(하위 경로 포함)만, about:blank 는 허용."""
    if url.startswith("about:"):
        return True
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in hosts


def run_steps(page, app: App, steps, samples_dir: Path | None = None, *, slow: bool = False,
              color: str = "#6d3ff0") -> None:
    """단계를 차례로 실행한다. slow=True(영상)면 누를 곳에 표시를 띄우고 글자를 사람이 치는 속도로 입력한다.
    page 는 Playwright Page. color 는 훅 화면 색 (영상의 end.color)."""
    for step in steps:
        kind, value = next(iter(step.items()))
        if kind == "goto":
            page.goto(urljoin(app.url, value.lstrip("/")), wait_until="networkidle")
        elif kind == "click":
            if slow:
                page.wait_for_selector(value, state="visible", timeout=STEP_TIMEOUT_MS)
                page.evaluate(TAP_JS, value)
                page.wait_for_timeout(TAP_LEAD_MS)
            page.click(value, timeout=STEP_TIMEOUT_MS)
        elif kind == "upload":
            path = media.resolve(value["file"], (samples_dir or SAMPLES_DIR) / app.id)
            if path is None or not path.is_file():
                raise CaptureError(f"예시 파일은 data/app_samples/{app.id}/ 안에 있어야 합니다: {value['file']!r}")
            page.set_input_files(value["selector"], str(path), timeout=STEP_TIMEOUT_MS)
        elif kind == "fill":
            page.fill(value["selector"], value["text"], timeout=STEP_TIMEOUT_MS)
        elif kind == "type":
            page.keyboard.type(value, delay=TYPE_DELAY_MS if slow else 0)
        elif kind == "press":
            key, times = (value, 1) if isinstance(value, str) else (value["key"], value["times"])
            for _ in range(times):
                page.keyboard.press(key, delay=PRESS_DELAY_MS if slow else 0)
        elif kind == "wait":
            page.wait_for_timeout(value)
        elif kind == "wait_for":
            page.wait_for_selector(value, state="visible", timeout=STEP_TIMEOUT_MS)
        elif kind == "zoom":
            page.wait_for_selector(value["selector"], state="visible", timeout=STEP_TIMEOUT_MS)
            page.evaluate(ZOOM_JS, [value["selector"], value["scale"]])
        elif kind == "unzoom":
            page.evaluate(UNZOOM_JS)
        elif kind == "caption":
            page.evaluate(CAPTION_JS, value)
        elif kind == "title":
            page.evaluate(TITLE_JS, [value["text"], value.get("sub", ""), color])
            page.wait_for_timeout(TITLE_MS)
            page.evaluate(TITLE_OUT_JS)


# ── 영상 ───────────────────────────────────────────────────────────────────

class Screencast:
    """CDP 화면 전송 프레임을 모은다. 화면이 바뀔 때만 프레임이 오므로 시각(timestamp)으로 길이를 맞춘다."""

    def __init__(self, context, page):
        self.cdp = context.new_cdp_session(page)
        self.frames: list[tuple[float, bytes]] = []
        self.cdp.on("Page.screencastFrame", self._on_frame)

    def _on_frame(self, ev) -> None:
        self.frames.append((ev["metadata"]["timestamp"], base64.b64decode(ev["data"])))
        self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

    def start(self) -> None:
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "everyNthFrame": 1,
                                               "maxWidth": VIEWPORT["width"] * SCALE,
                                               "maxHeight": VIEWPORT["height"] * SCALE})

    def stop(self) -> None:
        self.cdp.send("Page.stopScreencast")


def concat_list(frames: list[tuple[float, bytes]], start: float, end: float) -> list[tuple[bytes, float]]:
    """[(JPEG, 보여줄 초)]. start 이전 프레임은 마지막 하나만 시작 화면으로 남긴다."""
    frames = sorted(frames, key=lambda f: f[0])
    before = [f for f in frames if f[0] < start]
    kept = ([(start, before[-1][1])] if before else []) + [f for f in frames if start <= f[0] <= end]
    if not kept:
        raise CaptureError("녹화된 프레임이 없습니다")
    return [(data, max((kept[i + 1][0] if i + 1 < len(kept) else end) - ts, 0.001)) for i, (ts, data) in enumerate(kept)]


def encode_mp4(pieces: list[tuple[bytes, float]], out: Path, ffmpeg: str) -> None:
    """프레임을 30fps H.264 MP4(세로 780x1688, 소리 없음, faststart)로. Threads 영상 조건 (THREADS_API_NOTES.md)."""
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        for i, (data, seconds) in enumerate(pieces):
            (Path(tmp) / f"f{i:05d}.jpg").write_bytes(data)
            lines += [f"file 'f{i:05d}.jpg'", f"duration {seconds:.4f}"]
        lines.append(f"file 'f{len(pieces) - 1:05d}.jpg'")       # concat 은 마지막 파일을 한 번 더 적어야 길이가 맞는다
        (Path(tmp) / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        size = f"{VIEWPORT['width'] * SCALE}:{VIEWPORT['height'] * SCALE}"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
                        # JPEG 은 전체 범위(yuvj420p)라 일반 범위 yuv420p 로 바꾼다 (재생기·플랫폼 호환)
                        "-vf", f"fps=30,scale={size}:flags=lanczos:out_range=tv,format=yuv420p",
                        "-pix_fmt", "yuv420p", "-color_range", "tv", "-c:v", "libx264",
                        "-preset", "slow", "-crf", "18", "-movflags", "+faststart", "-an", str(out.resolve())],
                       cwd=tmp, check=True, capture_output=True)


def record_clip(context, page, app: App, clip: Clip, samples_dir: Path | None, out: Path, ffmpeg: str) -> None:
    run_steps(page, app, clip.setup, samples_dir)
    steps = clip.steps
    if "title" in steps[0]:
        # 첫 프레임이 Threads 미리보기 그림이 된다: 훅 문구가 다 나타난 뒤에 녹화를 시작한다
        title = steps[0]["title"]
        page.evaluate(TITLE_JS, [title["text"], title.get("sub", ""), clip.end_color])
        page.wait_for_timeout(900)
    cast = Screencast(context, page)
    cast.start()
    page.wait_for_timeout(150)
    start = time.time()
    if "title" in steps[0]:
        page.wait_for_timeout(TITLE_MS)
        page.evaluate(TITLE_OUT_JS)
        steps = steps[1:]
    if clip.caption:
        page.evaluate(CAPTION_JS, clip.caption)
    run_steps(page, app, steps, samples_dir, slow=True, color=clip.end_color)
    page.evaluate(END_JS, [clip.end_title, clip.end_sub, clip.end_cta, clip.end_color])
    page.wait_for_timeout(END_CARD_MS)
    end = time.time()
    cast.stop()
    if end - start > MAX_CLIP_SECONDS:
        raise CaptureError(f"영상이 {end - start:.0f}초입니다 (최대 {MAX_CLIP_SECONDS}초). 기다리는 시간을 줄이세요")
    encode_mp4(concat_list(cast.frames, start, end), out, ffmpeg)


# ── 저장 ───────────────────────────────────────────────────────────────────

def _write_manifest(folder: Path, item_id: str, entry: dict) -> None:
    path = folder / MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    manifest[item_id] = entry
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _old_entry(folder: Path, item_id: str) -> dict:
    path = folder / MANIFEST
    return (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}).get(item_id) or {}


def save_shot(app: App, shot: Shot, png: bytes, media_dir: Path, now: datetime) -> bool:
    """data/media/<앱>/<화면>.png 와 media.json 을 고친다. 그림이 바뀌었으면 True."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > media.MAX_BYTES["image/png"]:
        raise CaptureError(f"{app.id}/{shot.id}: 캡처가 PNG 가 아니거나 8MB 를 넘습니다")
    folder = media_dir / app.id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{shot.id}.png"
    changed = not path.exists() or path.read_bytes() != png
    if changed:
        path.write_bytes(png)
    old = _old_entry(folder, shot.id)
    _write_manifest(folder, shot.id, {
        "file": f"{app.id}/{shot.id}.png", "kind": "image", "feature": shot.feature,
        "captured_at": now.isoformat(timespec="seconds") if changed or not old.get("captured_at") else old["captured_at"]})
    return changed


def save_clip(app: App, clip: Clip, mp4: Path, media_dir: Path, now: datetime) -> None:
    head = mp4.read_bytes()[:12]
    if head[4:8] != b"ftyp" or mp4.stat().st_size > media.MAX_BYTES["video/mp4"]:
        raise CaptureError(f"{app.id}/{clip.id}: 영상이 MP4 가 아니거나 50MB 를 넘습니다")
    folder = media_dir / app.id
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(str(mp4), str(folder / f"{clip.id}.mp4"))
    _write_manifest(folder, clip.id, {"file": f"{app.id}/{clip.id}.mp4", "kind": "video", "feature": clip.feature,
                                      "captured_at": now.isoformat(timespec="seconds")})


# ── 실행 ───────────────────────────────────────────────────────────────────

def _open(browser, app: App, blocked: list[str]):
    """새 브라우저 창. 규칙 2: 앱 밖의 페이지로는 이동하지 않는다 (글꼴·스크립트 같은 부속 요청은 그대로 둔다).
    is_mobile 은 끈다: 켜면 확대한 내용 때문에 화면 기준이 넓어져 자막·끝 화면이 밀린다 (2026-10-30 시험)."""
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, is_mobile=False, has_touch=True,
                                  locale="ko-KR")
    page = context.new_page()

    def guard(route, request):
        if request.is_navigation_request() and request.frame == page.main_frame \
                and not host_allowed(request.url, app.hosts):
            blocked.append(request.url)
            return route.abort()
        return route.continue_()

    page.route("**/*", guard)
    return context, page


def capture(apps, *, browser, media_dir: Path | None = None, samples_dir: Path | None = None,
            now: datetime | None = None, clips: bool | None = None, ffmpeg: str | None = None) -> dict:
    """앱의 화면을 찍고 영상을 만든다. clips: True 면 영상을 모두 다시, False 면 영상은 건너뜀,
    None 이면 파일이 없거나 같은 앱 화면이 바뀐 영상만. 결과: {"saved": [...], "unchanged": [...], "failed": {id: 이유}}."""
    media_dir = media_dir or media.MEDIA_DIR
    now = now or datetime.now(timezone.utc)
    result = {"saved": [], "unchanged": [], "failed": {}}

    def attempt(name: str, work) -> bool | None:
        blocked: list[str] = []
        context, page = _open(browser, app, blocked)
        try:
            outcome = work(context, page)
            if blocked:
                raise CaptureError(f"앱 밖 주소로 이동하려 했습니다: {blocked[0]}")
            return outcome
        except Exception as e:      # 하나가 실패해도 나머지는 만든다
            reason = f"앱 밖 주소로 이동하려 했습니다: {blocked[0]}" if blocked else str(e).splitlines()[0][:300]
            result["failed"][name] = reason
            log.error(f"[캡처] {name} 실패: {reason}")
            return None
        finally:
            context.close()

    for app in apps:
        screens_changed = False
        for shot in app.shots:
            name = f"{app.id}/{shot.id}"

            def take(context, page, shot=shot):
                run_steps(page, app, shot.steps, samples_dir)
                return page.screenshot(type="png")

            png = attempt(name, take)
            if png is None:
                continue
            changed = save_shot(app, shot, png, media_dir, now)
            screens_changed |= changed
            result["saved" if changed else "unchanged"].append(name)
            log.info(f"[캡처] {name} {'저장' if changed else '그대로'}")

        for clip in app.clips:
            name = f"{app.id}/{clip.id}"
            exists = (media_dir / app.id / f"{clip.id}.mp4").exists()
            if clips is False or (clips is None and exists and not screens_changed):
                result["unchanged"].append(name)
                continue
            ffmpeg = ffmpeg or shutil.which("ffmpeg")
            if not ffmpeg:
                result["failed"][name] = "ffmpeg 이 없어 영상을 만들 수 없습니다"
                continue

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / f"{clip.id}.mp4"

                def record(context, page, clip=clip, out=out):
                    record_clip(context, page, app, clip, samples_dir, out, ffmpeg)
                    return True

                if attempt(name, record):
                    save_clip(app, clip, out, media_dir, now)
                    result["saved"].append(name)
                    log.info(f"[캡처] {name} 영상 저장")
    return result


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="내 앱 화면 자동 캡처·시연 영상")
    parser.add_argument("--app", help="apps.yaml 의 앱 id")
    parser.add_argument("--only", help="화면 또는 영상 id 하나만")
    parser.add_argument("--clips", action="store_true", help="영상을 모두 다시 만든다")
    args = parser.parse_args(argv)

    from settings import load_config
    cfg = load_config()
    if not cfg.allow_media:
        log.info("이 계정은 이미지·영상을 쓰지 않아(allow_media: false) 캡처하지 않습니다.")
        return 0
    if not APPS_PATH.exists():
        log.info(f"앱 목록이 없어 캡처하지 않습니다: {APPS_PATH}")
        return 0
    try:
        apps = load_apps()
    except CaptureError as e:
        log.error(f"apps.yaml 오류: {e}")
        return 1
    banned = banned_text_problems(apps, cfg.banned_words)
    if banned:
        log.error("apps.yaml 자막·끝 화면 문구에 금지어: " + " / ".join(banned))
        return 1
    if args.app:
        apps = tuple(a for a in apps if a.id == args.app)
        if not apps:
            log.error(f"apps.yaml 에 없는 앱입니다: {args.app}")
            return 1
    if args.only:
        apps = tuple(replace(a, shots=tuple(s for s in a.shots if s.id == args.only),
                             clips=tuple(c for c in a.clips if c.id == args.only)) for a in apps)
        if not any(a.shots or a.clips for a in apps):
            log.error(f"그런 화면·영상이 없습니다: {args.only}")
            return 1

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            result = capture(apps, browser=browser, clips=True if args.clips else None)
        finally:
            browser.close()
    log.info(f"결과: {json.dumps(result, ensure_ascii=False)}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
