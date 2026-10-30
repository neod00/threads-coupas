"""
코드 가드 (PLAN v2 Step 4). 로옥션 scripts/column_guard.py 의 숫자 검사를 옮겼다.

check_post(post, cfg) → 문제 목록 (빈 목록 = 통과). 문제마다 항목 코드 [G1]~[G9] 를 붙여
생성기 피드백·guard_log·주간 리포트의 항목별 실패 빈도에 그대로 쓴다.

  G1  전체 길이 max_chars(500) 이하, 이모지 없음 (본문·답글)
  G2  링크가 있으면 본문 첫 줄 == disclosure_text, 답글에 링크가 있으면 답글 첫 줄도 == disclosure_text (규칙 3)
  G3  본문·답글의 모든 숫자가 calc_payload(입력 데이터 포함)에 있음 (규칙 7)
  G4  금지어(banned_words) 없음
  G5  URL(본문·답글 텍스트 + 링크 첨부) 고유 개수 max_urls(1) 이하
  G6  calc·compare 는 assumption_text 를 그대로 포함
  G7  훅(링크 글 둘째 줄, tip 첫 줄) 30자 이하, calc·compare 훅은 calc_payload 숫자 1개 이상
  G8  금지 훅 문구(banned_hook_phrases)가 본문·답글 어디에도 없음
  G9  이미지·영상은 allow_media 계정의 data/media/ 파일만, JPEG·PNG 8MB·MP4 50MB 이하 20개 이하, 링크는 답글에만 (규칙 2, media.py)

column_guard.py 와 다른 점: 0~12 를 무조건 허용하지 않는다. PLAN 규칙 7 은 "입력 데이터와 계산 결과에 있는 값만"이므로
하루 사용시간·월 사용일수·누진 단계도 calc_payload 에 들어 있어야 허용된다. 사건번호 처리는 뺐다.

가드는 정해진 패턴을 확실하게 막는 1차 관문이다. 금지어를 피해 교묘하게 쓴 문장은 Step 4-B AI 검수자가 잡는다.
발행기(Step 6)도 발행 직전에 이 함수를 다시 실행한다.
"""
from __future__ import annotations

import re

import media
from settings import Config

HOOK_MAX_CHARS = 30
CALC_POST_TYPES = ("calc", "compare")           # 전기요금 계산 글: 가정 문구 필수 (G6)
LINK_POST_TYPES = ("calc", "compare", "pick", "bundle")   # 링크가 붙는 글
# 경험 표현. 사용자가 직접 쓴 경험 메모(calc_payload.product.experience)가 있는 글에서만 허용한다 (2026-10-16)
EXPERIENCE_WORDS = ("써봤", "사용해보니", "써보니", "우리 집")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# 이모지·그림 기호. 스레드는 이모지를 UTF-8 바이트로 세어 글자 수가 틀어진다.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"     # 그림 문자·이모티콘·기호 확장
    "\U0001F1E6-\U0001F1FF"     # 국기
    "☀-➿"             # 기타 기호·딩뱃 (☀ ❤ ✔)
    "⌀-⏿"             # ⌚ ⏰
    "⬀-⯿"             # ⭐ ⬆
    "〰〽㊗㊙"
    "️‍"              # 이모지 변형 선택자·결합 문자
    "]"
)


# ── 숫자 (column_guard.py) ───────────────────────────────────────────

def _norm(tok: str) -> str:
    """'4,700' → '4700', '38.50' → '38.5', '95.0' → '95', '08' → '8'."""
    tok = str(tok).replace(",", "").strip()
    if not tok:
        return tok
    if "." in tok:
        try:
            f = float(tok)
        except ValueError:
            return tok
        if f == int(f):
            return str(int(f))
        return f"{f:f}".rstrip("0").rstrip(".")
    try:
        return str(int(tok))
    except ValueError:
        return tok


