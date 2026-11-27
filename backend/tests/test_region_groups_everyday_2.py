import pytest

from app.services import qa


def first_group(q):
    return next((w for w in qa.REGION_GROUPS if qa._group_in(w, q)), None)


@pytest.mark.parametrize(
    "q, group, n",
    [
        ("영남 지역 인구", "영남", 5),
        ("영남 인구", "영남", 5),
        ("호남 지역 인구", "호남", 3),
        ("충청 지역 인구", "충청 지역", 4),
        ("충청지역 인구", "충청지역", 4),
        ("마용성 인구", "마용성", 3),
        ("노도강 인구", "노도강", 3),
        ("금관구 인구", "금관구", 3),
        ("지방 광역시 인구", "지방 광역시", 5),
        ("특례시 인구", "특례시", 5),
        ("영남권 인구", "영남권", 5),
        ("호남권 인구", "호남권", 3),
        ("충청권 인구", "충청권", 4),
        ("광역시 전체 인구", "광역시 전체", 6),
        # 11/27 차트 점검: '7개 광역시와 세종'을 묶음으로 못 읽어 지역 확인 실패
        ("7개 광역시와 세종의 출생아 수 추이", "광역시와 세종", 7),
        ("6대 광역시와 세종 인구", "광역시와 세종", 7),
        ("광역시랑 세종 인구 비교", "광역시랑 세종", 7),
        ("6개 광역시 인구", "6개 광역시", 6),
    ],
)
def test_region_groups_2(q, group, n):
    assert first_group(q) == group, q
    assert len(qa.REGION_GROUPS[group]) == n, q


def test_regional_metros_exclude_incheon():
    assert "인천" not in qa.REGION_GROUPS["지방 광역시"]


@pytest.mark.parametrize(
    "q, area",
    [
        ("운정 인구", "운정"),
        ("다산 인구", "다산"),
        ("별내 인구", "별내"),
        ("미사 인구", "미사"),
        ("평촌 인구", "평촌"),
        ("산본 인구", "산본"),
        ("부천 중동 인구", "중동"),
        ("여의도 인구", "여의도"),
        ("센텀 인구", "센텀"),
        ("서면 인구", "서면"),
        ("둔산 인구", "둔산"),
        ("1기 신도시 인구", "1기 신도시"),
        ("일산 인구", "일산"),
        ("잠실에서 사는 사람", "잠실"),
        ("분당 인구", "분당"),
    ],
)
def test_sub_areas_2(q, area):
    sub = qa.find_sub_area(q)
    assert sub is not None, q
    assert sub[0] == area, q


@pytest.mark.parametrize(
    "q",
    [
        "일산화탄소 중독 사망자",
        "미사일 발사",
        "서울 다산 가정 몇 가구야",
        "서면으로 신청한 사람 수",
        "중동 출신 외국인 몇 명이야",
        "1분당 출생아 수",
        "분당구 인구",
    ],
)
def test_not_sub_area(q):
    assert qa.find_sub_area(q) is None, q


@pytest.mark.parametrize(
    "q",
    [
        "부울경 인구",
        "특례시 인구",
        "마용성 인구",
        "강남 3구 인구",
        "지방 광역시 인구",
        "영남 인구",
        "운정 인구",
    ],
)
def test_group_name_is_not_population_modifier(q):
    assert qa.population_modifier(q) is None, q


@pytest.mark.needs_data
def test_population_modifier_still_works():
    assert qa.population_modifier("통학 인구") is not None
