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


@pytest.mark.parametrize(
    ("question", "regions", "expected"),
    [
        # 11/27 차트 점검: 추이는 지역을 하나씩 조회해 '광주'를 경기 광주시와 헷갈린다고 되물었다
        (
            "서울 부산 대구 인천 광주 대전 울산 출생아 수 추이",
            ["서울", "부산", "대구", "인천", "광주", "대전", "울산"],
            ["서울", "부산", "대구", "인천", "광주광역시", "대전", "울산"],
        ),
        # 혼자 말하거나 시군구와 함께 말하면 그대로 두고 되묻는다
        ("광주 인구 추이", ["광주"], ["광주"]),
        ("광주와 용인 인구", ["광주", "용인"], ["광주", "용인"]),
    ],
)
def test_ambiguous_name_among_sido_is_the_sido(question, regions, expected):
    from datetime import date

    from app.services import qa

    parsed = {"type": "trend", "indicator": "출생아 수", "regions": regions, "sex": "계"}
    assert qa.correct(question, parsed, date(2026, 11, 27))["regions"] == expected


SIDO_17 = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시",
    "울산광역시", "세종특별자치시", "경기도", "강원도", "충청북도", "충청남도", "전라북도",
    "전라남도", "경상북도", "경상남도", "제주특별자치도",
]  # fmt: skip


@pytest.mark.parametrize(
    ("question", "parsed", "level", "sido"),
    [
        # 11/27 차트 점검: HCX가 펼친 17개 시도를 '질문에 없는 지역'으로 막았다
        (
            "2024년 시도별 고령인구 비율 비교해줘",
            {"indicator": "고령인구 비율", "regions": SIDO_17, "level": "시도"},
            "시도",
            None,
        ),
        # 11/27 차트 점검: '시군별'을 못 읽고 경기도 값 하나로 답했다
        (
            "2024년 경기도 시군별 인구 비교",
            {"indicator": "총인구", "regions": ["경기도"], "sido": "경기도", "level": "시군구"},
            "시군구",
            "경기도",
        ),
    ],
)
def test_compare_by_region_without_named_regions_is_a_list(question, parsed, level, sido):
    from datetime import date

    from app.services import qa

    base = {"type": "compare", "year": 2024, "sex": "계"}
    p = qa.correct(question, {**base, **parsed}, date(2026, 11, 27))
    assert p["type"] == "rank" and p["level"] == level and p.get("sido") == sido
    assert not p.get("regions") and not p.get("ungrounded_region")


@pytest.mark.parametrize(
    "question", ["2024년 서울과 부산 인구 비교", "서울과 부산의 구별 인구 비교"]
)
def test_compare_of_named_regions_stays_a_compare(question):
    """지역을 둘 이상 말한 비교는 '별'이 있어도 그 지역끼리의 비교로 둔다"""
    from datetime import date

    from app.services import qa

    parsed = {"type": "compare", "indicator": "총인구", "regions": ["서울", "부산"], "year": 2024}
    p = qa.correct(question, {**parsed, "sex": "계"}, date(2026, 11, 27))
    assert p["type"] == "compare" and p["regions"] == ["서울", "부산"]


@pytest.mark.parametrize(
    ("question", "parsed", "kind"),
    [
        # PR 리뷰: 두 해 비교가 한 해 순위로 바뀌어 연도를 잃었다
        (
            "2020년과 2024년 경기도 시군별 인구 비교",
            {"indicator": "총인구", "regions": ["경기도"], "sido": "경기도", "level": "시군구",
             "start_year": 2020, "end_year": 2024},
            "trend",
        ),
        # PR 리뷰: 수원시 범위가 빠져 전국 시군구 순위로 넓어졌다
        ("2024년 수원시 구별 인구 비교", {"indicator": "총인구", "regions": ["수원시"]}, "compare"),
        # PR 리뷰: 순위로 먼저 바뀌어 사망자 수가 빠졌다
        (
            "2024년 경기도 시군별 출생아 수와 사망자 수 비교",
            {"indicator": "출생아 수", "regions": ["경기도"], "sido": "경기도", "level": "시군구"},
            "compare",
        ),
    ],
)  # fmt: skip
def test_compare_by_region_that_must_stay_a_compare(question, parsed, kind):
    from datetime import date

    from app.services import qa

    base = {"type": "compare", "year": 2024, "sex": "계"}
    p = qa.correct(question, {**base, **parsed}, date(2026, 11, 27))
    assert p["type"] == kind and p.get("regions") == parsed["regions"]
    if "사망자" in question:
        assert p["indicators"] == ["출생아 수", "사망자 수"]
