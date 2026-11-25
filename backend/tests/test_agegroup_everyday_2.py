import pytest

from app.services import agegroup


@pytest.mark.parametrize(
    "q, lo, hi",
    [
        ("서울 중고등학생 인구", 13, 18),
        ("서울 중고생 몇 명이야", 13, 18),
        ("서울 초중고생 인구", 7, 18),
        ("서울 초등생 인구", 7, 12),
        ("서울 고교생 인구", 16, 18),
        ("서울 중학교 다니는 인구", 13, 15),
        ("서울 고등학교 다니는 인구", 16, 18),
        ("서울 유아 몇 명이야", 3, 5),
        ("서울 핵심 경제활동 연령 인구", 25, 49),
        ("서울 핵심생산연령인구", 25, 49),
        ("서울 가임기 여성 인구", 15, 49),
        ("서울 가임 여성 몇 명이야", 15, 49),
        ("서울 성인 인구", 19, None),
        ("서울 어른 몇 명이야", 19, None),
        ("서울 시니어 인구", 65, None),
        ("부산 실버 인구", 65, None),
        ("서울 고등학생 인구", 16, 18),
        ("서울 중학생 인구", 13, 15),
        ("서울 영유아 인구", 0, 5),
        ("서울 생산연령인구", 15, 64),
        ("경기 미성년 인구", 0, 18),
    ],
)
def test_everyday_age_words_2(q, lo, hi):
    ref = agegroup.resolve(q)
    assert ref is not None, q
    assert (ref.lo, ref.hi) == (lo, hi), q


@pytest.mark.parametrize(
    "q",
    [
        "성인병 환자 많은 지역",
        "작년에 서울에서 신생아 몇 명 태어났어",
    ],
)
def test_not_age_group_2(q):
    assert agegroup.resolve(q) is None, q


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("미성년 자녀 셋 이상 키우는 집", "미성년 자녀 셋 이상 키우는 집"),
        ("아이 3명 이상 낳은 여성", "아이 3명 이상 낳은 여성"),
        ("미성년 자녀 넷 키우는 집", "미성년 4자녀 키우는 집"),
        ("자녀가 딱 셋인 집", "자녀가 딱 3인 집"),
    ],
)
def test_count_words_keep_ranges_for_binding(text: str, want: str) -> None:
    assert agegroup.count_digits(text) == want
