"""여러 테스트가 함께 쓰는 고정 데이터. 요금표·상품은 테스트용 값이다 (실제 한전 요금·쿠팡 상품 아님)."""
import copy

import pytest

import calculator
import settings
from products import Product

TARIFF_RAW = {
    "effective_date": "2026-10-01",
    "season": "winter",
    "tiers": [
        {"upto_kwh": 200, "rate_per_kwh": 120.0},
        {"upto_kwh": 400, "rate_per_kwh": 214.6},
        {"upto_kwh": None, "rate_per_kwh": 307.3},
    ],
    "surcharge_multiplier": 1.137,
    "assumed_tier_for_posts": 2,
}

PRODUCTS = [
    Product("h1", "A사 전기히터", "heater", 2000, 4, "https://link.coupang.com/a/aaa111", "방 하나 데우는 용도, 저전력 모드 1000W 있음"),
    Product("h2", "B사 미니히터", "heater", 800, 4, "https://link.coupang.com/a/bbb222", "책상 밑에 두는 소형"),
    Product("m1", "C사 전기요", "blanket", 55, 8, "https://link.coupang.com/a/ccc333", "싱글 사이즈"),
]


@pytest.fixture
def tariff():
    return calculator.parse_tariff(copy.deepcopy(TARIFF_RAW))


@pytest.fixture
def cfg():
    return settings.load_config()


@pytest.fixture
def products():
    return list(PRODUCTS)


CALC_HOOK = "2,000W 히터 하루 4시간이면 한 달 240kWh"
CALC_BODY = (
    "누진 2단계 가정, 기본요금·감면 제외 추정으로 약 5만 8,600원이 추가로 나옵니다.\n\n"
    "같은 시간 1,000W 저전력 모드로 돌리면 사용량이 줄어듭니다.\n"
    "작은 방이라면 저전력 모드부터 써보는 게 낫습니다."
)


@pytest.fixture
def make_post(cfg, tariff):
    """가드를 통과하는 calc 글 행을 만든다. 필요한 필드만 덮어쓴다."""
    import uuid

    import generator

    counter = iter(range(10_000))

    def make(**overrides):
        post = {
            "id": str(uuid.uuid4()),
            "type": "calc",
            "product_ids": ["h1"],
            "text": f"{cfg.disclosure_text}\n{CALC_HOOK}\n\n{CALC_BODY}",
            "reply_text": None, "has_link": True, "link_url": PRODUCTS[0].link, "variant": "link_in_body",
            "topic_tag": None,
            "calc_payload": generator.build_payload("calc", [PRODUCTS[0]], tariff),
            "hook_candidates": [
                {"style": "question", "text": "2,000W 히터 한 달 전기료는 얼마일까요?"},
                {"style": "result", "text": CALC_HOOK},
                {"style": "compare", "text": "히터 한 달 요금 차이는?"},
            ],
            "hook_style": "result",
            "status": "approved",
            "guard_log": {"attempts": []},
            "review_result": {"status": "approved", "items": {}, "errors": []},
            "review_model": "claude-opus-5", "review_attempts": 1,
            "scheduled_at": None, "published_at": None,
            "threads_media_id": None, "threads_reply_id": None,
            "created_at": f"2026-11-02T10:{next(counter) % 60:02d}:00+00:00",
        }
        post.update(overrides)
        return post

    return make
