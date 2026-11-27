"""행동 고리 3-3 (docs/agent_redesign_2026-11-26.md 2-3 · 2-5 · 2-8):
  table_check       검사 (칸 있음 · 값 종류 · 주제)
  failed_retryable  값 없는 실패 입구 (통계표 안내 · 표 경로의 partial_match)
  _retable          쓴 표 · 사전 표 · 이미 계산해 본 후보를 빼고 다른 표로
  _judged           판정 없이 값 없는 실패를 고쳐 바꾸기 · 못 고치면 처음 답 그대로
문장은 평가셋과 다르게 지어 썼다."""

import asyncio

import pytest

from app.core.config import get_settings
from app.services import agent_action, answer_judge, ask_session, binding, flow, table_check
from app.services import dynamic_indicator as di
from app.services.binding import Binding, Bound, Unbound
from app.services.dynamic_indicator import Dim

DIMS = [
    Dim(0, "항목", {"T1": "출생아수(명)", "T2": "출생성비(명)"}),
    Dim(1, "다태아별", {"0": "계", "1": "단태아", "2": "쌍태아", "3": "삼태아 이상"}),
    Dim(2, "행정구역별", {"00": "전국"}),
]

# ------------------------------------------------------------------ 검사


@pytest.mark.parametrize(
    ("question", "unit", "ok"),
    [
        ("출생아 중 쌍둥이 비율은?", "명", False),
        ("쌍둥이 출생아는 몇 명?", "%", False),
        ("인구 천 명당 출생아 수는?", "명", False),
        ("쌍둥이 출생아는 몇 명?", "명", True),
        ("쌍둥이 비중은?", "%", True),
        ("출생아 추이", "%", True),  # 수를 묻는 말이 없으면 비율 단위도 맞다고 본다
        ("쌍둥이 비율은?", None, True),  # 단위를 모르면 참
    ],
)
def test_kind_ok(question: str, unit: str | None, ok: bool) -> None:
    assert table_check.kind_ok(question, unit) is ok


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> dict:
    box: dict = {"b": None}
    monkeypatch.setattr(di, "schema", lambda tbl_id: (DIMS, 2, ""))
    monkeypatch.setattr(binding, "bind", lambda *a, **k: box["b"])
    return box


def test_bound_cells_that_differ_from_the_answer(bound: dict) -> None:
    """앞 답은 '쌍태아', 질문은 전체(계) → 칸 잇기만으로 다른 답"""
    bound["b"] = Binding("X", bound=[Bound("다태아별", "계", "total", 1, ["0"], ["계"])])
    c = table_check.check("X", "출생아 수", {}, used={(1, "2")})
    assert c.cells_ok and c.differs and c.cells
    c = table_check.check("X", "출생아 수", {}, used={(1, "0")})
    assert c.cells_ok and not c.differs and not c.cells


def test_value_cell_without_known_used_cells_counts_as_different(bound: dict) -> None:
    bound["b"] = Binding("X", bound=[Bound("word", "쌍태아", "value", 1, ["2"], ["쌍태아"])])
    assert table_check.check("X", "쌍둥이", {}).differs


def test_unbound_everyday_word_still_has_cells(bound: dict) -> None:
    """'세쌍둥이'는 글자로 안 닿지만 표에 고를 칸이 있다 → recell 여지 (cells_exist)"""
    bound["b"] = Binding("X", unbound=[Unbound("item", "세쌍둥이", "맞는 칸 없음")])
    c = table_check.check("X", "세쌍둥이 출생아", {})
    assert not c.cells_ok and c.cells_exist and c.cells and c.topic_ok


def test_missing_axis_has_no_cells(bound: dict) -> None:
    bound["b"] = Binding("X", unbound=[Unbound("sex", "여", "표에 성별 구분이 없음")])
    c = table_check.check("X", "여자 출생아", {})
    assert not c.cells_exist and not c.cells


def test_topic_left_unbound_does_not_fit(bound: dict) -> None:
    bound["b"] = Binding("X", unbound=[Unbound("topic", "혼인", "질문의 말과 맞는 칸이 없음")])
    c = table_check.check("X", "혼인 건수", {})
    assert not c.topic_ok and not c.fits
    assert c.as_trace()["fits"] is False


# ------------------------------------------------------------------ 값 없는 실패 입구

FOUND = {
    "handoff": {
        "understanding": {},
        "targets": [
            {
                "candidates": [
                    {"tbl_id": "A", "tbl_nm": "A 표", "picked": True},
                    {"tbl_id": "B", "tbl_nm": "B 표"},
                ]
            }
        ],
    }
}


