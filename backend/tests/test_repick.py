"""repick: 표는 맞는데 칸이 틀린 답을 같은 표에서 칸만 다시 고른다
(docs/agent_redesign_2026-11-26.md 2-2 · 2-6 · 2-7 · 3-3).

  _retry            repick 갈래, 못 하면 대체 행동
  repick_target     다시 볼 표: 값을 낸 표, 아래 순위 표가 답했으면 1위 표, 사전 답이면 출처 표
  choose            '조건이 빠짐 · 대상이 다름' + 칸 있음 → repick, 안 되면 retable
  compute_from_handoff(repick=...)  칸 잇기 → (판정 지적을 넘긴) recell
  _judged           repick 의 recell 은 LLM 행동 한도에 센다
문장은 평가셋과 다르게 지어 썼다."""

import asyncio

import pytest

from app.services import agent_action, answer_judge, ask_session, binding, recell
from app.services import dynamic_indicator as di
from app.services.binding import Binding, Bound
from app.services.dynamic_indicator import Dim

VERDICT = {"reason": "조건이 빠짐", "missing": "여자만 물었는데 전체", "verdict": "retry"}


def _res(**extra) -> dict:
    return {
        "status": "ok",
        "parsed": {"indicator": "x", "dynamic_spec": {"name": "x"}},
        "result": {"status": "ok", "value": 1},
        "sources": [{"tbl_id": "T1"}],
        **extra,
    }


def _found(*tbl_ids: str, picked: str | None = None) -> dict:
    picked = picked or tbl_ids[0]
    cands = [{"tbl_id": t, "tbl_nm": f"{t} 표", "picked": t == picked} for t in tbl_ids]
    return {"handoff": {"understanding": {}, "targets": [{"candidates": cands}]}}


# ---------------------------------------------------------------- _retry 갈래


def _patch(monkeypatch: pytest.MonkeyPatch, repick_out) -> list[str]:
    calls: list[str] = []

    async def repick(res, verdict, asked, llm_ok=True):
        calls.append(f"repick(llm={llm_ok})")
        return repick_out, "같은 표에서 칸 다시"

    async def recompute(res, state, asked, user_id):
        calls.append("recompute")
        return {"status": "ok", "answer": "다음 후보"}, "다음 후보 표"

    async def replan(res, verdict, asked, user_id):
        calls.append("replan")
        return None, "계획 다시"

    monkeypatch.setattr(ask_session, "_repick_compute", repick)
    monkeypatch.setattr(ask_session, "_recompute", recompute)
    monkeypatch.setattr(ask_session, "_replan_compute", replan)
    return calls


def _retry(action: agent_action.Action, done: set[str]):
    return asyncio.run(ask_session._retry(action, _res(), VERDICT, None, "q", None, done))


def test_repick_answer_is_returned_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, {"status": "ok", "answer": "같은 표 여자 칸"})
    done: set[str] = set()
    action = agent_action.Action(agent_action.REPICK, "시험", fallback=agent_action.RETABLE)
    again, how = _retry(action, done)
    assert calls == ["repick(llm=True)"]
    assert again["answer"] == "같은 표 여자 칸" and how == "같은 표에서 칸 다시"
    assert done == set()


def test_repick_failure_falls_back_and_marks_it_done(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, None)
    done: set[str] = set()
    action = agent_action.Action(
        agent_action.REPICK, "시험", fallback=agent_action.RETABLE, detail={"llm": False}
    )
    again, how = _retry(action, done)
    assert calls == ["repick(llm=False)", "recompute"]
    assert how == "다음 후보 표" and done == {agent_action.RETABLE}


def test_repick_without_fallback_ends_there(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, None)
    again, how = _retry(agent_action.Action(agent_action.REPICK, "시험"), set())
    assert calls == ["repick(llm=True)"] and again is None and how == "같은 표에서 칸 다시"


# ---------------------------------------------------------------- 다시 볼 표


def test_target_is_the_table_that_answered() -> None:
    res = _res(trace={"handoff": {"used": {"tbl_id": "T1", "kind": "picked"}}})
    entry, why = ask_session.repick_target(res, _found("T1", "T2"))
    assert entry["tbl_id"] == "T1" and entry["tbl_nm"] == "T1 표" and why == "값을 낸 표"