def _add_number(allowed: set, n) -> None:
    """정수 n 과, 글에서 흔히 쓰는 환산값(만·천만·억, 억 뒤의 만 단위, 만 뒤의 원 단위, 반올림값)을 넣는다.
    예: 58600 → '5만 8,600원' 의 5 와 8600."""
    n = abs(int(n))
    allowed.add(str(n))
    if n < 10_000:
        return
    allowed.add(str(n // 10_000))
    allowed.add(str(round(n / 10_000)))
    allowed.add(str((n % 100_000_000) // 10_000))
    allowed.add(str(n % 10_000))
    allowed.add(str(n // 10_000_000))
    if n >= 100_000_000:
        allowed.add(str(n // 100_000_000))
        allowed.add(str(round(n / 100_000_000)))
        allowed.add(_norm(f"{n / 100_000_000:.1f}"))
        allowed.add(_norm(f"{n / 100_000_000:.2f}"))
        allowed.add(str((n % 100_000_000) // 1_000_000))
        allowed.add(str((n % 100_000_000) // 10_000_000))


def allowed_numbers(payload) -> set:
    """payload 안의 모든 숫자(정수·실수·문자열 속 숫자)와 그 환산값."""
    allowed: set = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int):
            _add_number(allowed, v)
        elif isinstance(v, float):
            allowed.add(_norm(str(abs(v))))
            allowed.add(str(int(abs(v))))
            allowed.add(str(round(abs(v))))
            _add_number(allowed, int(abs(v)))
        elif isinstance(v, str):
            s = re.sub(r"(?<=\d),(?=\d)", "", v)
            for tok in re.findall(r"\d+(?:\.\d+)?", s):
                allowed.add(_norm(tok))
                if "." in tok:
                    allowed.add(str(int(float(tok))))
                    allowed.add(str(round(float(tok))))
                else:
                    _add_number(allowed, int(tok))
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(k)
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)

    walk(payload)
    return allowed


def extract_numbers(text: str) -> set:
    """본문 숫자 토큰 (쉼표 제거, 소수는 한 토큰). URL 안의 숫자는 뺀다 (URL 은 G5 에서 따로 센다)."""
    t = URL_RE.sub(" ", text or "")
    t = re.sub(r"(?<=\d),(?=\d)", "", t)
    return {_norm(tok) for tok in re.findall(r"\d+(?:\.\d+)?", t)}


def _numkey(s: str):
    try:
        return (0, float(s))
    except ValueError:
        return (1, s)


# ── 검사 ─────────────────────────────────────────────────────────────

def _lines(text: str) -> list[str]:
    return (text or "").replace("\r\n", "\n").split("\n")


def hook_line(post: dict) -> str:
    """훅 위치의 문장: 본문 첫 줄. 예전 형식(본문 첫 줄이 대가성 문구)이면 둘째 줄."""
    lines = _lines(post.get("text"))
    index = 1 if lines and lines[0].lstrip().startswith("[광고]") else 0
    return lines[index].strip() if len(lines) > index else ""


def check_post(post: dict, cfg: Config) -> list[str]:
    """빈 리스트 = 통과. 각 항목은 LLM 에 그대로 돌려줄 수 있는 한국어 문제 설명."""
    problems: list[str] = []
    text = (post.get("text") or "").replace("\r\n", "\n")
    reply = (post.get("reply_text") or "").replace("\r\n", "\n")
    parts = {"본문": text, "답글": reply} if reply else {"본문": text}
    post_type = post.get("type")
    payload = post.get("calc_payload") or {}
    disclosure = cfg.disclosure_text

    if not text.strip():
        return ["[G1] 본문이 비어 있습니다"]

    # G1 길이·이모지
    for name, part in parts.items():
        if len(part) > cfg.limits.max_chars:
            problems.append(f"[G1] {name}이 깁니다: {len(part)}자 (최대 {cfg.limits.max_chars}자)")
        emojis = sorted(set(EMOJI_RE.findall(part)))
        if emojis:
            problems.append(f"[G1] {name}에 이모지가 있습니다: {' '.join(repr(e) for e in emojis)}")

    # G2 대가성 문구 (규칙 3)
    link_url = (post.get("link_url") or "").strip()
    text_urls, reply_urls = URL_RE.findall(text), URL_RE.findall(reply)
    link_in_reply = post.get("variant") == "link_in_reply"
    reply_has_link = bool(reply_urls) or (bool(link_url) and link_in_reply)
    has_link = bool(post.get("has_link") or link_url or text_urls or reply_urls)
    # 2026-10-10 결정: 링크와 대가성 문구는 첫 답글에 둔다. 링크가 있는 곳(본문이든 답글이든)의 첫 줄은 대가성 문구여야 한다.
    body_has_link = bool(text_urls) or (bool(link_url) and not link_in_reply)
    if body_has_link and _lines(text)[0] != disclosure:
        problems.append("[G2] 본문에 링크가 있는데 본문 첫 줄이 대가성 문구가 아닙니다")
    if has_link and link_in_reply and not reply.strip():
        problems.append("[G2] 링크 글인데 링크와 대가성 문구를 넣을 답글이 없습니다")
    elif reply_has_link and _lines(reply)[0] != disclosure:
        problems.append("[G2] 링크가 있는 답글인데 답글 첫 줄이 대가성 문구가 아닙니다")

    # G3 숫자 (규칙 7)
    allowed = allowed_numbers(payload)
    for name, part in parts.items():
        extra = sorted(extract_numbers(part) - allowed, key=_numkey)
        if extra:
            problems.append(f"[G3] {name}에 입력·계산 데이터에 없는 숫자: {', '.join(extra)} (데이터의 값만 쓰세요)")

    # G4 금지어 (경험 메모가 있는 글은 경험 표현만 허용)
    has_experience = bool(((payload.get("product") or {}) if isinstance(payload, dict) else {}).get("experience"))
    for name, part in parts.items():
        found = [w for w in cfg.banned_words if w in part and not (has_experience and w in EXPERIENCE_WORDS)]
        if found:
            problems.append(f"[G4] {name}에 금지어: {', '.join(found)}")

    # G5 URL 개수
    urls = {u.rstrip(".,") for u in text_urls + reply_urls}
    if link_url:
        urls.add(link_url)
    if len(urls) > cfg.limits.max_urls:
        problems.append(f"[G5] URL 이 {len(urls)}개입니다 (최대 {cfg.limits.max_urls}개, 본문·답글·링크 첨부 합계)")

    # G6 가정 문구
    if post_type in CALC_POST_TYPES:
        assumption = payload.get("assumption_text")
        if not assumption:
            problems.append("[G6] calc_payload 에 assumption_text 가 없습니다")
        elif assumption not in text:
            problems.append(f"[G6] 가정 문구가 본문에 그대로 없습니다: \"{assumption}\"")

    # G7 훅
    hook = hook_line(post)
    # 2026-10-10: 훅은 궁금증으로 시작해도 되므로 숫자를 강제하지 않는다 (숫자를 쓰면 G3 가 값을 검사한다)
    if not hook:
        problems.append("[G7] 훅 위치(첫 줄)가 비어 있습니다")
    elif len(hook) > HOOK_MAX_CHARS:
        problems.append(f"[G7] 훅이 깁니다: {len(hook)}자 (최대 {HOOK_MAX_CHARS}자): \"{hook}\"")

    # G8 금지 훅 문구
    for name, part in parts.items():
        found = [p for p in cfg.banned_hook_phrases if p in part]
        if found:
            problems.append(f"[G8] {name}에 금지 훅 문구: {', '.join(found)}")

    # G9 이미지·영상 (규칙 2, 2026-10-30): 허용된 계정의 자기 폴더 파일만
    problems += media.media_problems(post, cfg)

    return problems


def make_checker(cfg: Config):
    """generator.generate_post 의 checks 로 넘길 함수."""
    return lambda post: check_post(post, cfg)
