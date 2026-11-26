"""답 판정 고리의 끝 처리 (ask_session._loop_end, 11/25): 고리가 답을 바꾸지 못하고 끝났을 때
경우 1 값은 냈는데 판정이 거절 → 처음 답 + 주의 한 줄
경우 2 어느 시도도 값을 못 냄  → 답변 불가 (마지막 실패 사유, 없으면 partial_match)
경우 3 다시 계산이 되묻기를 냄 → 그 되묻기를 전달
"""

import asyncio

import pytest

from app.services import agent_action, answer_judge, ask_session, dynamic_indicator, flow, refusal

RETRY = {"reason": "대상이 다름", "missing": "다른 대상을 봄", "verdict": "retry"}
OK = {"reason": "없음", "missing": "", "verdict": "ok"}


def _first_answer() -> dict:
    return {
        "status": "ok",
        "answer": "2025년 서울 인구는 933만 명입니다.",
        "parsed": {"indicator": "총인구", "dynamic_spec": None},
        "result": {"status": "ok", "value": 9330000},
        "sources": [{"tbl_id": "DT_1B04006"}],
        "request_type": ask_session.NEW,
        "question": "서울 인구",
    }


def _actions(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """행동 고르기를 정해진 순서로 (마지막은 give_up)"""
    seq = iter([*names, agent_action.GIVE_UP])

    def choose(verdict, res, found, tried, llm_tried, done):
        return agent_action.Action(next(seq), "시험")

    monkeypatch.setattr(agent_action, "choose", choose)


def _judge(monkeypatch: pytest.MonkeyPatch, *verdicts: dict) -> None:
    seq = iter(verdicts)

    async def verdict(question, parsed, result):
        return next(seq)

    monkeypatch.setattr(answer_judge, "enabled", lambda: True)
    monkeypatch.setattr(answer_judge, "verdict", verdict)


def _retry(monkeypatch: pytest.MonkeyPatch, *outcomes) -> None:
    """다시 계산 결과를 순서대로. (답, 방법, outcome): outcome 은 _retry_outcome 에 둔다"""
    seq = iter(outcomes)

    async def retry(action, res, verdict, state, asked, user_id, done=None):
        again, how, outcome = next(seq)
        ask_session._retry_outcome.set(outcome)
        return again, how

    monkeypatch.setattr(ask_session, "_retry", retry)


def _run(res: dict) -> dict:
    flow.start()
    asyncio.run(ask_session._judged(res, None, res["question"], None))
    return res


def test_case2_no_value_from_any_retry_becomes_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, RETRY)
    _actions(monkeypatch, agent_action.RETABLE, agent_action.REPLAN)
    _retry(
        monkeypatch,
        (None, "다음 후보 표", {"status": "band_mismatch", "clarification": None}),
        (None, "재계획", {"status": "no_region_level", "clarification": None}),
    )
    res = _run(_first_answer())
    assert res["status"] == "no_region_level"  # 마지막 실패 사유가 답변 불가 코드이면 그것
    assert res["answer"] == refusal.TEMPLATES["no_region_level"]
    assert "933만" not in res["answer"]
    assert res["request_type"] == ask_session.NEW and res["question"] == "서울 인구"
    end = res["trace"]["answer_judge"]["end"]
    assert end["case"] == "답변 불가" and end["how"] == "give_up"


def test_case2_unknown_failure_code_falls_to_partial_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, RETRY)
    _actions(monkeypatch, agent_action.RETABLE)
    _retry(monkeypatch, (None, "다음 후보 표", {"status": "band_mismatch", "clarification": None}))
    res = _run(_first_answer())
    assert res["status"] == "partial_match"
    assert res["trace"]["answer_judge"]["end"]["last_status"] == "band_mismatch"


def test_case2_without_any_failure_code_is_partial_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, RETRY)
    _actions(monkeypatch, agent_action.RETABLE)
    _retry(monkeypatch, (None, "다음 후보 표", None))
    res = _run(_first_answer())
    assert res["status"] == "partial_match"


def test_case1_keeps_first_answer_with_caution(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, RETRY, RETRY)  # 처음 판정 · 다시 한 답의 판정
    _actions(monkeypatch, agent_action.RETABLE)
    again = {**_first_answer(), "answer": "2025년 서울 인구는 100만 명입니다.", "trace": {}}
    _retry(monkeypatch, (again, "다음 후보 표", None))
    res = _run(_first_answer())
    assert res["status"] == "ok"
    assert res["answer"].startswith("2025년 서울 인구는 933만 명입니다.")
    assert res["answer"].endswith(ask_session.CAUTION)
    assert res["answer"].count(ask_session.CAUTION) == 1
    assert res["trace"]["answer_judge"]["end"]["case"] == "처음 답 + 주의"


