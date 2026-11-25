"""두 칸을 나눈 사전 지표(den_item_id)의 답에 분자 · 분모를 보여 준다 (11/25, 1인 가구 비율)"""

import pytest

from app.services import indicators, lookup

pytestmark = pytest.mark.needs_data


def test_divided_lookup_value_carries_operands() -> None:
    res = lookup.value(indicators.spec("1인 가구 비율"), "전국", 2020)
    assert res["status"] == "ok" and res["unit"] == "%" and res["kind"] == "ratio"
    a, b = res["operands"]
    assert (a["label"], a["unit"]) == ("1인 가구", "가구") and (b["label"], b["unit"]) == (
        "일반가구",
        "가구",
    )
    assert (
        isinstance(a["value"], int) and isinstance(b["value"], int) and 0 < a["value"] < b["value"]
    )
    assert res["value"] == round(a["value"] / b["value"] * 100, 2)


def test_plain_lookup_value_has_no_operands() -> None:
    res = lookup.value(indicators.spec("1인 가구 수"), "전국", 2020)
    assert res["status"] == "ok" and "operands" not in res
