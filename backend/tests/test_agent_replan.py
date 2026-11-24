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


def test_which_larger_with_one_indicator_moves_pair_into_parsed() -> None:
    base = {"indicator": "이혼 건수", "type": "value", "regions": ["부산광역시"], "year": 2025}
    refs = [
        {"expr": "재작년", "kind": "word", "year": 2024},
        {"expr": "작년", "kind": "word", "year": 2025},
    ]
    p = {**FIRST, "combine": {"op": "which_larger", "pair": "year"}}
    out = replan.apply_to_parsed(p, {**base, "time_refs": refs}, "q")
    assert (out["type"], out["start_year"], out["end_year"]) == ("trend", 2024, 2025)
    p = {**FIRST, "combine": {"op": "which_larger", "pair": "sex"}}
    out = replan.apply_to_parsed(p, base, "q")
    assert out["split_sex"] is True and out["sex"] == "계" and out["type"] == "value"
    p = {**FIRST, "combine": {"op": "which_larger", "pair": "region"}}
    two = {**base, "regions": ["서울특별시", "부산광역시"]}
    assert replan.apply_to_parsed(p, two, "q")["type"] == "compare"
    # 재료가 없으면 계획을 돌리지 않는다 (연도 둘 · 지역 둘이 해석에 없음)
    year_plan = {**FIRST, "combine": {"op": "which_larger", "pair": "year"}}
    assert "두 시점" in replan.runnable(year_plan, base)
    assert "두 지역" in replan.runnable(p, base)
    assert replan.runnable(p, two) is None


def test_same_indicator_split_by_sex_marks_collapses_into_pair_sex() -> None:
    # LLM 이 "사망자 수(남)" · "사망자 수(여)" 두 지표 + pair=indicator 로 적어도
    # 지표 하나 + pair=sex 로
    new = {
        "measure": "count",
        "changed": "combine, indicators",
        "shared": {"year_from": 2025, "year_to": 2025},
        "indicators": [
            {"span": "통근 인구(남)", "search": "통근 인구", "scope": "in", "role": "measure"},
            {"span": "통근 인구(여)", "search": "통근 인구", "scope": "in", "role": "measure"},
        ],
        "combine": {"op": "which_larger", "pair": "indicator"},
    }
    p, _ = replan.resolve(new, "작년에 결혼한 남자가 많아 여자가 많아", FIRST, "count")
    assert p is not None and len(p["indicators"]) == 1 and p["indicators"][0]["span"] == "통근 인구"
    assert p["combine"]["pair"] == "sex" and p["combine"]["collapsed"] == [
        "통근 인구(남)",
        "통근 인구(여)",
    ]
    out = replan.apply_to_parsed(p, {"indicator": "통근 인구", "type": "value", "regions": []}, "q")
    assert out["split_sex"] is True
    # 시점 표시면 pair=year
    new["indicators"] = [
        {"span": "통근 인구(2024년)", "search": "통근 인구", "scope": "in", "role": "measure"},
        {"span": "통근 인구(2025년)", "search": "통근 인구", "scope": "in", "role": "measure"},
    ]
    p, _ = replan.resolve(new, "q", FIRST, "count")
    assert p["combine"]["pair"] == "year" and len(p["indicators"]) == 1


def test_pair_sex_needs_an_indicator_that_splits_by_sex(monkeypatch) -> None:
    from app.services import indicators

    specs = {
        "사망자 수": {"name": "사망자 수", "method": "lookup", "sources": [{"table": "A"}]},
        "총인구": {"name": "총인구", "method": "computed", "sources": [{"table": "B"}]},
    }
    monkeypatch.setattr(indicators, "spec", lambda name: specs.get(name))
    base = {"indicator": "x", "type": "value", "regions": ["전국"]}
    plan = {**FIRST, "combine": {"op": "which_larger", "pair": "sex"}}
    dead = {
        **plan,
        "indicators": [
            {"span": "사망자 수", "search": "사망자 수", "dict": "사망자 수", "scope": "in"}
        ],
    }
    assert "성별 자료가 없음" in replan.runnable(dead, base)
    pop = {
        **plan,
        "indicators": [{"span": "인구", "search": "인구", "dict": "총인구", "scope": "in"}],
    }
    assert replan.runnable(pop, base) is None
