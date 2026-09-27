"""threads_token.py 테스트. Meta API 는 가짜로 바꾼다."""
import json
import types as pytypes
from datetime import date

import pytest
from dotenv import dotenv_values

import settings
import threads_token


class FakeHttp:
    def __init__(self, exchange_ok=True, me_ok=True):
        self.exchange_ok, self.me_ok, self.calls = exchange_ok, me_ok, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/access_token"):
            status, data = (200, {"access_token": "LONG", "token_type": "bearer", "expires_in": 5184000}) if self.exchange_ok \
                else (400, {"error": {"message": "Invalid OAuth access token"}})
        else:
            status, data = (200, {"id": "17841400000000000", "username": "my_test"}) if self.me_ok \
                else (400, {"error": {"message": "Session has expired"}})
        body = json.dumps(data).encode()
        return pytypes.SimpleNamespace(status_code=status, content=body, json=lambda: data)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("THREADS_USER_ID=\nTHREADS_ACCESS_TOKEN=SHORT\nTHREADS_APP_SECRET=APPSECRET\n"
                    "THREADS_TOKEN_ISSUED_AT=\nDRY_RUN=true\n", encoding="utf-8")
    return path


def test_short_token_exchanged_and_env_filled(env_file):
    http = FakeHttp()
    notes = threads_token.run(http, dotenv_values(env_file), env_path=env_file, today=date(2026, 9, 28))
    values = dotenv_values(env_file)
    assert values["THREADS_ACCESS_TOKEN"] == "LONG"
    assert values["THREADS_USER_ID"] == "17841400000000000"
    assert values["THREADS_TOKEN_ISSUED_AT"] == "2026-09-28"
    assert values["DRY_RUN"] == "true"                          # 다른 값은 건드리지 않는다
    assert http.calls[0][1]["grant_type"] == "th_exchange_token"
    assert http.calls[1][1]["access_token"] == "LONG"            # 계정 확인은 장기 토큰으로
    assert "장기 토큰으로 바꿨습니다 (약 60일 유효)" in notes[0]
    assert not any("LONG" in n or "SHORT" in n or "APPSECRET" in n for n in notes)     # 값은 출력하지 않는다
    # 설정 로더가 새 값을 받아들인다 (규칙 1: 숫자 ID 하나)
    assert settings.load_secrets(dict(values)).threads_user_id == "17841400000000000"


def test_already_long_token_kept(env_file):
    notes = threads_token.run(FakeHttp(exchange_ok=False), dotenv_values(env_file), env_path=env_file, today=date(2026, 9, 28))
    assert dotenv_values(env_file)["THREADS_ACCESS_TOKEN"] == "SHORT"
    assert "그대로 씁니다" in notes[0]


def test_bad_token_changes_nothing(env_file):
    before = env_file.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="Session has expired"):
        threads_token.run(FakeHttp(exchange_ok=False, me_ok=False), dotenv_values(env_file), env_path=env_file)
    assert env_file.read_text(encoding="utf-8") == before


def test_empty_token_rejected(env_file):
    with pytest.raises(RuntimeError, match="비어 있습니다"):
        threads_token.run(FakeHttp(), {"THREADS_ACCESS_TOKEN": ""}, env_path=env_file)
