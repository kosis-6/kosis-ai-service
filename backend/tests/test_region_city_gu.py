import pytest

from app.services import indicators

pytestmark = pytest.mark.needs_data


@pytest.mark.parametrize(
    ("question", "year", "name", "code_prefix"),
    [
        ("부천 원미구", 2025, "원미구", "41192"),
        ("부천시 원미구", 2025, "원미구", "41192"),
        ("경기 부천 원미구", 2025, "원미구", "41192"),
        ("부천 원미구", 2015, "원미구", "41195"),
        ("수원 영통구", 2025, "영통구", "41117"),
        ("성남 분당", 2025, "분당구", "41135"),
        ("고양 일산동구", 2025, "일산동구", "4128"),
        ("창원 의창구", 2025, "의창구", "48121"),
        ("포항 남구", 2025, "남구", "4711"),
    ],
)
def test_city_and_gu(question, year, name, code_prefix):
    r = indicators.resolve_region(question, year=year)
    assert r["status"] == "ok", r
    assert r["region_name"] == name
    assert r["region_code"].startswith(code_prefix)


@pytest.mark.parametrize(
    ("question", "year", "code"),
    [
        ("창원시", 2025, "48120"),
        ("창원", 2025, "48120"),
        ("창원시", 2005, "48110"),
        ("창원시", None, "48120"),
        ("원미구", None, "41192"),
    ],
)
def test_same_place_old_and_new_code(question, year, code):
    r = indicators.resolve_region(question, year=year)
    assert r["status"] == "ok", r
    assert r["region_code"] == code


def test_sido_and_gu_still_works():
    r = indicators.resolve_region("서울 중구", year=2025)
    assert r["status"] == "ok" and r["region_code"].startswith("11")


@pytest.mark.parametrize(
    ("regions", "question", "expected"),
    [
        (["부천", "원미구"], "2025년 부천 원미구 인구", ["부천 원미구"]),
        (["부천시", "원미구"], "부천시 원미구 인구", ["부천시 원미구"]),
        (["수원", "영통구"], "수원 영통구 인구", ["수원 영통구"]),
        (["창원", "의창구"], "창원 의창구 인구", ["창원 의창구"]),
        (["경기", "부천", "원미구"], "경기 부천 원미구 인구", ["경기 부천 원미구"]),
        (["부천", "원미구"], "부천과 원미구 인구 비교", ["부천", "원미구"]),
        (["서울", "중구"], "서울 중구 인구", ["서울 중구"]),
        (["서울", "부산"], "서울과 부산 인구 비교", ["서울", "부산"]),
    ],
)
def test_merge_city_and_gu(regions, question, expected):
    from app.services import qa

    assert qa._merge_regions(regions, question) == expected
