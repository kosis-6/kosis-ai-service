import pytest

from app.services import cell_pick, llm_json
from app.services.cell_pick import Group


def test_whole_json_is_read_as_is() -> None:
    assert llm_json.loads('```json\n{"a": 1, "b": [1, 2]}\n```') == ({"a": 1, "b": [1, 2]}, False)


def test_cut_after_whitespace_run_keeps_finished_fields() -> None:
    raw = '{"fits": "예", "picks": {"항목": \n"1", "국적별": "1"}, "read_as": \n' + " " * 800
    obj, salvaged = llm_json.loads(raw)
    assert salvaged and obj == {"fits": "예", "picks": {"항목": "1", "국적별": "1"}}


def test_cut_inside_a_string_drops_only_that_field() -> None:
    obj, salvaged = llm_json.loads('{"action": "offer", "offers": [{"id": "A1", "label": "2024')
    assert salvaged and obj == {"action": "offer", "offers": [{"id": "A1"}]}


@pytest.mark.parametrize("raw", [None, "", "답할 수 없습니다", "[1, 2]", '{"a": '])
def test_nothing_to_keep(raw) -> None:
    assert llm_json.loads(raw) == (None, False)


GROUPS = [Group(0, "항목", [("T1", "혼인")], 1)]


@pytest.mark.parametrize(
    "raw, picked, label",
    [
        ({"fits": "예", "picks": {"항목": "1"}}, True, {"0": "T1"}),
        ({"fits": "예", "picks": {"항목": "정할 수 없음"}}, True, "정할 수 없음"),
        ({"fits": "아니오", "picks": {}}, False, "표에 없음"),
        (None, False, "응답 해석 실패"),
        ({"fits": "예"}, False, "응답 해석 실패"),
        ({"fits": "예", "reason": "r", "_salvaged": True}, True, "정할 수 없음 (응답 잘림)"),
        ({"fits": "아니오", "_salvaged": True}, False, "표에 없음"),
    ],
)
def test_cell_result_labels(raw, picked, label) -> None:
    p = cell_pick.decide(raw, GROUPS)
    assert (p is not None) is picked
    assert cell_pick.result_label(raw, p) == label
