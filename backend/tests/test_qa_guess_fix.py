from datetime import date

import pytest

from app.services import indicators, qa

SAYS_YEAR = [
    "2022년 서울 인구",
    "2022년도 서울 인구",
    "재작년 서울 인구",
    "지난해 서울 인구",
    "전년도 서울 인구",
    "금년 서울 인구",
    "2년전 서울 인구",
    "2022 서울 인구",
    "서울 인구 2022",
    "서울 인구(2022)",
    "22년 서울 인구",
    "'22년 서울 인구",
    "2022학년도 서울 인구",
    "2022.12 서울 인구",
    "2022-12 서울 인구",
    "2020 서울과 부산 합계출산율 비교",
    "2021 인구가 가장 많이 빠져나간 시군구",
    "이년 전 서울 인구",
    "십 년 전 서울 인구",
    "이천이십이년 서울 인구",
    "코로나 첫 해 서울 인구",
    "2002 월드컵 때 서울 인구",
    "서울 출생아 수 (2015)",
]

NO_YEAR = [
    "서울 인구",
    "서울 외국인주민 수",
    "인구가 가장 많이 빠져나간 시군구는?",
    "부산 청년 인구",
    "유소년 인구 비율",
    "노년 인구",
    "해운대 출생아 수",
]


@pytest.mark.parametrize("question", SAYS_YEAR)
def test_says_year(question):
    assert qa._says_year(question)


@pytest.mark.parametrize("question", NO_YEAR)
def test_no_year(question):
    assert not qa._says_year(question)


def _ground(question, region):
    parsed = {"regions": [region]}

    def fix(key, value, why):
        parsed[key] = value

    qa._ground_regions(question, parsed, fix)
    return "막음" if parsed.get("ungrounded_region") else parsed["regions"]


@pytest.mark.parametrize(
    "question, region, expected",
    [
        ("강남 인구", "서울특별시 강남구", ["서울특별시 강남구"]),
        ("서울 강서 인구", "서울특별시 강서구", ["서울특별시 강서구"]),
        ("서울 강서 인구", "부산광역시 강서구", "막음"),
        ("강서 인구", "서울특별시 강서구", ["강서"]),
        ("고성 인구", "강원특별자치도 고성군", ["고성"]),
        ("송도 인구", "인천광역시", "막음"),
        ("고령 인구 비율", "경상북도 고령군", "막음"),
        ("장수 노인 인구", "전북특별자치도 장수군", "막음"),
        ("영양 상태가 나쁜 인구", "경상북도 영양군", "막음"),
        ("이천 명이 넘는 출생아 수", "경기도 이천시", "막음"),
    ],
)
@pytest.mark.needs_data
def test_ground_regions(question, region, expected):
    assert _ground(question, region) == expected


@pytest.mark.parametrize(
    ("question", "parsed_regions", "expected"),
    [
        ("서울 강남 인구 알려줘", ["서울"], ["서울특별시 강남구"]),
        ("강남 인구 알려줘", [], ["서울특별시 강남구"]),
        ("서울 강서 인구 알려줘", ["서울"], ["서울특별시 강서구"]),
        ("강서 인구 알려줘", ["서울특별시 강서구"], ["강서"]),
        ("서울 인구 알려줘", ["서울"], ["서울"]),
    ],
)
def test_short_population_region_is_used_for_calculation(
    monkeypatch, question, parsed_regions, expected
):
    seoul = {
        "status": "ok",
        "region_code": "11",
        "region_name": "서울특별시",
        "kind": "시도",
        "display": "서울특별시",
    }
    gangnam = {
        "status": "ok",
        "region_code": "11680",
        "region_name": "강남구",
        "sido_code": "11",
        "kind": "시군구",
        "display": "서울특별시 강남구",
    }
    gangseo = {
        "status": "ok",
        "region_code": "11500",
        "region_name": "강서구",
        "sido_code": "11",
        "kind": "시군구",
        "display": "서울특별시 강서구",
    }
    regions = {
        "서울": seoul,
        "강남": gangnam,
        "서울 강남": gangnam,
        gangnam["display"]: gangnam,
        "강서": {"status": "ambiguous_region", "candidates": [gangseo["display"], "부산 강서구"]},
        "서울 강서": gangseo,
        gangseo["display"]: gangseo,
    }
    monkeypatch.setattr(
        indicators, "_region_names", lambda: ["서울특별시", "서울", "강남구", "강서구"]
    )
    monkeypatch.setattr(
        indicators, "resolve_region", lambda name: regions.get(name, {"status": "unknown_region"})
    )
    parsed = {"type": "value", "indicator": "총인구", "regions": parsed_regions, "sex": "계"}
    corrected = qa.correct(question, parsed, date(2026, 11, 24))
    assert corrected["regions"] == expected
    assert "ungrounded_region" not in corrected


@pytest.mark.parametrize(
    "period", ["재작년까지 5년간", "2024년까지 최근 5년", "2년 전까지 5년 동안"]
)
def test_explicit_end_year_is_used_in_new_question(monkeypatch, period):
    monkeypatch.setattr(indicators, "find_regions", lambda _: [])
    question = f"{period} 출생아 수 알려줘"
    parsed = {"type": "value", "indicator": "출생아 수", "regions": [], "sex": "계", "year": 2025}
    corrected = qa.correct(question, parsed, date(2026, 11, 24))
    assert corrected["type"] == "trend"
    assert (corrected["start_year"], corrected["end_year"]) == (2020, 2024)
    assert corrected["year"] is None
    assert "recent" not in (corrected.get("asked") or {})
