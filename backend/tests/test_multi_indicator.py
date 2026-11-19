"""지표가 여럿인 질문 계산 (multi_indicator.py, 구현 5): 지표마다 계산해 combine.op대로 합친다

지표 하나 계산(사전 · 캐시 · 넘기기 계산)은 가짜로 바꾸고 합치기만 본다. 예문은 일반 예문이다.
"""

import asyncio

import pytest

from app.services import dynamic_indicator as d
from app.services import indicators, multi_indicator, qa

BASE = {"type": "value", "regions": ["서울특별시"], "year": 2020, "sex": "계"}


def _target(span, role="measure", scope="in", cands=("T1",)):
    return {
        "span": span,
        "question": f"2020년 서울 {span}",
        "role": role,
        "scope": scope,
        "conditions": {},
        "together": [],
        "candidates": [{"tbl_id": c, "tbl_nm": f"{c} 표", "checks": {}} for c in cands],
    }


def _found(targets, op="none"):
    return {
        "status": "ok",
        "recommendations": [{"tbl_id": "T1", "tbl_nm": "T1 표"}],
        "handoff": {
            "status": "ok",
            "understanding": {"conditions": {}},
            "targets": targets,
            "combine": {"op": op, "note": None, "out_of_scope": []},
        },
    }


def _value(name, value, unit="가구", region="서울특별시", year=2020):
    result = {
        "status": "ok", "indicator": name, "kind": "count", "unit": unit, "value": value,
        "region": region, "year": year, "sex": "계", "source": f"{name} 표",
        "basis": f"{name} 기준", "tbl_id": f"T_{name}", "url": None, "notes": [],
    }  # fmt: skip
    return {"status": "ok", "answer": f"{name} {value}", "result": result, "parsed": {}}


def _rank(name, regions):
    rows = [{"region": r, "value": 100 - i, "rank": i + 1} for i, r in enumerate(regions)]
    result = {
        "status": "ok", "indicator": name, "kind": "count", "unit": "명", "year": 2020,
        "sex": "계", "ranking": rows, "order": "높은 순", "level": "시도",
        "total_regions": len(rows), "source": f"{name} 표", "basis": "기준",
        "tbl_id": f"T_{name}", "url": None, "notes": [],
    }  # fmt: skip
    return {"status": "ok", "result": result}


@pytest.fixture
def world(monkeypatch):
    """지표마다의 결과를 정해 둔다: state["values"][지표] = 답 또는 None (못 함)"""
    state: dict = {"values": {}, "asked": []}

    async def fake_target(question, parsed, found, target, trace, multi=False):
        state["asked"].append((target["span"], parsed["type"], tuple(parsed.get("regions") or [])))
        got = state["values"].get(target["span"])
        return got(parsed) if callable(got) else got

    async def no_cache(question, parsed, trace):
        return None

    monkeypatch.setattr(d, "_compute_target", fake_target)
    monkeypatch.setattr(d, "cached_answer", no_cache)
    monkeypatch.setattr(indicators, "spec", lambda name: None)  # 모두 사전에 없는 지표로
    monkeypatch.setattr(indicators, "canonical_name", lambda name: None)
    return state


def run(question, found, parsed=None):
    trace: dict = {}
    res = asyncio.run(multi_indicator.answer(question, parsed or BASE, found, trace))
    return res, trace


def test_several_indicators_side_by_side(world) -> None:
    world["values"] = {
        "1인 가구 수": _value("1인 가구 수", 100),
        "통근 인구": _value("통근 인구", 50, "명"),
    }
    res, trace = run(
        "2020년 서울 1인 가구 수와 통근 인구",
        _found([_target("1인 가구 수"), _target("통근 인구")]),
    )
    assert res["status"] == "ok" and res["result"]["split_by"] == "indicator"
    assert [p["indicator"] for p in res["result"]["split"]] == ["1인 가구 수", "통근 인구"]
    assert [t["status"] for t in trace["multi"]["targets"]] == ["ok", "ok"]


def test_partial_success_keeps_numbers_and_points_to_table(world) -> None:
    # 하나만 계산되면 그 숫자는 내고, 못 한 지표는 표 안내 한 줄
    world["values"] = {"1인 가구 수": _value("1인 가구 수", 100), "통근 인구": None}
    res, _ = run("질문", _found([_target("1인 가구 수"), _target("통근 인구", cands=("T9",))]))
    assert res["status"] == "ok" and res["result"]["value"] == 100
    assert "통근 인구는 통계표에서 바로 계산하지 못했습니다 ('T9 표'" in res["answer"]


