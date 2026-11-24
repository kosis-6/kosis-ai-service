import pytest

from app.services import qa


def first_group(question):
    return next((w for w in qa.REGION_GROUPS if qa._group_in(w, question)), None)


@pytest.mark.parametrize(
    "q, group",
    [
        ("비수도권 인구", "비수도권"),
        ("수도권 외 인구", "수도권 외"),
        ("수도권 외의 출생아 수", "수도권 외"),
        ("수도권 외 지역 출생아 수", "수도권 외"),
        ("수도권 외에서 태어난 아이", "수도권 외"),
        ("수도권 밖 인구", "수도권 밖"),
        ("2024년 수도권 외국인 수는?", "수도권"),
        ("수도권 외곽 인구", "수도권"),
        ("지방 인구 알려줘", "지방 인구"),
        ("수도권 인구", "수도권"),
        ("광역시 전체 인구", "광역시 전체"),
        ("모든 광역시 인구", "모든 광역시"),
        ("6대 광역시 인구", "6대 광역시"),
        ("9개 도 인구", "9개 도"),
        ("부울경 인구", "부울경"),
    ],
)
def test_region_group(q, group):
    assert first_group(q) == group


@pytest.mark.parametrize(
    "q",
    ["시도 지역별 인구", "모든 도시 인구", "지방소멸 위험 지역 알려줘", "서울 인구"],
)
def test_not_region_group(q):
    assert first_group(q) is None


def test_non_capital_has_14_sido():
    members = qa.REGION_GROUPS["비수도권"]
    assert len(members) == 14
    assert not {"서울", "인천", "경기"} & set(members)


@pytest.mark.parametrize("q", ["강남3구 인구 합치면 몇 명이야?", "강남 3구 인구"])
def test_gangnam_three_districts(q):
    assert qa.REGION_GROUPS[first_group(q)] == ["서울 강남구", "서울 서초구", "서울 송파구"]
