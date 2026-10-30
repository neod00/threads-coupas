"""
앱 홍보 계정의 글감 (2026-10-30 사용자 결정, PLAN 1-2). config content: app 인 계정(openbrain)만 쓴다.

data/apps.yaml 의 앱마다 info 블록(사람이 적은 사실)이 있어야 한다. 글의 모든 사실·숫자는 여기서만 나온다.
  one_liner, audience, problem, how, facts, limits, link, launch_media, features, updates, questions

글 유형 (하루 배분은 plan_day)
  launch  : 앱마다 한 번. 불편 → 만든 이유 → 작동 원리 → 무료·설치 여부 → 솔직한 한계 → 피드백 요청. 시연 영상 + 캡처
  feature : 기능 하나. 그 기능 캡처 1장, 같은 앱의 기능 글 VIDEO_EVERY 번에 한 번은 시연 영상. 앱을 소개(launch)한 뒤에만
  devlog  : info.updates 에 사람이 적은 '실제로 고친 것'만. 적힌 게 없으면 만들지 않는다 (지어낸 개발 이야기 금지)
  ask     : info.questions 의 질문 하나. 링크·이미지 없음
launch·feature 는 링크 글(링크는 첫 답글, 첫 줄은 disclosure_text), devlog·ask 는 링크 없는 글이다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

import media
import rules
from settings import ACCOUNT_DIR, Config

APPS_PATH = ACCOUNT_DIR / "data" / "apps.yaml"
LINK_TYPES = ("launch", "feature")
NO_LINK_TYPES = ("devlog", "ask")
VIDEO_EVERY = 5             # 기능 글 5개 중 1개는 시연 영상 (2026-10-30 계획)
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
MAX_FACT_CHARS = 120


class AppInfoError(ValueError):
    pass


@dataclass(frozen=True)
class Feature:
    id: str
    name: str
    desc: str
    media: str | None = None        # media.json 의 그림 id
    clip: str | None = None         # media.json 의 영상 id


@dataclass(frozen=True)
class Update:
    id: str
    date: str
    note: str
    media: str | None = None


@dataclass(frozen=True)
class AppInfo:
    id: str
    name: str
    link: str
    hosts: frozenset[str]
    one_liner: str
    audience: str
    problem: str
    how: str
    facts: tuple[str, ...]
    limits: tuple[str, ...]
    launch_media: tuple[str, ...]
    features: tuple[Feature, ...]
    updates: tuple[Update, ...]
    questions: tuple[str, ...]


# ── 읽기 ───────────────────────────────────────────────────────────────────

def _text(value, where: str, limit: int = MAX_FACT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise AppInfoError(f"{where}: 1~{limit}자 문장이어야 합니다: {value!r}")
    return value.strip()


def _texts(value, where: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        raise AppInfoError(f"{where}: 문장 목록이어야 합니다")
    return tuple(_text(v, where) for v in value)


def _id(value, seen: set, where: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or value in seen:
        raise AppInfoError(f"{where}: id 는 겹치지 않는 소문자·숫자·하이픈이어야 합니다: {value!r}")
    seen.add(value)
    return value


def parse_app(item: dict) -> AppInfo | None:
    """apps.yaml 의 앱 항목 하나. info 가 없는 앱(캡처만 하는 앱)은 None."""
    info = item.get("info")
    if info is None:
        return None
    app_id, where = item["id"], f"apps.yaml {item['id']}.info"
    keys = {"one_liner", "audience", "problem", "how", "facts", "limits", "link", "launch_media", "features",
            "updates", "questions"}
    if not isinstance(info, dict) or set(info) - keys or not {"one_liner", "problem", "facts", "link", "features"} <= set(info):
        raise AppInfoError(f"{where}: one_liner, problem, facts, link, features 가 있어야 하고 모르는 키가 없어야 합니다 "
                           f"(쓸 수 있는 키: {', '.join(sorted(keys))})")
    link = urlparse(str(info["link"]))
    hosts = {urlparse(str(item["url"])).hostname or "", *(h.lower() for h in item.get("allowed_hosts") or [])}
    if link.scheme != "https" or (link.hostname or "").lower() not in hosts:
        raise AppInfoError(f"{where}: link 는 이 앱 주소({', '.join(sorted(hosts))})의 https 주소여야 합니다: {info['link']!r}")

    features, ids = [], set()
    for f in info["features"] or []:
        if not isinstance(f, dict) or set(f) - {"id", "name", "desc", "media", "clip"} or not {"id", "name", "desc"} <= set(f):
            raise AppInfoError(f"{where}.features: id, name, desc (와 선택 media, clip) 이어야 합니다: {f!r}")
        features.append(Feature(_id(f["id"], ids, f"{where}.features"), _text(f["name"], f"{where}.features.name", 30),
                                _text(f["desc"], f"{where}.features.desc", 200), f.get("media"), f.get("clip")))
    if not features:
        raise AppInfoError(f"{where}: 기능(features)이 하나는 있어야 합니다")

    updates, uids = [], set()
    for u in info.get("updates") or []:
        if not isinstance(u, dict) or set(u) - {"id", "date", "note", "media"} or not {"id", "date", "note"} <= set(u):
            raise AppInfoError(f"{where}.updates: id, date, note (와 선택 media) 이어야 합니다: {u!r}")
        try:
            day = date.fromisoformat(str(u["date"])).isoformat()
        except ValueError:
            raise AppInfoError(f"{where}.updates: date 는 YYYY-MM-DD 여야 합니다: {u['date']!r}") from None
        updates.append(Update(_id(u["id"], uids, f"{where}.updates"), day, _text(u["note"], f"{where}.updates.note", 200),
                              u.get("media")))

    return AppInfo(
        id=app_id, name=str(item["name"]), link=str(info["link"]), hosts=frozenset(h.lower() for h in hosts if h),
        one_liner=_text(info["one_liner"], f"{where}.one_liner"),
        audience=_text(info.get("audience") or "누구나", f"{where}.audience"),
        problem=_text(info["problem"], f"{where}.problem", 200),
        how=_text(info.get("how") or info["one_liner"], f"{where}.how", 200),
        facts=_texts(info["facts"], f"{where}.facts", required=True),
        limits=_texts(info.get("limits"), f"{where}.limits"),
        launch_media=tuple(info.get("launch_media") or ()),
        features=tuple(features), updates=tuple(updates),
        questions=_texts(info.get("questions"), f"{where}.questions"),
    )


def load_apps(path: Path | None = None) -> tuple[AppInfo, ...]:
    raw = yaml.safe_load((path or APPS_PATH).read_text(encoding="utf-8")) or {}
    apps = tuple(a for a in (parse_app(item) for item in raw.get("apps") or []) if a)
    if not apps:
        raise AppInfoError("apps.yaml 에 info 가 있는 앱이 없습니다")
    return apps


def link_hosts(apps) -> frozenset[str]:
    return frozenset(h for a in apps for h in a.hosts)


def make_link_checker(apps):
    """앱 계정의 링크 확인: https + apps.yaml 에 등록한 내 앱 호스트. HTTP 요청은 보내지 않는다."""
    hosts = link_hosts(apps)

    def check(url: str) -> str | None:
        parsed = urlparse((url or "").strip())
        if parsed.scheme != "https":
            return f"링크가 https 가 아닙니다: {url}"
        if (parsed.hostname or "").lower() not in hosts:
            return f"링크가 apps.yaml 에 등록한 내 앱 주소({', '.join(sorted(hosts))})가 아닙니다: {parsed.hostname}"
        return None
    return check


# ── 미디어 ─────────────────────────────────────────────────────────────────

def media_index(app_id: str, media_dir: Path | None = None) -> dict:
    """data/media/<앱>/media.json → {id: {file, kind, feature}}. 파일이 실제로 있는 것만."""
    folder = (media_dir or media.MEDIA_DIR) / app_id
    path = folder / "media.json"
    if not path.exists():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8"))
    base = media_dir or media.MEDIA_DIR
    return {k: v for k, v in entries.items() if (base / v["file"]).is_file()}


def _files(ids, index: dict) -> list[str]:
    return [index[i]["file"] for i in ids if i and i in index]


def _features_since_video(app_id: str, used: list[dict]) -> int:
    """이 앱의 기능 글 중 마지막 영상 글 뒤로 몇 개가 나갔는지 (영상 글이 없으면 전체 개수)."""
    count = 0
    for post in reversed(used):
        if post.get("type") != "feature" or app_id not in (post.get("product_ids") or []):
            continue
        if any(str(m).endswith(".mp4") for m in post.get("media") or []):
            return count
        count += 1
    return count


# ── 주제 고르기 ────────────────────────────────────────────────────────────

def launched(app: AppInfo, used) -> bool:
    return any(p.get("type") == "launch" and app.id in (p.get("product_ids") or []) for p in used)


def subjects(apps, post_type: str, used) -> list[dict]:
    """고를 수 있는 주제: {key, app, feature?, update?, question?}."""
    out = []
    for app in apps:
        if post_type == "launch" and not launched(app, used):
            out.append({"key": f"launch|{app.id}", "app": app})
        elif post_type == "feature" and launched(app, used):
            out += [{"key": f"feature|{app.id}|{f.id}", "app": app, "feature": f} for f in app.features]
        elif post_type == "devlog":
            done = {subject_key(p) for p in used}
            out += [{"key": f"devlog|{app.id}|{u.id}", "app": app, "update": u} for u in app.updates
                    if f"devlog|{app.id}|{u.id}" not in done]
        elif post_type == "ask":
            out += [{"key": f"ask|{app.id}|{i}", "app": app, "question": q} for i, q in enumerate(app.questions)]
    return out


def subject_key(post: dict) -> str | None:
    return (post.get("calc_payload") or {}).get("subject")


def pick_subject(apps, post_type: str, day: date, used, offset: int = 0) -> dict | None:
    """가장 오래 안 쓴 주제. 한 번도 안 쓴 주제가 먼저, 동률이면 날짜로 돌린다. 고를 게 없으면 None."""
    candidates = subjects(apps, post_type, used)
    if not candidates:
        return None
    last = {}
    for i, post in enumerate(used):
        if key := subject_key(post):
            last[key] = i
    best = min(last.get(c["key"], -1) for c in candidates)
    pool = [c for c in candidates if last.get(c["key"], -1) == best]
    return pool[(day.toordinal() + offset) % len(pool)]


def build_payload(subject: dict, post_type: str) -> dict:
    """LLM·가드·검수자가 함께 보는 calc_payload. 글의 모든 사실·숫자는 여기에 있어야 한다."""
    app: AppInfo = subject["app"]
    payload = {"subject": subject["key"],
               "app": {"name": app.name, "one_liner": app.one_liner, "audience": app.audience,
                       "problem": app.problem, "how": app.how, "facts": list(app.facts), "limits": list(app.limits)}}
    if post_type == "launch":
        payload["features"] = [{"name": f.name, "desc": f.desc} for f in app.features]
    elif post_type == "feature":
        f: Feature = subject["feature"]
        payload["feature"] = {"name": f.name, "desc": f.desc}
    elif post_type == "devlog":
        u: Update = subject["update"]
        payload["update"] = {"date": u.date, "note": u.note}
    elif post_type == "ask":
        payload["question_topic"] = subject["question"]
        payload["choice_numbers"] = [1, 2, 3]
    return payload


def pick_media(subject: dict, post_type: str, used, cfg: Config, media_dir: Path | None = None) -> list[str]:
    """글에 붙일 data/media/ 파일 이름. allow_media 가 꺼져 있거나 파일이 없으면 빈 목록."""
    if not cfg.allow_media:
        return []
    app: AppInfo = subject["app"]
    index = media_index(app.id, media_dir)
    if post_type == "launch":
        return _files(app.launch_media, index)
    if post_type == "feature":
        f: Feature = subject["feature"]
        if f.clip and f.clip in index and _features_since_video(app.id, used) >= VIDEO_EVERY - 1:
            return _files([f.clip], index)
        return _files([f.media], index)
    if post_type == "devlog":
        return _files([subject["update"].media], index)
    return []


def review_rows(app: AppInfo) -> list[dict]:
    """검수자에게 넘기는 '연결된 앱' 행."""
    return [{"name": app.name, "link": app.link, "facts": list(app.facts),
             "features": [{"name": f.name, "desc": f.desc} for f in app.features]}]


# ── 하루 배분 ──────────────────────────────────────────────────────────────

def plan_day(posts, cfg: Config, day: date, apps, used) -> list[str]:
    """그날 새로 만들 글 유형 목록. 빈 슬롯 수만큼, 링크 글은 링크 한도까지만.
    링크 자리: 아직 소개하지 않은 앱이 있으면 launch(하루 하나), 나머지는 feature.
    링크 없는 자리: 아직 안 쓴 devlog 가 있으면 하루 하나, 나머지는 ask."""
    occupied = [(t, link) for t, link in rules.occupied(posts) if rules.kst_date(t) == day]
    free = max(0, min(cfg.max_posts_per_day, len(cfg.slots_kst)) - len(occupied))
    link_quota = max(0, rules.max_link_posts_per_day(cfg) - sum(link for _, link in occupied))
    n_links = min(free, link_quota)
    allowed = cfg.post_types
    links = []
    for _ in range(n_links):
        if "launch" in allowed and "launch" not in links and subjects(apps, "launch", used):
            links.append("launch")
        elif "feature" in allowed and (subjects(apps, "feature", used) or "launch" in links):
            links.append("feature")
    no_link = []
    for _ in range(free - n_links):
        if "devlog" in allowed and "devlog" not in no_link and subjects(apps, "devlog", used):
            no_link.append("devlog")
        elif "ask" in allowed and subjects(apps, "ask", used):
            no_link.append("ask")
    return links + no_link
