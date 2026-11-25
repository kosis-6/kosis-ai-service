import pytest

from app.services import indicators


def _r(code, name, sido_code, sido_name, valid_from=None, valid_to=None):
    return {
        "region_code": code,
        "region_name": name,
        "kind": "시군구",
        "sido_code": sido_code,
        "sido_name": sido_name,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


REGIONS = [
    _r("29110", "동구", "29", "광주광역시", valid_to=2025),
    _r("12210", "동구", "12", "전남광주통합특별시", valid_from=2026),
    _r("26110", "동구", "26", "부산광역시"),
    _r("27140", "동구", "27", "대구광역시"),
    _r("28140", "동구", "28", "인천광역시", valid_to=2025),
    _r("46110", "목포시", "46", "전라남도", valid_to=2025),
    _r("12110", "목포시", "12", "전남광주통합특별시", valid_from=2026),
]
CHANGES = [
    {
        "year": 2026,
        "description": "광주광역시와 전라남도가 전남광주통합특별시로 통합 (2026-07-01 출범)",
        "region_codes": "29,46,12,29110,46110,12110,12210",
    },
]


def _valid(r, year):
    return (r["valid_from"] is None or r["valid_from"] <= year) and (
        r["valid_to"] is None or r["valid_to"] >= year
    )


def _fake_rows(sql, params=None):
    if "region_changes" in sql:
        return CHANGES
    if "from regions" in sql and params:
        name, code, year, _ = params
        return [
            r
            for r in REGIONS
            if r["region_name"] == name and r["region_code"] != code and _valid(r, year)
        ]
    return []


@pytest.fixture(autouse=True)
def fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_rows", _fake_rows)


def _get(code):
    return next(r for r in REGIONS if r["region_code"] == code)


def test_valid_year_unchanged() -> None:
    reg = _get("29110")
    assert indicators.in_year(reg, 2024) is reg


def test_unique_name_follows_new_code() -> None:
    assert indicators.in_year(_get("46110"), 2026)["region_code"] == "12110"


def test_same_name_many_follows_change_record() -> None:
    out = indicators.in_year(_get("29110"), 2026)
    assert out["region_code"] == "12210"
    assert "note" in out


def test_same_name_without_change_record_unchanged() -> None:
    reg = _get("28140")
    assert indicators.in_year(reg, 2026) is reg
