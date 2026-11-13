"""인구밀도 = 주민등록인구 ÷ 행정구역 면적 (면적: pipeline/load_area.py → mart.area)"""

import pytest

from app.core.stats_db import StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import indicators
from pipeline import load_area

REGIONS = [
    {"region_code": "11140", "region_name": "중구", "kind": "시군구", "sido_code": "11",
     "valid_from": None, "valid_to": None},
    {"region_code": "26110", "region_name": "중구", "kind": "시군구", "sido_code": "26",
     "valid_from": None, "valid_to": None},
    {"region_code": "47111", "region_name": "남구", "kind": "일반구", "sido_code": "47",
     "valid_from": None, "valid_to": None},
    {"region_code": "47720", "region_name": "군위군", "kind": "시군구", "sido_code": "47",
     "valid_from": None, "valid_to": 2022},
    {"region_code": "27720", "region_name": "군위군", "kind": "시군구", "sido_code": "27",
     "valid_from": 2023, "valid_to": None},
    {"region_code": "48110", "region_name": "창원시", "kind": "시군구", "sido_code": "48",
     "valid_from": None, "valid_to": 2009},
    {"region_code": "48120", "region_name": "창원시(통합)", "kind": "시군구", "sido_code": "48",
     "valid_from": 2010, "valid_to": None},
    {"region_code": "28170", "region_name": "남구", "kind": "시군구", "sido_code": "28",
     "valid_from": None, "valid_to": 2017},
    {"region_code": "28177", "region_name": "미추홀구", "kind": "시군구", "sido_code": "28",
     "valid_from": 2018, "valid_to": None},
]  # fmt: skip


def row(year, sido, name, km2):
    return {"year": year, "sido": sido, "name": name, "area_m2": km2 * 1_000_000}


def test_area_rows_are_matched_by_sido_and_name() -> None:
    area, missed = load_area.match(
        [
            row(2025, "전국", "전국", 100_472.4),
            row(2025, "서울", "서울", 605.2),
            row(2025, "서울", "중구", 10.0),  # 같은 이름(부산 중구)과 시도로 구분
            row(2025, "부산", "중구", 2.8),
            row(2011, "울산", "합계", 1_060.0),  # 어떤 해는 시도 행 이름이 '합계'
            row(2025, "경북", "포항시남구", 394.1),  # 일반구: 앞의 시 이름을 뗀다
            row(2020, "경북", "군위군", 614.3),  # 그해 있던 지역 (2023년 대구 편입 전)
            row(2025, "대구", "군위군", 614.3),
            row(2015, "경남", "창원시(계)", 747.8),  # '(계)'·'(통합)' 꼬리는 뗀다
            row(2012, "인천", "미추홀구", 24.8),  # 2012년의 '미추홀구'는 당시 남구
            row(2020, "세종특별자치시", "세종특별자치시", 464.9),  # 세종은 시도이자 시군구
            row(2025, "서울", "없는구", 1.0),
        ],
        REGIONS,
    )
    assert area[(2025, "00")] == pytest.approx(100_472.4)
    assert area[(2025, "11")] == pytest.approx(605.2)
    assert area[(2025, "11140")] == pytest.approx(10.0)
    assert area[(2025, "26110")] == pytest.approx(2.8)
    assert area[(2011, "31")] == pytest.approx(1_060.0)
    assert area[(2025, "47111")] == pytest.approx(394.1)
    assert (2020, "47720") in area and (2020, "27720") not in area
    assert (2025, "27720") in area
    assert (2015, "48120") in area
    assert (2012, "28170") in area
    assert area[(2020, "36")] == area[(2020, "36110")] == pytest.approx(464.9)
    assert missed == [(2025, "서울", "없는구")]


def area_loaded() -> bool:
    try:
        return bool(_rows("select 1 from area limit 1"))
    except StatsTableMissing:
        return False


needs_area = pytest.mark.skipif(
    not area_loaded(), reason="mart.area 없음 (python -m pipeline.load_area)"
)


@needs_area
def test_density_is_population_over_area() -> None:
    r = indicators.value("인구밀도", "서울", 2025)
    assert r["status"] == "ok" and r["unit"] == "명/㎢"
    # area_km2는 소수 둘째 자리로 줄여 보여 줘서 다시 나누면 조금 다르다
    assert r["value"] == pytest.approx(r["population"] / r["area_km2"], rel=1e-4)
    assert [s["tbl_id"] for s in r["extra_sources"]] == ["DT_MLTM_2300"]
    # 별칭
    assert indicators.value("인구 밀도", "서울", 2025)["value"] == r["value"]
    assert indicators.canonical_name("빽빽") == "인구밀도"


@needs_area
def test_density_trend_compare_rank() -> None:
    t = indicators.trend("인구밀도", "경기", 2015, 2025)
    assert t["change"]["diff_unit"] == "명/㎢" and "rate" in t["change"]
    c = indicators.compare("인구밀도", ["서울", "강원"], 2025)
    assert c["highest"] == "서울특별시" and c["gap_unit"] == "명/㎢"
    top = indicators.rank("인구밀도", 2025, "시군구", top=3)
    assert top["status"] == "ok" and top["total_regions"] > 200
    assert all(x["region"].startswith("서울특별시") for x in top["ranking"])


def test_density_without_area_table(monkeypatch: pytest.MonkeyPatch) -> None:
    real = indicators._rows

    def no_area(sql, params=()):
        if "from area" in sql:
            raise StatsTableMissing("area 없음")
        return real(sql, params)

    monkeypatch.setattr(indicators, "_rows", no_area)
    r = indicators.value("인구밀도", "서울", 2025)
    assert r["status"] == "missing_data" and "면적" in r["reason"]
    assert indicators.rank("인구밀도", 2025)["status"] == "missing_data"
    assert indicators.value("고령인구 비율", "서울", 2025)["status"] == "ok"  # 다른 지표는 그대로


def test_annual_year_has_no_monthly_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """연간 자료가 있는 해에는 최신 달 기준 안내를 붙이지 않는다 (KOSIS에 묻지도 않는다)"""
    calls = []
    monkeypatch.setattr(
        indicators, "_month_values", lambda codes, sex, year: calls.append(year) or {}
    )
    hi = indicators._data_years()[1]
    r = indicators.value("고령인구 비율", "서울", hi - 5)
    assert r["status"] == "ok" and "as_of" not in r and calls == []
