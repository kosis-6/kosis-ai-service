import pytest

from app.services import agegroup


@pytest.mark.parametrize(
    "q, lo, hi",
    [
        ("서울 신중년 인구", 50, 64),
        ("서울 준고령자 인구", 50, 54),
        ("서울 후기 고령인구", 75, None),
        ("서울 초고령 인구", 85, None),
        ("서울 미취학 아동 인구", 0, 6),
        ("서울 취학 전 인구", 0, 6),
        ("서울 아이들 인구", 0, 14),
        ("서울 아이 인구", 0, 14),
        ("서울 아기 인구", 0, 0),
        ("서울 갓난아기 인구", 0, 0),
        ("서울 영아 인구", 0, 0),
        ("서울 유치원생 인구", 3, 5),
        ("서울 대학생 인구", 18, 21),
        ("서울 2030세대 인구", 20, 39),
        ("서울 4050세대 인구", 40, 59),
        ("서울 할머니 할아버지 몇 명이야", 65, None),
        ("서울 중년 인구", 40, 64),
        ("서울 노인 인구", 65, None),
        ("서울 애들 몇 명이야", 0, 14),
        ("세종 청년 인구 몇 명이야?", 19, 34),
    ],
)
def test_everyday_age_words(q, lo, hi):
    ref = agegroup.resolve(q)
    assert ref is not None, q
    assert (ref.lo, ref.hi) == (lo, hi), q


@pytest.mark.parametrize(
    "q",
    [
        "작년에 서울에서 아기 몇 명 태어났어",
        "서울 영아 사망률",
        "초고령사회 진입한 시도",
        "어린이집 다니는 아이들 수",
    ],
)
def test_not_age_group(q):
    assert agegroup.resolve(q) is None, q


def test_label_single_year():
    assert agegroup.resolve("서울 영아 인구").label() == "0세"