def test_nothing_computed_is_none(world) -> None:
    res, _ = run("질문", _found([_target("가"), _target("나")]))
    assert res is None  # 지금처럼 표 안내


def test_out_of_scope_targets_are_not_computed(world) -> None:
    world["values"] = {"1인 가구 수": _value("1인 가구 수", 100)}
    run("질문", _found([_target("전세 가격", scope="out"), _target("1인 가구 수")]))
    assert [a[0] for a in world["asked"]] == ["1인 가구 수"]


def test_diff_subtracts_same_units(world) -> None:
    world["values"] = {"전입": _value("전입", 300, "명"), "전출": _value("전출", 120, "명")}
    res, _ = run("2020년 서울 전입과 전출 차이", _found([_target("전입"), _target("전출")], "diff"))
    assert res["result"]["value"] == 180 and res["result"]["op"] == "−"


def test_diff_with_different_units_is_side_by_side(world) -> None:
    world["values"] = {"혼인": _value("혼인", 300, "건"), "출생": _value("출생", 120, "명")}
    res, _ = run("질문", _found([_target("혼인"), _target("출생")], "diff"))
    assert res["result"]["split_by"] == "indicator"
    assert "직접 계산하지 않고 나란히" in res["answer"]


def test_ratio_later_indicator_is_numerator(world) -> None:
    # "1인 가구 중 노인 1인 가구 비율": 뒤에 말한 지표가 분자
    world["values"] = {
        "1인 가구": _value("1인 가구", 400),
        "노인 1인 가구": _value("노인 1인 가구", 100),
    }
    found = _found([_target("1인 가구"), _target("노인 1인 가구")], "ratio")
    res, trace = run("2020년 서울 1인 가구 중 노인 1인 가구 비율", found)
    assert res["result"]["value"] == 25.0 and res["result"]["unit"] == "%"
    assert trace["multi"]["ratio_by"] == "rule"


def _ratio_found(num=None, den=None):
    found = _found([_target("1인 가구 수"), _target("통근 인구")], "ratio")
    found["handoff"]["combine"].update(num=num, den=den)
    return found


@pytest.fixture
def two_counts(world):
    world["values"] = {
        "1인 가구 수": _value("1인 가구 수", 800),
        "통근 인구": _value("통근 인구", 200, "명"),
    }


@pytest.mark.usefixtures("two_counts")
def test_ratio_times_is_first_over_second() -> None:
    # "A는 B의 몇 배" = A ÷ B (11/18 전에는 늘 뒤 ÷ 앞이라 0.25배가 나왔다)
    res, trace = run("2020년 서울 1인 가구 수는 통근 인구의 몇 배야", _ratio_found())
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "rule"


@pytest.mark.usefixtures("two_counts")
def test_rule_wins_over_the_plan() -> None:
    # 계획(LLM)이 분자 · 분모를 거꾸로 적어도 문형 규칙이 확실하면 규칙대로
    found = _ratio_found(num="통근 인구", den="1인 가구 수")
    res, trace = run("2020년 서울 1인 가구 수는 통근 인구의 몇 배야", found)
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "rule"


@pytest.mark.usefixtures("two_counts")
def test_plan_decides_when_no_rule_matches() -> None:
    # 규칙이 모르는 말투 ("B에 비해 A는"): 계획의 분자 · 분모를 쓴다
    found = _ratio_found(num="1인 가구 수", den="통근 인구")
    res, trace = run("2020년 서울 통근 인구에 비해 1인 가구 수는 얼마나 되나", found)
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "plan"


@pytest.mark.usefixtures("two_counts")
def test_default_is_later_over_earlier() -> None:
    # 규칙도 계획도 없으면 예전처럼 뒤 ÷ 앞. 계획이 지표에 없는 말을 적었으면 쓰지 않는다
    found = _ratio_found(num="외국인", den="통근 인구")
    res, trace = run("2020년 서울 통근 인구에 비해 1인 가구 수는 얼마나 되나", found)
    assert res["result"]["value"] == 0.25 and trace["multi"]["ratio_by"] == "default"


def test_compare_same_unit_adds_gap_line(world) -> None:
    world["values"] = {"가": _value("가", 300, "명"), "나": _value("나", 120, "명")}
    res, _ = run("질문", _found([_target("가"), _target("나")], "compare"))
    assert "가가 나보다 180명 많습니다" in res["answer"]


