"""행동 고르기 규칙 (docs/agent_redesign_2026-11-26.md 3-3): 판정 · 실패 이유 × 검사 → 행동.
행동 넷 repick(칸) → retable(표) → replan(계획) → give_up, 좁은 것부터. 문장은 평가셋과 다르게"""

import pytest

from app.services import agent_action as aa


def _v(reason: str) -> dict:
    return {"reason": reason, "missing": "x", "verdict": "retry"}


def _pick(reason: str, **kw) -> tuple[str, str | None]:
    a = aa.choose(_v(reason), **kw)
    return a.name, a.fallback


# ------------------------------------------------------------------ 3-3 표 한 줄씩


def test_missing_condition_with_cells_repicks_then_retables() -> None:
    assert _pick(aa.MISSING, cells=True) == (aa.REPICK, aa.RETABLE)


def test_missing_condition_without_cells_retables_then_replans() -> None:
    assert _pick(aa.MISSING) == (aa.RETABLE, aa.REPLAN)


def test_target_with_cells_and_fit_repicks() -> None:
    assert _pick(aa.TARGET, cells=True, fits=True) == (aa.REPICK, aa.RETABLE)


def test_target_without_fit_or_cells_retables_then_replans() -> None:
    assert _pick(aa.TARGET, cells=True, fits=False) == (aa.RETABLE, aa.REPLAN)
    assert _pick(aa.TARGET) == (aa.RETABLE, aa.REPLAN)


@pytest.mark.parametrize("reason", aa.PLAN_REASONS)
def test_plan_reasons_replan_then_retable(reason: str) -> None:
    assert _pick(reason, cells=True) == (aa.REPLAN, aa.RETABLE)


def test_period_reason_marks_timeref_rerun() -> None:
    assert aa.choose(_v("기간이 다름")).detail == {"rerun_timeref": True}


def test_no_value_cells_follow_the_cell_check() -> None:
    assert _pick(aa.NO_VALUE_CELLS, cells=True) == (aa.REPICK, aa.RETABLE)
    assert _pick(aa.NO_VALUE_CELLS) == (aa.RETABLE, aa.REPLAN)


def test_no_value_all_checks_failed_replans_then_gives_up() -> None:
    assert _pick(aa.NO_VALUE_CHECKS, cells=True) == (aa.REPLAN, None)
    assert _pick(aa.NO_VALUE_CHECKS, done=frozenset({aa.REPLAN}))[0] == aa.GIVE_UP


def test_unanswerable_and_limit_give_up() -> None:
    assert _pick(aa.UNANSWERABLE)[0] == aa.GIVE_UP
    assert _pick(aa.MISSING, tried=aa.MAX_RETRIES)[0] == aa.GIVE_UP


def test_unknown_reason_starts_from_tables() -> None:
    assert _pick("없음") == (aa.RETABLE, aa.REPLAN)


# ------------------------------------------------------------------ 사다리 · 한도


def test_target_again_after_retable_goes_replan() -> None:
    """retable 뒤에도 대상이 다름 → replan (3-3 끝에서 둘째 줄)"""
    done = frozenset({aa.RETABLE})
    assert _pick(aa.TARGET, done=done, tried=1) == (aa.REPLAN, None)


def test_after_repick_and_retable_the_ladder_reaches_replan() -> None:
    done = frozenset({aa.REPICK, aa.RETABLE})
    assert _pick(aa.MISSING, cells=True, done=done, tried=1)[0] == aa.REPLAN


def test_replan_limit_skips_replan() -> None:
    assert _pick(aa.PLAN_REASONS[0], llm_tried=1) == (aa.RETABLE, None)
    done = frozenset({aa.RETABLE})
    assert _pick(aa.TARGET, done=done, llm_tried=1)[0] == aa.GIVE_UP


def test_repick_is_never_chosen_without_cells_or_when_blocked() -> None:
    assert _pick(aa.MISSING, cells=True, repick_ok=False)[0] == aa.RETABLE
    for reason in (*aa.PLAN_REASONS, aa.NO_VALUE_CHECKS, "없음"):
        assert _pick(reason, cells=True)[0] != aa.REPICK


def test_repick_recell_is_separate_from_the_replan_limit() -> None:
    a = aa.choose(_v(aa.MISSING), llm_tried=1, cells=True)
    assert (a.name, a.detail) == (aa.REPICK, {"llm": True})
    assert a.fallback == aa.RETABLE


def test_trace_shape_and_helpers() -> None:
    a = aa.choose(_v(aa.MISSING), cells=True)
    assert a.as_trace()["action"] == aa.REPICK and a.as_trace()["fallback"] == aa.RETABLE
    found = {
        "handoff": {"targets": [{"candidates": [{"tbl_id": "A"}, {"tbl_id": "B", "picked": True}]}]}
    }
    assert aa.picked_candidate(found)["tbl_id"] == "B"
    assert aa.picked_candidate({"handoff": {"targets": [{}, {}]}}) is None
    assert aa.failed_at_cell({"dynamic": [{"skip": "질문의 말과 맞는 칸이 없음"}]})
    assert not aa.failed_at_cell({"dynamic": [{"skip": "적재 안 됨"}]})
    assert set(aa.ACTIONS) == {aa.REPICK, aa.RETABLE, aa.REPLAN, aa.GIVE_UP}