def test_target_is_the_top_table_when_a_lower_one_answered() -> None:
    res = _res(
        sources=[{"tbl_id": "T2"}],
        trace={"handoff": {"used": {"tbl_id": "T2", "kind": "other"}}},
    )
    entry, why = ask_session.repick_target(res, _found("T1", "T2"))
    assert entry["tbl_id"] == "T1" and why.startswith("아래 순위")


def test_target_without_bundle_uses_the_source_table() -> None:
    entry, why = ask_session.repick_target(_res(), None)
    assert entry == {"tbl_id": "T1"} and why == "값을 낸 표"


def test_dictionary_answer_targets_its_source_table(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = {"sources": [{"table": "DT_A"}, {"table": "DT_B"}]}
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: spec)
    res = {"parsed": {"indicator": "사전 지표"}, "sources": [{"tbl_id": "DT_B"}]}
    assert ask_session.repick_target(res, None) == ({"tbl_id": "DT_B"}, "사전 지표의 출처 표")
    res["sources"] = []
    assert ask_session.repick_target(res, None)[0] == {"tbl_id": "DT_A"}
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: None)
    assert ask_session.repick_target(res, None) is None


def test_wrong_cells_are_the_cells_the_answer_used() -> None:
    res = _res(
        trace={
            "dynamic": [
                {"tbl_id": "T1", "cells": "성별=계", "status": "ok"},
                {"tbl_id": "T2", "cells": "성별=남자", "status": "ok"},
                {"tbl_id": "T1", "skip": "칸 못 고름"},
            ]
        }
    )
    assert ask_session.wrong_cells(res, "T1") == "성별=계"
    assert ask_session.wrong_cells(res, "T3") is None


# ---------------------------------------------------------------- 행동 고르기


def test_missing_condition_with_cells_goes_repick_then_retable() -> None:
    a = agent_action.choose(VERDICT, _res(), _found("T1"), cells=True)
    assert (a.name, a.fallback, a.detail) == (
        agent_action.REPICK, agent_action.RETABLE, {"llm": True},
    )  # fmt: skip


def test_missing_condition_without_cells_keeps_replan() -> None:
    a = agent_action.choose(VERDICT, _res(), _found("T1"), cells=False)
    assert a.name == agent_action.REPLAN


def test_target_reason_with_cells_goes_repick() -> None:
    verdict = {**VERDICT, "reason": "대상이 다름"}
    a = agent_action.choose(verdict, _res(), _found("T1"), cells=True)
    assert a.name == agent_action.REPICK


def test_dictionary_repick_falls_back_to_tables() -> None:
    res = {"parsed": {"indicator": "총인구"}, "trace": {}}
    a = agent_action.choose(VERDICT, res, None, cells=True)
    assert (a.name, a.fallback) == (agent_action.REPICK, agent_action.REROUTE)


def test_repick_recell_is_counted_apart_from_replan() -> None:
    """repick 의 recell 은 replan 한도와 따로 센다: replan 을 이미 했어도 recell 까지 한다"""
    a = agent_action.choose(VERDICT, _res(), _found("T1"), llm_tried=1, cells=True)
    assert a.name == agent_action.REPICK and a.detail == {"llm": True}


def test_blocked_repick_is_not_chosen_even_by_the_old_rule() -> None:
    a = agent_action.choose(VERDICT, _res(), _found("T1"), cells=True, repick_ok=False)
    assert a.name != agent_action.REPICK
    res = {**_res(), "trace": {"dynamic": [{"tbl_id": "T1", "skip": "맞는 칸 없음"}]}}
    found = {
        "handoff": {
            "targets": [
                {
                    "candidates": [
                        {
                            "tbl_id": "T1",
                            "picked": True,
                            "checks": {"year": True},
                            "review": {"match": "exact"},
                        }
                    ]
                }
            ]
        }
    }
    target = {**VERDICT, "reason": "대상이 다름"}
    assert agent_action.choose(target, res, found).name == agent_action.REPICK
    assert agent_action.choose(target, res, found, repick_ok=False).name != agent_action.REPICK


def test_period_and_shape_reasons_do_not_repick() -> None:
    for reason in ("기간이 다름", "형태가 다름", "계산이 빠짐"):
        a = agent_action.choose({**VERDICT, "reason": reason}, _res(), _found("T1"), cells=True)
        assert a.name != agent_action.REPICK


# ---------------------------------------------------------------- recell 에 판정 지적

SEX = [
    Dim(0, "항목", {"T10": "인구"}),
    Dim(1, "성별", {"0": "계", "1": "남자", "2": "여자"}),
    Dim(2, "행정구역별", {"00": "전국"}),
]


