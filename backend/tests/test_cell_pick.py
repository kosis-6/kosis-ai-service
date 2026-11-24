import asyncio

import pytest

from app.prompts import cell_pick as prompt
from app.services import cell_pick
from app.services.cell_pick import Group


@pytest.mark.parametrize(
    "why, expected",
    [
        ("질문의 말과 맞는 칸이 없음", True),
        ("항목에서 칸을 고르지 못함", True),
        ("통계표 찾기 조건 불충족 (target)", False),
        ("표에 성별 구분이 없음", False),
        (None, False),
    ],
)
def test_needs_llm_only_for_cell_failures(why, expected) -> None:
    assert cell_pick.needs_llm(why) is expected


def test_needs_llm_ignores_asks() -> None:
    from app.services.dynamic_indicator import Ask

    assert not cell_pick.needs_llm(Ask(1, "항목", [("a", "가"), ("b", "나")]))


def test_narrow_keeps_total_and_cells_sharing_words() -> None:
    cells = [("00", "계")] + [(f"{i:03}", f"업종{i}") for i in range(100)] + [("X", "음식점업")]
    out = cell_pick.narrow("작년 음식점 수", cells)
    assert out == [("00", "계"), ("X", "음식점업")]
    small = cells[:10]
    assert cell_pick.narrow("아무 말", small) == small


GROUPS = [
    Group(0, "항목", [("T1", "혼인"), ("T2", "이혼")], 2),
    Group(2, "혼인종류", [("00", "총계"), ("01", "초혼")], 2),
]


def test_decide_maps_numbers_to_codes_and_keeps_real_readings() -> None:
    raw = {
        "fits": "예",
        "picks": {"항목": "1", "혼인종류": "1"},
        "read_as": [{"질문": "결혼", "표": "혼인"}, {"질문": "결혼", "표": "출생"}],
        "reason": "r",
    }
    picked = cell_pick.decide(raw, GROUPS)
    assert picked.forced == {0: "T1", 2: "00"}
    assert picked.unsure == []
    assert picked.read_as == [("결혼", "혼인")]
    assert picked.notice() == "질문의 '결혼'을(를) 표의 '혼인'(으)로 보고 계산했습니다"


@pytest.mark.parametrize("value", ["정할 수 없음", "9", "", None])
def test_decide_unsure_or_out_of_list(value) -> None:
    raw = {"fits": "예", "picks": {"항목": "1", "혼인종류": value}, "reason": "r"}
    picked = cell_pick.decide(raw, GROUPS)
    assert picked.forced == {0: "T1"}
    assert [g.no for g in picked.unsure] == [2]


@pytest.mark.parametrize(
    "raw", [None, {"fits": "아니오", "picks": {}}, {"fits": "예", "picks": "1"}, {"picks": {}}]
)
def test_decide_rejects(raw) -> None:
    assert cell_pick.decide(raw, GROUPS) is None


def test_user_text_and_schema_use_the_same_names() -> None:
    groups = [*GROUPS, Group(3, "혼인종류", [("00", "총계")], 120)]
    text = cell_pick.user_text("대전 결혼 건수", {"topic": "혼인 건수"}, "혼인 표", groups)
    assert "[항목] 1. 혼인  2. 이혼" in text
    assert "[혼인종류 (3)] 1. 총계  (칸 120개 중 질문과 글자가 겹치는 것만)" in text
    shape = prompt.schema({"항목": 2, "혼인종류": 2})
    assert shape["properties"]["picks"]["properties"]["항목"]["enum"] == ["1", "2", "정할 수 없음"]


def test_choose_records_and_survives_llm_errors(monkeypatch) -> None:
    async def broken(*args):
        raise RuntimeError("down")

    monkeypatch.setattr(cell_pick, "judge", broken)
    step: dict = {}
    assert asyncio.run(cell_pick.choose("q", {}, "t", GROUPS, step)) is None
    assert step["cell_llm"]["error"].startswith("RuntimeError")


def test_schema_puts_picks_last_and_lets_read_as_be_null() -> None:
    shape = prompt.schema({"항목": 2})
    assert list(shape["properties"]) == ["fits", "reason", "read_as", "picks"]
    assert shape["required"] == ["fits", "reason", "read_as", "picks"]
    assert {"type": "null"} in shape["properties"]["read_as"]["anyOf"]
    assert "null로 둔다" in prompt.SYSTEM
