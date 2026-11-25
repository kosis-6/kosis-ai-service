import pytest

from app.core.stats_db import StatsTableMissing
from app.services import indicators, lookup


def loaded() -> bool:
    try:
        return bool(lookup._years({"table": indicators.FOREIGN_TABLE, "item_id": "H001"}))
    except StatsTableMissing:
        return False


needs_table = pytest.mark.skipif(not loaded(), reason="DT_1B040A9C 적재 안 됨")


@pytest.mark.parametrize(
    "text, name",
    [
        ("부산의 등록된 외국인 비율", "등록외국인 비율"),
        ("등록 외국인 비중이 높은 곳", "등록외국인 비율"),
        ("대구 외국인 비율", "외국인주민 비율"),
        ("등록된 외국인 몇 명", "등록외국인 수"),
    ],
)
def test_registered_foreigner_words(text, name) -> None:
    assert [s[2] for s in indicators.find_in_text(text)] == [name]


def test_foreign_resident_aliases_skip_registered_words() -> None:
    assert "외국인주민 비율" not in [s[2] for s in indicators.find_in_text("등록된외국인비율")]
    assert "외국인주민 수" not in [s[2] for s in indicators.find_in_text("등록 외국인 수")]


@needs_table
def test_ratio_is_foreigners_over_population() -> None:
    year = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])[1]
    r = indicators.value("등록외국인 비율", "서울", year)
    assert r["status"] == "ok" and r["unit"] == "%"
    num = lookup._fetch(
        {"table": "DT_1B040A9C", "item_id": "H001", "filters": {"c2_id": "0", "c3_id": "B001"}},
        ["11"],
        year,
        year,
    )[(year, "11")]
    den = indicators._components(["11"], year, year, "계")[(year, "11")]["total"]
    assert r["value"] == pytest.approx(num / den * 100, abs=0.01)
    assert [s["tbl_id"] for s in r["extra_sources"]] == ["DT_1B040A9C"]


@needs_table
def test_sex_changes_the_numerator_cell() -> None:
    year = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])[1]
    both = indicators.value("등록외국인 비율", "경기", year)["value"]
    men = indicators.value("등록외국인 비율", "경기", year, sex="남")["value"]
    women = indicators.value("등록외국인 비율", "경기", year, sex="여")["value"]
    assert men != women and min(men, women) < both < max(men, women)


@needs_table
def test_years_follow_the_foreigner_table() -> None:
    lo, hi, monthly = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])
    assert monthly is False
    r = indicators.value("등록외국인 비율", "서울", lo - 1)
    assert r["status"] == "out_of_range"
    assert r["reason"].startswith("등록외국인(법무부) 자료가")
    assert r["available_years"] == [lo, hi]


@needs_table
def test_rank_inside_one_sido() -> None:
    year = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])[1]
    r = indicators.rank("등록외국인 비율", year, level="시군구", sido="부산", top=3)
    assert r["status"] == "ok" and len(r["ranking"]) == 3
    assert all(it["region"].startswith("부산광역시") for it in r["ranking"])


def test_value_answer_shows_numerator_and_denominator() -> None:
    from app.services import qa

    r = {
        "status": "ok",
        "indicator": "등록외국인 비율",
        "kind": "ratio",
        "unit": "%",
        "value": 2.5,
        "operands": [
            {"label": "등록외국인", "value": 250, "unit": "명"},
            {"label": "주민등록인구", "value": 10000, "unit": "명"},
        ],
    }
    assert qa._body(r) == ["- 계산: 등록외국인 250명 ÷ 주민등록인구 10,000명 × 100 = 2.5%"]
    r["operands"][1]["value"] = None
    assert qa._body(r) == []


def test_ratio_trend_shows_relative_change_apart_from_points() -> None:
    from app.services import qa

    r = {
        "region": "가시",
        "indicator": "등록외국인 비율",
        "unit": "%",
        "sex": "계",
        "series": [{"year": 2020, "value": 2.0}, {"year": 2025, "value": 3.0}],
        "change": {"from_year": 2020, "to_year": 2025, "diff": 1.0, "diff_unit": "%p",
                   "relative_rate": 50.0},
    }  # fmt: skip
    assert qa._body(r)[-1] == "- 2020년 → 2025년 전체 변화: +1.0%p (비율 자체는 50% 커짐)"


@needs_table
def test_trend_change_uses_unrounded_values() -> None:
    lo, hi, _ = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])
    t = indicators.trend("등록외국인 비율", "서울", hi - 4, hi)
    raw = [indicators.value("등록외국인 비율", "서울", y)["operands"] for y in (hi - 4, hi)]
    first, last = (a["value"] / b["value"] * 100 for a, b in raw)
    assert t["change"]["diff"] == round(last - first, 2)
    assert t["change"]["relative_rate"] == round((last - first) / first * 100, 1)


@needs_table
def test_dictionary_note_is_in_the_answer() -> None:
    year = indicators._years_of(indicators.INDICATORS["등록외국인 비율"])[1]
    note = indicators.specs()["등록외국인 비율"]["note"]
    assert note in indicators.value("등록외국인 비율", "서울", year)["notes"]
    assert note in indicators.rank("등록외국인 비율", year, top=1)["notes"]
