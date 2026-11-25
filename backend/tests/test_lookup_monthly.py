"""달 묶음 합 (lookup.month_sum): 월별 행(prd_se M)만 읽어 달마다 값을 더한다.
실제 DB 가 있어야 돈다"""

import pytest

from app.services import indicators, lookup

pytestmark = pytest.mark.needs_data


def test_month_sum_adds_monthly_rows_and_keeps_each_month() -> None:
    r = lookup.month_sum(indicators.spec("혼인 건수"), "부산광역시", 2025, 1, 6)
    assert r["status"] == "ok" and r["tbl_id"] == "DT_1B8000G"
    assert [m["region"] for m in r["members"]] == [f"{m}월" for m in range(1, 7)]
    assert r["value"] == sum(m["value"] for m in r["members"]) and r["months"] == [1, 6]
    assert any("1~6월 합계" in n for n in r["notes"])


def test_month_sum_uses_sigungu_table_and_reports_range() -> None:
    sp = indicators.spec("사망자 수")
    gu = lookup.month_sum(sp, "서울특별시 강남구", 2025, 1, 3)
    assert gu["status"] == "ok" and gu["tbl_id"] == "DT_1B82A01"
    assert lookup.month_sum(sp, "전국", 2031, 1, 3)["status"] == "out_of_range"
    assert lookup.month_sum(indicators.spec("총인구"), "전국", 2025, 1, 3) is None  # 월별 출처 없음
