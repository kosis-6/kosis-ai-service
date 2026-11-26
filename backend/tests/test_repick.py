"""repick: 표는 맞는데 칸이 틀린 답을 같은 표에서 칸만 다시 고른다
(docs/agent_redesign_2026-11-26.md).
1단계: _retry 가 repick 을 _repick_compute 로 보내고, 다시 고르지 못하면 대체 행동으로 간다."""

import asyncio

import pytest

from app.services import agent_action, ask_session

VERDICT = {"reason": "조건이 빠짐", "missing": "여자만 물었는데 전체", "verdict": "retry"}


def _res() -> dict:
    return {
        "status": "ok",
        "parsed": {"indicator": "x", "dynamic_spec": {"name": "x"}},
        "result": {"status": "ok", "value": 1},
        "sources": [{"tbl_id": "T1"}],
    }


def _patch(monkeypatch: pytest.MonkeyPatch, repick_out) -> list[str]:
    calls: list[str] = []

    async def repick(res, verdict, asked):
        calls.append("repick")
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
    assert calls == ["repick"]
    assert again["answer"] == "같은 표 여자 칸" and how == "같은 표에서 칸 다시"
    assert done == set()


def test_repick_failure_falls_back_and_marks_it_done(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, None)
    done: set[str] = set()
    action = agent_action.Action(agent_action.REPICK, "시험", fallback=agent_action.RETABLE)
    again, how = _retry(action, done)
    assert calls == ["repick", "recompute"]
    assert how == "다음 후보 표" and done == {agent_action.RETABLE}


def test_repick_without_fallback_ends_there(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, None)
    again, how = _retry(agent_action.Action(agent_action.REPICK, "시험"), set())
    assert calls == ["repick"] and again is None and how == "같은 표에서 칸 다시"


def test_repick_compute_records_why_it_did_not_run() -> None:
    res = _res()
    again, how = asyncio.run(ask_session._repick_compute(res, VERDICT, "q"))
    assert again is None and how == "같은 표에서 칸 다시"
    assert "repick" in res["trace"]["answer_judge"]