def test_hint_becomes_a_condition_when_binding_bound_everything() -> None:
    b = Binding("DT_S", bound=[Bound("성별", "계", "total", 1, ["0"], ["계"])])
    assert recell.conditions(b, SEX, None) == []
    conds = recell.conditions(b, SEX, None, VERDICT)
    assert [(u.cond, u.value) for u in conds] == [(recell.JUDGE, VERDICT["missing"])]


def test_hint_lines_carry_reason_and_wrong_cells() -> None:
    b = Binding("DT_S", bound=[Bound("성별", "계", "total", 1, ["0"], ["계"])])
    hint = {**VERDICT, "wrong": "성별=계"}
    conds = recell.conditions(b, SEX, None, hint)
    text = recell.user_text("질문", "표", b, conds, SEX, 2, hint)
    assert "앞 답의 판정: 조건이 빠짐 — 여자만 물었는데 전체" in text
    assert "앞 답이 쓴 칸: 성별=계" in text
    assert "앞 답" not in recell.user_text("질문", "표", b, conds, SEX, 2)


def test_judge_pick_replaces_the_cell_the_wrong_answer_used() -> None:
    b = Binding("DT_S", bound=[Bound("word", "남자", "value", 1, ["1"], ["남자"])])
    conds = recell.conditions(b, SEX, None, VERDICT)
    raw = {"picks": [{"no": 1, "how": "칸", "dim": 1, "codes": ["2"], "broader": False}]}
    fix = recell.apply(b, conds, raw, SEX, 2)
    assert fix is not None and fix.binding.picks() == {1: ["2"]}


# ---------------------------------------------------------------- 표 하나로 다시 계산


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> dict:
    """표 하나짜리 계산 길: 칸 잇기 결과 · _try 가 쓰는 칸 · recell 결과를 시험마다 정한다"""
    box: dict = {"bind": None, "cells": [], "recell": None, "recell_calls": []}

    monkeypatch.setattr(binding, "bind", lambda *a, **k: box["bind"])
    monkeypatch.setattr(di, "schema", lambda tbl_id: (SEX, 2, ""))

    async def try_(tbl_id, org_id, question, parsed, sub, forced, *a, **k):
        cells = box["cells"].pop(0)
        sub["dynamic"].append({"tbl_id": tbl_id, "cells": cells, "status": "ok"})
        return {"status": "ok", "result": {"value": 7}, "answer": cells}, None

    async def fix(question, tbl_nm, b, topic, step, hint=None):
        box["recell_calls"].append(hint)
        step["recell"] = {"conds": ["judge"]}
        return box["recell"]

    monkeypatch.setattr(di, "_try", try_)
    monkeypatch.setattr(recell, "fix", fix)
    monkeypatch.setattr(di, "_recent_floor", lambda: None)
    monkeypatch.setattr(di.qa, "_leaves", lambda r: [r])
    return box


def _repick(llm: bool = True, wrong: str = "성별=계") -> tuple[dict | None, dict]:
    trace: dict = {}
    repick = {"entry": {"tbl_id": "DT_S"}, "hint": {**VERDICT, "wrong": wrong}, "llm": llm}
    found = {"handoff": {"understanding": {}}}
    parsed = {"type": "value", "indicator": "x"}
    out = asyncio.run(di.compute_from_handoff("질문", parsed, found, trace, repick=repick))
    return out, trace


def _bound(*codes: str) -> Binding:
    return Binding("DT_S", bound=[Bound("성별", "", "value", 1, list(codes), [])])


def test_binding_with_new_cells_answers_without_recell(table: dict) -> None:
    table["bind"], table["cells"] = _bound("2"), ["성별=여자"]
    out, trace = _repick()
    assert out["answer"] == "성별=여자"
    assert trace["repick"]["binding"] == "ok" and table["recell_calls"] == []
    assert trace["handoff"]["used"] == {"tbl_id": "DT_S", "kind": "repick"}


def test_same_cells_go_to_recell_with_the_hint(table: dict) -> None:
    table["bind"], table["cells"] = _bound("0"), ["성별=계", "성별=여자"]
    table["recell"] = recell.Fix(_bound("2"), "질문의 '여자만'을 표의 '여자'로 보고 계산했습니다")
    out, trace = _repick()
    assert out["answer"] == "성별=여자"
    assert trace["repick"]["binding"] == "앞 답과 같은 칸"
    assert trace["repick"]["recell"] == "ok"
    assert table["recell_calls"][0]["wrong"] == "성별=계"


