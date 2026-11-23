"""그 해에 없던 지역을 말없이 빼서 다른 지역으로 답하던 문제 (2026년 광주 → 경기 광주시)"""

import pytest

from app.services import indicators

pytestmark = pytest.mark.needs_data  # regions 테이블을 읽음 (CI에는 DB 없음)


def test_gwangju_2026_still_asks():
    r = indicators.resolve_region("광주", year=2026, sigungu=True)
    assert r["status"] == "ambiguous_region"
    assert len(r["candidates"]) == 2


def test_gwangju_2025_asks_as_before():
    r = indicators.resolve_region("광주", year=2025, sigungu=True)
    assert r["status"] == "ambiguous_region"


def test_gyeonggi_gwangju_2026_is_direct():
    r = indicators.resolve_region("경기 광주", year=2026, sigungu=True)
    assert r["status"] == "ok" and r["region_name"] == "광주시"
