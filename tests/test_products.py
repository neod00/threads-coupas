"""products.csv 로더 테스트. 규칙 2(남의 후기 금지)의 입력 단계 검사 포함."""
import pytest

import products
from settings import ConfigError

ROW = {"product_id": "h1", "name": "A사 전기히터", "category": "heater", "power_w": "2000",
       "default_hours": "4", "link": "https://link.coupang.com/a/aaa111", "note": "방 하나 데우는 용도"}


def row(**changes):
    return {**ROW, **changes}


def test_valid_row():
    p = products.parse_products([row()])[0]
    assert (p.product_id, p.power_w, p.default_hours) == ("h1", 2000.0, 4.0)


@pytest.mark.parametrize("changes, message", [
    ({"link": "http://link.coupang.com/a/x"}, "간편링크"),
    ({"link": "https://www.coupang.com/vp/products/1"}, "간편링크"),
    ({"link": "https://evil.example/link.coupang.com"}, "간편링크"),
    ({"note": "구매자 리뷰가 좋음"}, "규칙 2"),
    ({"note": "써보니 따뜻합니다"}, "규칙 2"),
    ({"note": "우리 집 거실용"}, "규칙 2"),
    ({"note": "아이 방에 둬도 걱정 없음"}, "안전·건강"),
    ({"note": "전자파 적음"}, "안전·건강"),
    ({"note": "과열 방지 기능, 화재 걱정 적음"}, "안전·건강"),
    ({"note": "가" * 121}, "note 가 깁니다"),
    ({"power_w": "0"}, "power_w"),
    ({"power_w": "2kW"}, "power_w"),
    ({"default_hours": "25"}, "default_hours"),
    ({"category": "tv"}, "category"),
    ({"name": ""}, "name"),
])
def test_invalid_rows(changes, message):
    with pytest.raises(ConfigError, match=message):
        products.parse_products([row(**changes)])


def test_coupa_ng_short_link_ok():
    assert products.parse_products([row(link="https://coupa.ng/abc")])


def test_duplicate_ids_rejected():
    with pytest.raises(ConfigError, match="중복"):
        products.parse_products([row(), row()])


def test_csv_with_excel_bom(tmp_path):
    path = tmp_path / "products.csv"
    header = ",".join(products.COLUMNS)
    values = ",".join(ROW[c] for c in products.COLUMNS)
    path.write_text(f"{header}\n{values}\n", encoding="utf-8-sig")
    assert products.load_products(path)[0].name == "A사 전기히터"


def test_wrong_header_rejected(tmp_path):
    path = tmp_path / "products.csv"
    path.write_text("id,name\n1,x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="머리글"):
        products.load_products(path)


def test_empty_template_rejected(tmp_path):
    path = tmp_path / "products.csv"
    path.write_text(",".join(products.COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="상품이 없습니다"):
        products.load_products(path)
