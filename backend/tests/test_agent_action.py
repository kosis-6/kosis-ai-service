"""행동 고르기 (N9) 규칙: 판정 이유 × 답의 경로 × 1위 후보 상태 → 행동. 문장은 평가셋과 다르게"""

from app.services import agent_action as aa


def _verdict(reason: str) -> dict:
    return {"reason": reason, "missing": "x", "verdict": "retry"}


def _found(checks: dict | None, match: str | None, picked: bool = True) -> dict:
    cand = {"tbl_id": "T1", "picked": picked, "checks": checks or {}}
    if match:
        cand["review"] = {"match": match}
    return {"handoff": {"targets": [{"candidates": [cand, {"tbl_id": "T2", "checks": {}}]}]}}


DICT_RES = {"parsed": {"indicator": "총인구"}, "trace": {}}
SEARCH_RES = {"parsed": {"indicator": "x", "dynamic_spec": {"name": "x"}}, "trace": {}}


def test_plan_reasons_go_to_replan_with_fallback_by_path() -> None:
    a = aa.choose(_verdict("형태가 다름"), SEARCH_RES, _found({"year": True}, "exact"))
    assert (a.name, a.fallback) == (aa.REPLAN, aa.RETABLE)
    b = aa.choose(_verdict("조건이 빠짐"), DICT_RES, None)
    assert (b.name, b.fallback) == (aa.REPLAN, aa.REROUTE)
    c = aa.choose(_verdict("계산이 빠짐"), DICT_RES, None)
    assert c.name == aa.REPLAN


def test_period_reason_reruns_timeref_before_replan() -> None:
    a = aa.choose(_verdict("기간이 다름"), SEARCH_RES, _found({}, None))
    assert a.name == aa.REPLAN and a.detail.get("rerun_timeref") is True


def test_target_reason_dictionary_path_reroutes_then_requeries() -> None:
    # 표 찾기를 아직 안 거쳤으면 경로부터 (지금 _recompute의 '통계표 찾기로')
    a = aa.choose(_verdict("대상이 다름"), DICT_RES, None)
    assert a.name == aa.REROUTE
    # 표 찾기까지 거쳤는데도 틀리면 후보에 정답이 없는 것 → 검색어 다시 (게이트 통과)
    b = aa.choose(_verdict("대상이 다름"), DICT_RES, _found({"year": True}, "partial"))
    assert (b.name, b.fallback) == (aa.REQUERY, aa.RETABLE)


def test_target_reason_search_path_sure_top_and_cell_failure_goes_recell() -> None:
    res = {
        **SEARCH_RES,
        "trace": {"dynamic": [{"tbl_id": "T1", "skip": "질문의 말과 맞는 칸이 없음"}]},
    }
    a = aa.choose(
        _verdict("대상이 다름"), res, _found({"year": True, "region_level": True}, "exact")
    )
    assert (a.name, a.fallback) == (aa.RECELL, aa.REPLAN)


def test_target_reason_search_path_unsure_top_goes_retable() -> None:
    # 1위에 ✘가 있거나 검토가 exact가 아니면 다음 후보 (11/22: 재검색하면 깨지던 경우)
    a = aa.choose(_verdict("대상이 다름"), SEARCH_RES, _found({"year": False}, "exact"))
    assert a.name == aa.RETABLE
    b = aa.choose(_verdict("대상이 다름"), SEARCH_RES, _found({"year": True}, "partial"))
    assert b.name == aa.RETABLE


def test_limits_turn_llm_actions_into_code_actions_then_give_up() -> None:
    a = aa.choose(_verdict("형태가 다름"), SEARCH_RES, _found({}, None), tried=1, llm_tried=1)
    assert a.name == aa.RETABLE  # LLM 행동 상한 → 표만
    b = aa.choose(_verdict("형태가 다름"), SEARCH_RES, _found({}, None), tried=2)
    assert b.name == aa.GIVE_UP  # 다시 계산 상한


def test_refuse_and_unknown_reasons() -> None:
    assert aa.choose(_verdict("답할 수 없는 질문"), DICT_RES, None).name == aa.GIVE_UP
    assert aa.choose(_verdict(""), DICT_RES, None).name == aa.REROUTE
    assert aa.choose(_verdict(""), SEARCH_RES, _found({}, None)).name == aa.RETABLE


def test_trace_shape_and_helpers() -> None:
    a = aa.choose(_verdict("형태가 다름"), DICT_RES, None)
    t = a.as_trace()
    assert t["action"] == aa.REPLAN and t["fallback"] == aa.REROUTE and "why" in t
    assert aa.picked_candidate({"handoff": {"targets": []}}) is None
    assert (
        aa.top_is_sure({"checks": {}, "review": {"match": "exact"}}) is False
    )  # 확인한 조건이 없으면 확실하지 않다


def test_done_actions_are_not_repeated_and_code_failure_leads_to_replan() -> None:
    # 코드 행동(reroute)을 썼는데도 틀렸다: 이유가 무엇이든 LLM 행동이 남았으면 계획 다시
    a = aa.choose(_verdict("대상이 다름"), DICT_RES, None, tried=1, done=frozenset({aa.REROUTE}))
    assert a.name == aa.REPLAN and a.fallback == aa.RETABLE
    # replan 까지 썼으면(LLM 상한) 남은 코드 행동으로, 다 썼으면 give_up
    b = aa.choose(
        _verdict("형태가 다름"), DICT_RES, None, tried=1, llm_tried=1,
        done=frozenset({aa.REPLAN, aa.REROUTE}),
    )  # fmt: skip
    assert b.name == aa.RETABLE
    c = aa.choose(
        _verdict("형태가 다름"), SEARCH_RES, _found({}, None), tried=1, llm_tried=1,
        done=frozenset({aa.REPLAN, aa.RETABLE, aa.REROUTE}),
    )  # fmt: skip
    assert c.name == aa.GIVE_UP
    # 상한(2회)은 done 보다 먼저
    assert aa.choose(_verdict("형태가 다름"), DICT_RES, None, tried=2).name == aa.GIVE_UP
