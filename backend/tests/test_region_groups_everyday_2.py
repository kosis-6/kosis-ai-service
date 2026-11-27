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


METROS_6 = ["부산", "대구", "인천", "광주광역시", "대전", "울산"]


def group_regions(q):
    return qa._with_named_regions(q, *qa.region_group(q))[1]


@pytest.mark.needs_data  # 따로 말한 지역을 resolve_region으로 확인한다
@pytest.mark.parametrize(
    ("q", "members"),
    [
        # 11/27 차트 점검: '7개 광역시와 세종'을 묶음으로 못 읽어 지역 확인 실패
        ("7개 광역시와 세종의 출생아 수 추이", [*METROS_6, "세종"]),
        ("6개 광역시 인구", METROS_6),
        ("6대 광역시와 세종 인구", [*METROS_6, "세종"]),
        # PR 리뷰: '5대 광역시와 세종'에 울산이 끼어 7곳이 됐다 (5대 광역시 + 세종 = 6곳)
        ("5대 광역시와 세종 인구 비교", [*qa.REGION_GROUPS["5대 광역시"], "세종"]),
        ("지방 광역시와 세종 인구", [*qa.REGION_GROUPS["지방 광역시"], "세종"]),
        # 세종만이 아니라 묶음 밖에서 따로 말한 지역은 모두 더한다
        ("수도권과 강원 인구", [*qa.REGION_GROUPS["수도권"], "강원"]),
        ("영남과 제주 인구 비교", [*qa.REGION_GROUPS["영남"], "제주"]),
        # 묶음에 이미 든 지역은 두 번 넣지 않는다
        ("수도권과 서울 인구", qa.REGION_GROUPS["수도권"]),
        ("5대 광역시 인구", qa.REGION_GROUPS["5대 광역시"]),
    ],
)
def test_group_and_named_regions(q, members):
    assert group_regions(q) == members


def test_counted_words_without_a_group_are_not_groups():
    assert qa.region_group("17개 시도 인구") is None
    assert qa.region_group("부산광역시 인구") is None


@pytest.mark.needs_data
@pytest.mark.parametrize(
    ("q", "label"),
    [
        # 더한 지역이 있으면 묶음 이름도 질문의 말로 ('수도권은 … 강원 기준'이라 하지 않게)
        ("수도권과 강원 인구 비교", "수도권과 강원"),
        ("5대 광역시와 세종 인구 비교", "5대 광역시와 세종"),
        ("7개 광역시와 세종의 출생아 수 추이", "7개 광역시와 세종"),
        ("수도권과 서울 인구", "수도권"),
        ("5대 광역시 인구", "5대 광역시"),
    ],
)
def test_group_label_names_the_added_regions(q, label):
    assert qa._with_named_regions(q, *qa.region_group(q))[0] == label