def test_recell_that_lands_on_the_same_cells_fails(table: dict) -> None:
    table["bind"], table["cells"] = _bound("0"), ["성별=계", "성별=계"]
    table["recell"] = recell.Fix(_bound("0"), None)
    out, trace = _repick()
    assert out is None and trace["repick"]["recell"] == "앞 답과 같은 칸"


def test_same_value_as_the_first_answer_counts_as_the_same_cells(table: dict) -> None:
    """사전 답은 칸 기록이 없다: 칸 잇기가 같은 값을 다시 내면 새 답이 아니다"""
    table["bind"], table["cells"] = _bound("0"), ["성별=계"]
    trace: dict = {}
    repick = {
        "entry": {"tbl_id": "DT_S"},
        "hint": VERDICT,
        "llm": False,
        "same": lambda result: result.get("value") == 7,
    }
    found = {"handoff": {"understanding": {}}}
    out = asyncio.run(di.compute_from_handoff("질문", {"type": "value"}, found, trace, repick))
    assert out is None and trace["repick"]["binding"] == "앞 답과 같은 값"


def test_no_llm_left_stops_after_binding(table: dict) -> None:
    table["bind"], table["cells"] = _bound("0"), ["성별=계"]
    out, trace = _repick(llm=False)
    assert out is None and table["recell_calls"] == []
    assert trace["repick"]["recell"] == "LLM 상한"


def test_repick_does_not_spend_the_table_path_recell_budget(table: dict) -> None:
    di._recell_budget.set([0])  # 표 경로가 질문당 recell 을 이미 썼다
    table["bind"], table["cells"] = _bound("0"), ["성별=계", "성별=여자"]
    table["recell"] = recell.Fix(_bound("2"), None)
    out, _ = _repick()
    assert out is not None


def test_binding_that_leaves_only_cell_choices_asks_back(table: dict) -> None:
    """칸을 다시 고르지 못했고 남은 조건이 칸 고르기뿐이면 되묻기 답을 낸다"""
    from app.services.binding import Unbound

    table["bind"] = Binding(
        "DT_S",
        unbound=[
            Unbound(
                "성별", "", "합계 칸이 없음 (되묻기)", 1, "성별", [("1", "남자"), ("2", "여자")]
            )
        ],
    )
    out, trace = _repick(llm=False)
    assert out["clarification"] and trace["repick"]["ask"] == "성별"
    assert "used" not in trace["handoff"]


# -------------------------------------------------------- 거르기: 여러 지표 · 계산 · 근거 칸


def test_multi_indicator_and_composite_answers_are_blocked() -> None:
    split = _res(result={"status": "ok", "split_by": "indicator", "split": []})
    assert ask_session.repick_blocked(split, VERDICT) == "여러 지표 · 복합 답"
    composite = _res(parsed={"indicator": "x", "composite": True})
    assert ask_session.repick_blocked(composite, VERDICT)
    two = _res(parsed={"indicator": "x", "indicators": ["a", "b"]})
    assert ask_session.repick_blocked(two, VERDICT)
    assert ask_session.repick_blocked(_res(), VERDICT) is None


BIRTH = [
    Dim(0, "항목", {"T1": "출생아수(명)", "T2": "출생성비(명)", "T3": "혼인건수 (건)"}),
    Dim(1, "성별", {"0": "계", "1": "남자", "2": "여자"}),
    Dim(2, "행정구역별", {"00": "전국"}),
]


@pytest.fixture
def birth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(di, "schema", lambda tbl_id: (BIRTH, 2, ""))
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: None)


def _cells(res: dict, missing: str) -> dict:
    verdict = {**VERDICT, "reason": "대상이 다름", "missing": missing}
    return asyncio.run(ask_session.repick_cells(res, verdict, None))


def test_cell_named_in_the_hint_is_evidence(birth) -> None:
    res = _res(trace={"dynamic": [{"tbl_id": "T1", "cells": "항목 출생아수(명)", "status": "ok"}]})
    out = _cells(res, "출생아 수가 아니라 출생성비를 써야 함")
    assert out["cells"] is True and out["hits"] == ["출생성비(명)"]


def test_cells_the_wrong_answer_used_are_not_evidence(birth) -> None:
    res = _res(trace={"dynamic": [{"tbl_id": "T1", "cells": "출생아수(명) · 계", "status": "ok"}]})
    out = _cells(res, "여자를 물었는데 출생아수 전체로 답함")
    assert out["hits"] == ["여자"]
    out = _cells(res, "출생아수(명)만 씀")
    assert out["cells"] is False and out["hits"] == []


