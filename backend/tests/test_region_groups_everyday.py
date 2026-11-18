"""지역 묶음 일상어 (일상어 사전 확장, 2026-11-18)

qa._read_question 은 REGION_GROUPS 를 위에서부터 보고 질문에 처음 들어 있는 묶음을 쓴다.
"""

import pytest

from app.services import qa


def first_group(question):
    return next((w for w in qa.REGION_GROUPS if w in question), None)


@pytest.mark.parametrize(
    "q, group",
    [
        ("비수도권 인구", "비수도권"),
        ("수도권 외 인구", "수도권 외"),
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
