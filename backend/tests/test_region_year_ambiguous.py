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


def test_same_name_other_place_stays_in_candidates():
    # 인천 동구(2025년까지)는 부산·대구 동구의 옛 코드가 아니다 → 2026년에도 후보에 남는다
    r = indicators.resolve_region("동구", year=2026)
    assert r["status"] == "ambiguous_region"
    assert any("인천" in c for c in r["candidates"]), r["candidates"]


def test_moved_to_other_sido_is_same_place():
    # 군위군: 2023년 경북(47720) → 대구(27720) 편입. 개편 기록으로 이어진 같은 지역
    r = indicators.resolve_region("군위군", year=2024)
    assert r["status"] == "ok" and r["region_code"] == "27720"


def test_old_code_with_successor_is_dropped_one_by_one():
    # 2026년 동구: 광주 동구(29110)는 전남광주 동구(12210)로 이어져 빠지고, 인천 동구는 남는다
    r = indicators.resolve_region("동구", year=2026)
    cands = r["candidates"]
    assert any("전남광주" in c for c in cands), cands
    assert not any(c.startswith("광주광역시") for c in cands), cands
    assert any("인천" in c for c in cands), cands