def test_dictionary_item_and_topic_cells_are_not_evidence(
    birth, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = {"sources": [{"table": "T1", "item_id": "T3"}]}
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: spec)
    res = _res(parsed={"indicator": "출생아수"})
    out = _cells(res, "혼인건수와 출생아수가 아님")
    assert out["cells"] is False and out["hits"] == []


def test_single_cell_dimension_is_not_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    one = [Dim(0, "항목", {"T1": "혼인"}), *BIRTH[1:]]
    monkeypatch.setattr(di, "schema", lambda tbl_id: (one, 2, ""))
    monkeypatch.setattr(ask_session.indicators, "spec", lambda name: None)
    out = _cells(_res(), "결혼한 사람 수를 물었으나 혼인 건수로 답함")
    assert out["cells"] is False and out["hits"] == []


def test_hint_that_needs_a_calculation_blocks_repick(birth) -> None:
    out = _cells(_res(), "혼인건수를 출생아로 나눈 비율을 구해야 함")
    assert out["cells"] is False and out["blocked"].startswith("계산이 필요한 지적")


def test_calculation_word_that_names_a_cell_is_not_blocked(birth) -> None:
    out = _cells(_res(), "출생 성비를 써야 함")
    assert out["cells"] is True and out["blocked"] is None


# -------------------------------------------------------- 판정 고리: repick recell 과 replan


def test_repick_recell_does_not_use_up_replan(monkeypatch: pytest.MonkeyPatch) -> None:
    """repick 이 recell 까지 하고 실패해도 그 뒤 replan(Gemma)을 쓸 수 있다"""
    seen: list[str] = []

    async def cells(res, verdict, found):
        return {"cells": True, "blocked": None, "tbl_id": "T1", "by": "시험"}

    async def repick(res, verdict, asked, llm_ok=True):
        seen.append("repick")
        res["trace"]["answer_judge"]["repick"] = {"tbl_id": "T1", "llm": True}
        return None, "같은 표에서 칸 다시"

    async def recompute(res, state, asked, user_id):
        seen.append("recompute")
        return None, "다음 후보 표"

    async def replan(res, verdict, asked, user_id):
        seen.append("replan")
        return None, "계획 다시"

    async def verdict(question, parsed, result):
        return VERDICT

    monkeypatch.setattr(answer_judge, "enabled", lambda: True)
    monkeypatch.setattr(answer_judge, "verdict", verdict)
    monkeypatch.setattr(ask_session, "repick_cells", cells)
    monkeypatch.setattr(ask_session, "_repick_compute", repick)
    monkeypatch.setattr(ask_session, "_recompute", recompute)
    monkeypatch.setattr(ask_session, "_replan_compute", replan)
    res = {**_res(), "request_type": ask_session.NEW, "answer": "처음 답"}
    asyncio.run(ask_session._judged(res, None, "q", None))
    assert seen[:3] == ["repick", "recompute", "replan"]
    record = res["trace"]["answer_judge"]
    assert record["repick_cells"][0]["cells"] is True
    assert record["attempts"][0]["action"]["action"] == agent_action.REPICK


def test_blocked_evidence_keeps_repick_out_of_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def cells(res, verdict, found):
        return {"cells": False, "blocked": "여러 지표 · 복합 답", "by": "여러 지표 · 복합 답"}

    async def repick(res, verdict, asked, llm_ok=True):
        seen.append("repick")
        return None, "같은 표에서 칸 다시"

    async def other(*a, **k):
        seen.append("other")
        return None, "다른 행동"

    async def verdict(question, parsed, result):
        return {**VERDICT, "reason": "대상이 다름"}

    monkeypatch.setattr(answer_judge, "enabled", lambda: True)
    monkeypatch.setattr(answer_judge, "verdict", verdict)
    monkeypatch.setattr(ask_session, "repick_cells", cells)
    monkeypatch.setattr(ask_session, "_repick_compute", repick)
    monkeypatch.setattr(ask_session, "_recompute", other)
    monkeypatch.setattr(ask_session, "_replan_compute", other)
    res = {**_res(), "request_type": ask_session.NEW, "answer": "처음 답"}
    asyncio.run(ask_session._judged(res, None, "q", None))
    assert "repick" not in seen and seen
