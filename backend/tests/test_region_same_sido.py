import pytest

from app.core.stats_db import StatsTableMissing
from app.services import indicators


def regions_loaded() -> bool:
    try:
        return indicators.resolve_region("서울")["status"] == "ok"
    except StatsTableMissing:
        return False


pytestmark = pytest.mark.skipif(not regions_loaded(), reason="regions 표 없음")


def test_city_inside_the_same_sido_answers_the_sido() -> None:
    r = indicators.resolve_region("제주", 2024, sigungu=True)
    assert r["status"] == "ok" and r["kind"] == "시도"
    assert "제주시만 보시려면 '제주시'로 질문해 주세요" in r["note"]


def test_city_in_another_sido_still_asks() -> None:
    r = indicators.resolve_region("광주", 2024, sigungu=True)
    assert r["status"] == "ambiguous_region"
    assert set(r["candidates"]) == {"광주광역시", "경기도 광주시"}


def test_full_city_name_is_the_city() -> None:
    r = indicators.resolve_region("제주시", 2024, sigungu=True)
    assert r["status"] == "ok" and r["kind"] == "시군구"
