import pytest

from app.services import qa

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