def _tables_answer(*steps: dict, status: str = "tables", came: str | None = None) -> dict:
    trace: dict = {"dynamic": list(steps)}
    if came:
        trace["refusal"] = {"from": came, "to": status, "why": "시험"}
    return {
        "status": status,
        "answer": "통계표를 안내합니다",
        "parsed": {"indicator": "쌍둥이 출생아"},
        "result": {"status": "tables"},
        "sources": [],
        "trace": trace,
        "request_type": ask_session.NEW,
        "question": "세쌍둥이 출생아 수",
    }


CELL_FAIL = {"tbl_id": "A", "skip": "질문의 말과 맞는 칸이 없음"}
CHECK_FAIL = {"tbl_id": "B", "skip": "통계표 찾기 조건 불충족 (year)"}


def _retryable(res: dict, found: dict | None = FOUND) -> str | None:
    async def run():
        ask_session._found_now.set(found)
        return ask_session.failed_retryable(res)

    return asyncio.run(run())


def test_cell_failure_enters_as_no_value_cells() -> None:
    res = _tables_answer(CELL_FAIL, CHECK_FAIL)
    assert _retryable(res) == agent_action.NO_VALUE_CELLS


def test_all_candidates_failing_checks_enter_as_no_value_checks() -> None:
    res = _tables_answer({**CHECK_FAIL, "tbl_id": "A"}, CHECK_FAIL)
    assert _retryable(res) == agent_action.NO_VALUE_CHECKS
    res = _tables_answer(CELL_FAIL, status="partial_match", came="tables")
    assert _retryable(res) == agent_action.NO_VALUE_CHECKS


def test_what_does_not_enter() -> None:
    assert _retryable(_tables_answer()) is None  # 계산을 시도하지 않은 통계표 안내
    assert _retryable(_tables_answer(CELL_FAIL), found=None) is None  # 꾸러미 없음
    real_refusal = _tables_answer(CELL_FAIL, status="suppressed", came="tables")
    assert _retryable(real_refusal) is None
    other_partial = _tables_answer(CELL_FAIL, status="partial_match", came="ok")
    assert _retryable(other_partial) is None
    follow = {**_tables_answer(CELL_FAIL), "request_type": ask_session.CHANGE}
    assert _retryable(follow) is None
    asking = {**_tables_answer(CELL_FAIL), "clarification": {"kind": "table_cell"}}
    assert _retryable(asking) is None
    two = {**_tables_answer(CELL_FAIL), "parsed": {"indicators": ["a", "b"]}}
    assert _retryable(two) is None


def test_no_value_repick_looks_at_the_top_candidate() -> None:
    entry, why = ask_session.repick_target(_tables_answer(CELL_FAIL), FOUND)
    assert entry["tbl_id"] == "A" and why.startswith("값 없음")


# ------------------------------------------------------------------ retable


def test_used_tables_for_each_kind_of_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ask_session.indicators, "spec", lambda name: {"sources": [{"table": "DICT"}]}
    )
    dictionary = {"parsed": {"indicator": "총인구"}, "result": {"status": "ok"}, "sources": []}
    assert ask_session.used_tables(dictionary) == {"DICT"}
    valued = {
        "parsed": {"dynamic_spec": {"name": "x"}},
        "result": {"status": "ok"},
        "sources": [{"tbl_id": "A"}],
        "trace": {"answer_judge": {"repick": {"tbl_id": "R"}}, "dynamic": [CHECK_FAIL]},
    }
    assert ask_session.used_tables(valued) == {"A", "R"}  # 값 있는 답은 계산 기록의 표를 빼지 않음
    failed = _tables_answer(CELL_FAIL, {"tbl_id": "B", "skip": di.EXACT_SKIP})
    failed["parsed"] = {"dynamic_spec": {"name": "x"}}
    assert ask_session.used_tables(failed) == {"A"}  # 검토가 건너뛴 B 는 아직 안 해 봄


def test_without_tables_strict_and_loose() -> None:
    assert ask_session.without_tables(FOUND, {"Z"}) is None  # 표 경로 답: 쓴 표가 후보에 없음
    assert ask_session.without_tables(FOUND, {"Z"}, strict=False) is FOUND  # 사전 표는 없어도 됨
    rest = ask_session.without_tables(FOUND, {"A"}, strict=False)
    assert [c["tbl_id"] for c in rest["handoff"]["targets"][0]["candidates"]] == ["B"]
    assert ask_session.without_tables(FOUND, {"A", "B"}, strict=False) is None