def test_filter_then_lookup_uses_top_region(world) -> None:
    world["values"] = {
        "인구": lambda p: _rank("인구", ["경기도", "서울특별시"]),
        "출생아": lambda p: _value("출생아", 7, "명", region=p["regions"][0]),
    }
    found = _found([_target("인구", role="filter"), _target("출생아")], "filter_then_lookup")
    res, _ = run("인구가 가장 많은 시도의 출생아 수", found)
    assert ("인구", "rank", ()) in world["asked"]
    assert ("출생아", "value", ("경기도",)) in world["asked"]  # 1위 지역으로 다시 계산
    assert "인구가 가장 높은 지역은 경기도입니다" in res["answer"]


def test_filter_then_lookup_lowest_region_says_lowest(world) -> None:
    # "가장 적은 시도의 B": 안내문도 '가장 낮은' (11/18 전에는 늘 '가장 높은'이었다)
    world["values"] = {
        "인구": lambda p: _rank("인구", ["세종특별자치시", "제주특별자치도"]),
        "출생아": lambda p: _value("출생아", 3, "명", region=p["regions"][0]),
    }
    found = _found([_target("인구", role="filter"), _target("출생아")], "filter_then_lookup")
    res, _ = run("인구가 가장 적은 시도의 출생아 수", found)
    assert "인구가 가장 낮은 지역은 세종특별자치시입니다" in res["answer"]


def test_intersect_of_regions(world) -> None:
    world["values"] = {
        "인구": lambda p: _rank("인구", ["경기도", "서울특별시", "부산광역시"]),
        "출생아": lambda p: _rank("출생아", ["서울특별시", "세종특별자치시", "경기도"]),
    }
    found = _found([_target("인구"), _target("출생아")], "intersect")
    res, _ = run(
        "인구도 많고 출생아도 많은 시도는 어디야?", found, {**BASE, "type": "rank", "top": 2}
    )
    assert "모두 상위 2곳에 드는 지역은 서울특별시입니다" in res["answer"]


def test_intersect_of_one_group_has_no_number(world) -> None:
    # 한 집단이 두 특성을 함께 가진 값은 두 표의 숫자로 만들 수 없다
    found = _found([_target("노인 1인 가구"), _target("연금 수급자")], "intersect")
    res, _ = run("2020년 서울 노인 1인 가구 중 연금 수급자 수", found)
    assert res["status"] == "missing_data" and "value" not in res["result"]
    assert "함께 만족하는 대상을 따로 집계한 통계표를 찾지 못했습니다" in res["answer"]
    assert res["tables"]["recommendations"][0]["tbl_id"] == "T1"
    assert world["asked"] == []


def test_target_parsed_keeps_whole_question_conditions() -> None:
    p = multi_indicator.target_parsed(
        {**BASE, "type": "compare", "indicators": ["a", "b"]}, _target("통근 인구")
    )
    assert p["term"] == "통근 인구" and p["type"] == "value" and p["indicators"] is None
    assert p["regions"] == ["서울특별시"] and p["year"] == 2020


def test_dictionary_target_uses_dictionary(monkeypatch) -> None:
    # 사전에 있는 지표는 사전으로 (넘기기 계산으로 다시 보내지 않는다)
    used = []

    async def fake_answer(parsed, trace, question):
        used.append(parsed["indicator"])
        return _value("출생아 수", 10, "명")

    async def fail_target(*a, **k):
        raise AssertionError("사전 지표를 넘기기 계산으로 보냄")

    monkeypatch.setattr(indicators, "spec", lambda n: {"name": n} if n == "출생아 수" else None)
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n if n == "출생아 수" else None)
    monkeypatch.setattr(qa, "answer", fake_answer)
    monkeypatch.setattr(d, "_compute_target", fail_target)
    found = _found([_target("출생아 수")])
    found["handoff"]["targets"].append(_target("출생아 수"))
    res, _ = run("질문", found)
    assert used == ["출생아 수", "출생아 수"] and res["status"] == "ok"


def test_top_region_of_question_is_filter_then_lookup(world) -> None:
    # 계획이 intersect로 봐도 "A가 가장 많은 시도의 B"는 1위 지역의 B
    world["values"] = {
        "1인 가구": lambda p: _rank("1인 가구", ["경기도", "서울특별시"]),
        "통근 인구": lambda p: _value("통근 인구", 7, "명", region=p["regions"][0]),
    }
    found = _found([_target("1인 가구"), _target("통근 인구")], "intersect")
    res, trace = run("2020년 1인 가구가 가장 많은 시도의 통근 인구는?", found)
    assert trace["multi"]["op"] == "filter_then_lookup"
    assert ("통근 인구", "value", ("경기도",)) in world["asked"]
    assert "1인 가구가 가장 높은 지역은 경기도입니다" in res["answer"]