def test_case1_mixed_value_and_failure_keeps_first_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 시도는 값을 냈고(거절) 다른 시도는 못 냈으면 — 값을 낸 적이 있으니 경우 1"""
    _judge(monkeypatch, RETRY, RETRY)
    _actions(monkeypatch, agent_action.RETABLE, agent_action.REPLAN)
    again = {**_first_answer(), "answer": "다른 값", "trace": {}}
    _retry(
        monkeypatch,
        (again, "다음 후보 표", None),
        (None, "재계획", {"status": "partial_match", "clarification": None}),
    )
    res = _run(_first_answer())
    assert res["status"] == "ok" and "933만" in res["answer"]
    assert res["answer"].endswith(ask_session.CAUTION)


def test_no_attempt_at_all_keeps_first_answer_with_caution(monkeypatch: pytest.MonkeyPatch) -> None:
    """첫 행동이 give_up 이면 값을 못 낸 것이 아니라 시도하지 않은 것 → 답변 불가로 바꾸지 않는다"""
    _judge(monkeypatch, RETRY)
    _actions(monkeypatch)
    res = _run(_first_answer())
    assert res["status"] == "ok" and "933만" in res["answer"]
    assert res["answer"].endswith(ask_session.CAUTION)


def test_case3_clarification_from_retry_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, RETRY)
    _actions(monkeypatch, agent_action.RETABLE, agent_action.REPLAN)
    asking = {
        "status": "clarify_question",
        "answer": "'표' 표에서 연령별 중 어느 것을 볼까요?",
        "parsed": {},
        "result": {"status": "clarify_question"},
        "sources": [],
        "trace": {},
        "question": "서울 인구",
        "clarification": {"kind": "table_cell", "prompt": "어느 것을?", "options": ["a", "b"]},
    }
    _retry(
        monkeypatch,
        (None, "다음 후보 표", {"status": "clarify_question", "clarification": asking}),
        (None, "재계획", {"status": "partial_match", "clarification": None}),
    )
    res = _run(_first_answer())
    assert res["status"] == "clarify_question"
    assert res["clarification"]["kind"] == "table_cell"
    assert res["request_type"] == ask_session.NEW
    assert res["trace"]["answer_judge"]["end"]["case"] == "되묻기"
    assert res["trace"]["answer_judge"]["attempts"][0]["clarification"] is True


def test_success_after_clarification_wins_over_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """되묻기가 나왔더라도 뒤 행동이 판정을 통과한 값을 내면 그 값으로 (되묻기는 버린다)"""
    _judge(monkeypatch, RETRY, OK)
    _actions(monkeypatch, agent_action.RETABLE, agent_action.REPLAN)
    asking = {"status": "clarify_question", "answer": "?", "clarification": {"kind": "table_cell"}}
    again = {**_first_answer(), "answer": "2025년 서울 인구는 940만 명입니다.", "trace": {}}

    async def no_cards(res, asked):
        return None

    monkeypatch.setattr(ask_session, "_attach_cards", no_cards)
    _retry(
        monkeypatch,
        (None, "다음 후보 표", {"status": "clarify_question", "clarification": asking}),
        (again, "재계획", None),
    )
    res = _run(_first_answer())
    assert res["status"] == "ok" and "940만" in res["answer"]
    assert ask_session.CAUTION not in res["answer"]


def test_ok_verdict_leaves_the_answer_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _judge(monkeypatch, OK)
    res = _run(_first_answer())
    assert res["answer"] == "2025년 서울 인구는 933만 명입니다."
    assert "end" not in res["trace"]["answer_judge"]


def test_recompute_reports_clarification_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_recompute 가 값을 못 내면 그 이유(status · 되묻기)를 _retry_outcome 에 둔다"""
    asking = {"status": "clarify_question", "clarification": {"kind": "table_cell"}}
    outs = iter([asking, {"status": "band_mismatch"}, None])

    async def compute(question, parsed, found, trace):
        return next(outs)

    monkeypatch.setattr(dynamic_indicator, "compute_from_handoff", compute)
    found = {"handoff": {"targets": [{"candidates": [{"tbl_id": "DT_1B04006"}, {"tbl_id": "B"}]}]}}
    res = {**_first_answer(), "parsed": {"indicator": "x", "dynamic_spec": {"name": "x"}}}

    # asyncio.run 은 컨텍스트를 복사하므로 outcome 은 같은 태스크 안에서 읽는다
    async def one(expected_status, expect_ask):
        ask_session._found_now.set(found)
        ask_session._retry_outcome.set(None)
        again, how = await ask_session._recompute(res, None, "q", None)
        out = ask_session._retry_outcome.get()
        assert again is None and how == "다음 후보 표"
        assert out["status"] == expected_status
        assert (out["clarification"] is not None) is expect_ask

    asyncio.run(one("clarify_question", True))
    asyncio.run(one("band_mismatch", False))
    asyncio.run(one(None, False))