def test_retable_on_a_dictionary_answer_drops_the_dictionary_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: {"sources": [{"table": "A"}]})

    async def compute(question, parsed, found, trace, repick=None):
        seen.append([c["tbl_id"] for c in found["handoff"]["targets"][0]["candidates"]])
        return {"status": "ok", "answer": "B 표로 계산", "result": {"status": "ok", "value": 1}}

    monkeypatch.setattr(di, "compute_from_handoff", compute)
    res = {"parsed": {"indicator": "총인구"}, "result": {"status": "ok"}, "sources": []}

    async def run():
        ask_session._found_now.set(FOUND)
        return await ask_session._retable(res, None, "q", None)

    again, how = asyncio.run(run())
    assert seen == [["B"]] and again["answer"] == "B 표로 계산" and how == "다음 후보 표"


# ------------------------------------------------------------------ 고리: 판정 없이 값 없는 실패


@pytest.fixture
def on_failure(monkeypatch: pytest.MonkeyPatch) -> dict:
    """판정은 끄고 값 없는 실패 입구만 켠다. 행동은 정해 둔 결과를 낸다"""
    box: dict = {"repick": None, "retable": None, "replan": None, "calls": []}
    monkeypatch.setattr(answer_judge, "enabled", lambda: False)
    monkeypatch.setattr(get_settings(), "agent_on_failure", True)

    async def verdict(*a):
        raise AssertionError("판정을 부르면 안 된다")

    async def cells(res, verdict, found, asked=None):
        return {"cells": True, "fits": True, "blocked": None, "by": "시험"}

    def act(name):
        async def run(*a, **k):
            box["calls"].append(name)
            return box[name], name

        return run

    monkeypatch.setattr(answer_judge, "verdict", verdict)
    monkeypatch.setattr(ask_session, "repick_cells", cells)
    monkeypatch.setattr(ask_session, "_repick_compute", act("repick"))
    monkeypatch.setattr(ask_session, "_retable", act("retable"))
    monkeypatch.setattr(ask_session, "_replan_compute", act("replan"))

    async def attach(res, asked):
        return None

    monkeypatch.setattr(ask_session, "_attach_cards", attach)
    return box


def _judged(res: dict) -> dict:
    async def run():
        flow.start()
        ask_session._found_now.set(FOUND)
        await ask_session._judged(res, None, res["question"], None)

    asyncio.run(run())
    return res


def test_no_value_failure_is_fixed_without_the_judge(on_failure: dict) -> None:
    on_failure["repick"] = {
        "status": "ok",
        "answer": "세쌍둥이 이상 출생아는 300명입니다.",
        "result": {"status": "ok", "value": 300},
        "trace": {},
    }
    res = _judged(_tables_answer(CELL_FAIL))
    assert on_failure["calls"] == ["repick"]
    assert res["status"] == "ok" and res["answer"].startswith("세쌍둥이")
    record = res["trace"]["answer_judge"]
    assert record["entry"] == ask_session.NO_VALUE_ENTRY and record["replaced"]["attempt"] == 1
    assert res["request_type"] == ask_session.NEW


def test_unfixed_no_value_failure_keeps_the_table_answer(on_failure: dict) -> None:
    res = _judged(_tables_answer(CELL_FAIL))
    assert on_failure["calls"] == ["repick", "retable", "replan"]
    assert res["status"] == "tables" and res["answer"] == "통계표를 안내합니다"
    assert ask_session.CAUTION not in res["answer"]
    assert res["trace"]["answer_judge"]["end"]["case"] == "처음 답 그대로 (값 없음)"


def test_all_checks_failed_goes_straight_to_replan(on_failure: dict) -> None:
    res = _judged(_tables_answer({**CHECK_FAIL, "tbl_id": "A"}, CHECK_FAIL))
    assert on_failure["calls"] == ["replan"]
    assert res["status"] == "tables"


def test_a_refusal_from_a_retry_does_not_replace_a_no_value_answer(on_failure: dict) -> None:
    for status in ("not_collected_item", "suppressed"):
        on_failure["repick"] = {"status": status, "answer": "답하지 못함", "result": {}}
        res = _judged(_tables_answer(CELL_FAIL))
        assert res["status"] == "tables"


def test_switch_off_leaves_failures_alone(on_failure: dict, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_on_failure", False)
    res = _judged(_tables_answer(CELL_FAIL))
    assert on_failure["calls"] == [] and "answer_judge" not in res["trace"]
