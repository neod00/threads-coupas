"""
요즘 검색이 느는 물건 고르기 (2026-10-25). 네이버 데이터랩 통합검색어 트렌드 API 로 후보 키워드의 최근 검색량을 비교한다.
LLM 을 쓰지 않는다. 네이버 API 는 무료(하루 1,000회)이고, 후보 32개면 하루 8번 부른다.

  - 후보: config.yaml 의 talk_topics 앞부분 ("식기세척기, 살까 말까" → "식기세척기")
  - 한 번에 5개까지만 비교되고 값이 요청마다 0~100 으로 다시 맞춰지므로, 모든 요청에 같은 기준 키워드(첫 후보)를 넣어 크기를 맞춘다
  - 점수 = 최근 7일 평균 × (1 + 앞선 21일 대비 증가율, -50%~+200% 로 자름)
  - 일일 요약의 "오늘 둘러볼 검색어"에 위 3개를 쓴다 (사람이 직접 둘러보고 답글을 단다 — 자동 답글은 없다, 규칙 6)

API (2026-07-31 부터 신규 신청은 NAVER API HUB, 네이버 클라우드 콘솔에서):
  POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search
  headers X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY, body {startDate, endDate, timeUnit: "date", keywordGroups: [{groupName, keywords}]}
  예전 개발자센터 키(openapi.naver.com/v1/datalab/search, X-Naver-Client-Id/Secret)는 legacy=True 로 쓴다.
  (2026-10-25 2차 자료로 확인. 첫 실제 호출로 응답 형식을 확인할 것)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Sequence

API = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
LEGACY_API = "https://openapi.naver.com/v1/datalab/search"
GROUPS_PER_REQUEST = 5          # API 한도
RECENT_DAYS = 7
BASE_DAYS = 21
MAX_KEYWORD_CHARS = 20

FetchFn = Callable[[Sequence[str], date, date], dict]      # (키워드들, 시작일, 끝일) -> {키워드: [일별 값...]}


def candidates_from(talk_topics: Sequence[str]) -> list[str]:
    out = []
    for topic in talk_topics:
        kw = topic.split(",")[0].strip()
        if kw and len(kw) <= MAX_KEYWORD_CHARS and kw not in out:
            out.append(kw)
    return out


def naver_fetcher(client_id: str, client_secret: str, http=None, timeout: float = 20, legacy: bool = False) -> FetchFn:
    if http is None:
        import requests as http

    def fetch(keywords: Sequence[str], start: date, end: date) -> dict:
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "date",
                "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords]}
        if legacy:
            url, headers = LEGACY_API, {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
        else:
            url, headers = API, {"X-NCP-APIGW-API-KEY-ID": client_id, "X-NCP-APIGW-API-KEY": client_secret}
        resp = http.post(url, json=body, timeout=timeout, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"네이버 데이터랩 오류 HTTP {resp.status_code}: {resp.text[:200]}")
        by_day = {}
        for result in resp.json().get("results", []):
            by_day[result["title"]] = {d["period"]: float(d["ratio"]) for d in result.get("data", [])}
        days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        return {k: [by_day.get(k, {}).get(d, 0.0) for d in days] for k in keywords}   # 검색이 없는 날은 빠져서 온다
    return fetch


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def trend_scores(candidates: Sequence[str], fetch: FetchFn, today: date) -> list[dict]:
    """[{keyword, recent, change, score}] 점수 높은 순. 데이터랩은 하루 늦게 채워지므로 어제까지 본다."""
    if not candidates:
        return []
    end = today - timedelta(days=1)
    start = end - timedelta(days=RECENT_DAYS + BASE_DAYS - 1)
    anchor, rest = candidates[0], list(candidates[1:])
    series, anchor_ref = {}, None
    batches = [rest[i:i + GROUPS_PER_REQUEST - 1] for i in range(0, len(rest), GROUPS_PER_REQUEST - 1)] or [[]]
    for batch in batches:
        got = fetch([anchor, *batch], start, end)
        anchor_sum = sum(got.get(anchor, []))
        if anchor_ref is None:
            anchor_ref = anchor_sum
            series[anchor] = got.get(anchor, [])
        scale = anchor_ref / anchor_sum if anchor_sum else 1.0
        for k in batch:
            series[k] = [v * scale for v in got.get(k, [])]
    out = []
    for k in candidates:
        values = series.get(k) or []
        recent, base = _mean(values[-RECENT_DAYS:]), _mean(values[:-RECENT_DAYS])
        change = (recent - base) / base if base else (1.0 if recent else 0.0)
        out.append({"keyword": k, "recent": recent, "change": change,
                    "score": recent * (1 + max(-0.5, min(2.0, change)))})
    return sorted(out, key=lambda r: (-r["score"], r["keyword"]))


def hot_keywords(talk_topics: Sequence[str], fetch: FetchFn, today: date, n: int = 3) -> list[dict]:
    """검색량이 중앙값 이상인 물건 중 늘어나는 순으로 n 개. 모자라면 점수 순으로 채운다.
    (점수 순만 쓰면 검색량이 큰 물건이 줄어드는 중이어도 맨 앞에 온다)"""
    rows = [r for r in trend_scores(candidates_from(talk_topics), fetch, today) if r["recent"] > 0]
    if not rows:
        return []
    recents = sorted(r["recent"] for r in rows)
    median = recents[(len(recents) - 1) // 2]        # 짝수 개면 아래쪽 중앙값
    popular = sorted((r for r in rows if r["recent"] >= median), key=lambda r: (-r["change"], r["keyword"]))[:n]
    rest = [r for r in rows if r not in popular]
    return (popular + rest)[:n]


def describe(rows: Sequence[dict]) -> str:
    """'식기세척기(+35%), 건조기(-5%)' 형식."""
    return ", ".join(f"{r['keyword']}({r['change'] * 100:+.0f}%)" for r in rows)
