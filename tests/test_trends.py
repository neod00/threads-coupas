"""네이버 데이터랩으로 '요즘 검색이 느는 물건' 고르기. 네트워크 없이 가짜 응답으로 본다."""
import dataclasses
import json
import types
from datetime import date, datetime, timezone

import pytest

import digest
import settings
import trends
from store import MemoryStore

TODAY = date(2026, 10, 25)
DAYS = trends.RECENT_DAYS + trends.BASE_DAYS


def flat(v):
    return [v] * DAYS


def rising(base, recent):
    return [base] * trends.BASE_DAYS + [recent] * trends.RECENT_DAYS


def test_candidates_from_talk_topics():
    topics = ["식기세척기, 살까 말까", "건조기, 살까 말까", "식기세척기, 또", "아주 아주 긴 물건 이름이 스무 글자를 넘는 경우, 살까 말까"]
    assert trends.candidates_from(topics) == ["식기세척기", "건조기"]


def test_rising_keyword_ranks_first_and_batches_share_an_anchor():
    data = {"A": flat(50), "B": flat(50), "C": rising(10, 40), "D": flat(5), "E": flat(30), "F": rising(20, 60)}
    calls = []

    def fetch(keywords, start, end):
        calls.append(list(keywords))
        assert (end - start).days + 1 == DAYS and end == date(2026, 10, 24)
        # 요청마다 값을 다시 맞추는 것을 흉내 낸다: 두 번째 요청은 모든 값이 절반
        factor = 1 if len(calls) == 1 else 0.5
        return {k: [v * factor for v in data[k]] for k in keywords}

    rows = trends.trend_scores(list(data), fetch, TODAY)
    assert calls == [["A", "B", "C", "D", "E"], ["A", "F"]]               # 기준 A 가 모든 요청에 들어간다
    f = next(r for r in rows if r["keyword"] == "F")
    assert f["recent"] == pytest.approx(60) and f["change"] == pytest.approx(2.0)   # 절반이 된 요청도 기준으로 되돌린다
    assert [r["keyword"] for r in rows[:2]] == ["F", "C"]                  # 많이 늘어난 것이 위
    assert rows[-1]["keyword"] == "D"


def test_hot_keywords_skip_zero_and_describe():
    data = {"A": flat(10), "B": flat(0), "C": rising(10, 20)}
    rows = trends.hot_keywords(["A, 살까 말까", "B, 살까 말까", "C, 살까 말까"],
                               lambda ks, s, e: {k: data[k] for k in ks}, TODAY)
    assert [r["keyword"] for r in rows] == ["C", "A"]
    assert trends.describe(rows) == "C(+100%), A(+0%)"


def test_naver_fetcher_request_shape_and_missing_days():
    sent = {}

    def post(url, json=None, timeout=None, headers=None):
        sent.update(url=url, body=json, headers=headers)
        data = {"results": [{"title": "식기세척기", "keywords": ["식기세척기"],
                             "data": [{"period": "2026-10-23", "ratio": 80.5}, {"period": "2026-10-24", "ratio": 100}]}]}
        return types.SimpleNamespace(status_code=200, text="", json=lambda: data)

    fetch = trends.naver_fetcher("id-1", "secret-1", http=types.SimpleNamespace(post=post))
    got = fetch(["식기세척기"], date(2026, 10, 22), date(2026, 10, 24))
    assert sent["url"] == trends.API
    assert sent["headers"] == {"X-Naver-Client-Id": "id-1", "X-Naver-Client-Secret": "secret-1"}
    assert sent["body"] == {"startDate": "2026-10-22", "endDate": "2026-10-24", "timeUnit": "date",
                            "keywordGroups": [{"groupName": "식기세척기", "keywords": ["식기세척기"]}]}
    assert got == {"식기세척기": [0.0, 80.5, 100.0]}                       # 검색이 없는 날은 0


def test_naver_error_raises_without_secret_in_message():
    post = lambda *a, **k: types.SimpleNamespace(status_code=401, text="Authentication failed", json=lambda: {})
    with pytest.raises(RuntimeError, match="HTTP 401") as e:
        trends.naver_fetcher("id-1", "secret-1", http=types.SimpleNamespace(post=post))(["a"], TODAY, TODAY)
    assert "secret-1" not in str(e.value)


def test_digest_uses_hot_keywords_when_given(cfg):
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    hot = [{"keyword": "건조기", "change": 0.35}, {"keyword": "제습기", "change": -0.05}]
    text = digest.build_digest(MemoryStore(), cfg, now, dry_run=True, hot=hot)
    assert "오늘 둘러볼 검색어 (네이버 검색 상승순): 건조기(+35%), 제습기(-5%)" in text
    assert "네이버" not in digest.build_digest(MemoryStore(), cfg, now, dry_run=True)   # 없으면 기본 목록


def test_naver_keys_are_optional_secrets():
    s = settings.load_secrets({})
    assert "NAVER_CLIENT_ID" not in s.missing() and "NAVER_CLIENT_SECRET" not in s.missing()
    s = settings.load_secrets({"NAVER_CLIENT_SECRET": "x-SECRET"})
    assert "x-SECRET" not in repr(s)
