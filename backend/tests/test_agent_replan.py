"""재계획(agent/replan.py): 처음 계획 만들기 · LLM 출력 풀기(번호 → span, 종류 잠금, 같은 계획) ·
실행 가능 여부. 문장은 평가셋과 다르게"""

import asyncio

from app.agent import multi, replan

FIRST = {
    "shared": {"region_level": "시도"},
    "indicators": [
        {"span": "노인 인구", "search": "시도별 연령별 인구", "scope": "in", "role": "measure"}
    ],
    "combine": {"op": "none"},
}


def test_first_plan_comes_from_handoff_plan_or_parsed() -> None:
    found = {"handoff": {"plan": {**FIRST, "given": False}}}
    p = replan.first_plan_of(found, {"indicator": "x"})
    assert p["indicators"][0]["search"] == "시도별 연령별 인구" and p["combine"] == {"op": "none"}
    # 사전 경로(꾸러미 없음): 해석의 지표 하나로 최소 계획
    q = {"indicator": "출생아 수", "year": 2024, "sex": "여", "age": {"expr": "30대"}}
    p = replan.first_plan_of(None, q)
    ind = p["indicators"][0]
    assert (ind["span"], ind["search"], ind["dict"]) == ("출생아 수",) * 3
    assert (ind["scope"], ind["role"]) == ("in", "measure")
    assert p["shared"] == {"year_from": 2024, "year_to": 2024, "sex": "여", "age": "30대"}


def test_resolve_turns_index_num_den_into_spans_and_keeps_which_larger() -> None:
    new = {
        "measure": "ratio",
        "changed": "combine",
        "shared": {"region_level": "시도"},
        "indicators": [
            {"span": "노인 인구", "search": "시도별 연령별 인구", "scope": "in"},
            {"span": "어린이 인구", "search": "시도별 연령별 인구", "scope": "in"},
        ],
        "combine": {"op": "ratio", "num": 0, "den": 1, "scale": 1},
    }
    p, note = replan.resolve(new, "어느 시도 노인이 어린이의 몇 배", FIRST, "ratio")
    assert p is not None and (p["combine"]["num"], p["combine"]["den"]) == (
        "노인 인구",
        "어린이 인구",
    )
    new["combine"] = {"op": "which_larger", "num": 7, "den": None, "pair": "indicator"}
    new["measure"] = "count"
    p, _ = replan.resolve(new, "노인이 많아 어린이가 많아", FIRST, "count")
    assert p["combine"]["op"] == "which_larger" and p["combine"]["pair"] == "indicator"
    assert p["combine"]["num"] is None  # 범위 밖 번호는 버린다


def test_resolve_rejects_kind_change_and_same_plan() -> None:
    new = {"measure": "count", "changed": "combine", **FIRST}
    assert replan.resolve(new, "q", FIRST, "ratio") == (
        None,
        "묻는 값의 종류가 바뀜 (ratio → count)",
    )
    assert replan.resolve(new, "q", FIRST, "count")[0] is None  # 처음 계획과 같음


def test_runnable_blocks_only_empty_plans() -> None:
    assert replan.runnable({**FIRST, "combine": {"op": "sum", "over": "indicator"}}) is None
    assert (
        replan.runnable({**FIRST, "combine": {"op": "sum", "over": "month"}}) is None
    )  # 월 합도 코드가
    assert replan.runnable({**FIRST, "combine": {"op": "not_computable"}}) is None  # 사유로 끝낸다
    assert replan.runnable({**FIRST, "combine": {"op": "ratio"}}) is None
    assert replan.runnable({**FIRST, "indicators": []}) is not None


def test_apply_to_parsed_moves_single_indicator_combine_into_the_dictionary_fields() -> None:
    base = {"indicator": "총인구", "type": "value", "regions": ["서울특별시"]}
    p = {**FIRST, "combine": {"op": "rank", "within": "서울특별시"}}
    out = replan.apply_to_parsed(p, base)
    assert (out["type"], out["sido"], out["level"]) == ("rank", "서울특별시", "시군구")
    out = replan.apply_to_parsed({**FIRST, "combine": {"op": "rank", "axis": "국적"}}, base)
    assert out["rank_axis"] == "국적" and "sido" not in out
    out = replan.apply_to_parsed({**FIRST, "combine": {"op": "sum", "over": "region"}}, base)
    assert out["aggregate"] == "sum"
    # 지표가 둘이면 multi_indicator 가 combine 을 직접 읽으므로 해석은 그대로
    two = {**FIRST, "indicators": FIRST["indicators"] * 2}
    two["combine"] = {"op": "sum", "over": "indicator"}
    assert replan.apply_to_parsed(two, base) == base


def test_user_text_opens_search_only_for_target_reason() -> None:
    v = {"reason": "계산이 빠짐", "missing": "둘을 더해야 함"}
    text = replan.user_text("q", FIRST, v, "count", ["T1"])
    assert "고쳐도 되는 칸: combine, indicators" in text and "시도한 표: T1" in text
    text = replan.user_text("q", FIRST, {"reason": "대상이 다름", "missing": ""}, "count", [])
    assert "indicators[].search, combine" in text and "시도한 표" not in text


def test_multi_plan_node_skips_llm_when_plan_is_given() -> None:
    out = asyncio.run(multi.plan({"question": "q", "plan": FIRST}))
    assert out["plan"]["_given"] is True and out["plan"]["indicators"] == FIRST["indicators"]


def test_apply_to_parsed_reads_month_range_for_month_sums() -> None:
    base = {"indicator": "사망자 수", "type": "value"}
    p = {**FIRST, "combine": {"op": "sum", "over": "month"}}
    out = replan.apply_to_parsed(p, base, "올해 1월부터 3월까지 돌아가신 분 다 합치면")
    assert out["months"] == [1, 3]
    assert "months" not in replan.apply_to_parsed(
        p, base, "올해 돌아가신 분"
    )  # 달 표현이 없으면 그대로
    assert replan.runnable(p) is None  # 월 합은 이제 코드가 계산한다
