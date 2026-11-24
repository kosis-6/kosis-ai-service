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
    state: dict = {"values": {}, "asked": []}

    async def fake_target(question, parsed, found, target, trace, multi=False):
        state["asked"].append((target["span"], parsed["type"], tuple(parsed.get("regions") or [])))
        got = state["values"].get(target["span"])
        return got(parsed) if callable(got) else got

    async def no_cache(question, parsed, trace):
        return None

    monkeypatch.setattr(d, "_compute_target", fake_target)
    monkeypatch.setattr(d, "cached_answer", no_cache)
    monkeypatch.setattr(indicators, "spec", lambda name: None)
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
    world["values"] = {"1인 가구 수": _value("1인 가구 수", 100), "통근 인구": None}
    res, _ = run("질문", _found([_target("1인 가구 수"), _target("통근 인구", cands=("T9",))]))
    assert res["status"] == "ok" and res["result"]["value"] == 100
    assert "통근 인구는 통계표에서 바로 계산하지 못했습니다 ('T9 표'" in res["answer"]


def test_nothing_computed_is_none(world) -> None:
    res, _ = run("질문", _found([_target("가"), _target("나")]))
    assert res is None


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
    res, trace = run("2020년 서울 1인 가구 수는 통근 인구의 몇 배야", _ratio_found())
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "rule"


@pytest.mark.usefixtures("two_counts")
def test_rule_wins_over_the_plan() -> None:
    found = _ratio_found(num="통근 인구", den="1인 가구 수")
    res, trace = run("2020년 서울 1인 가구 수는 통근 인구의 몇 배야", found)
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "rule"


@pytest.mark.usefixtures("two_counts")
def test_plan_decides_when_no_rule_matches() -> None:
    found = _ratio_found(num="1인 가구 수", den="통근 인구")
    res, trace = run("2020년 서울 통근 인구에 비해 1인 가구 수는 얼마나 되나", found)
    assert res["result"]["value"] == 4.0 and trace["multi"]["ratio_by"] == "plan"


@pytest.mark.usefixtures("two_counts")
def test_default_is_later_over_earlier() -> None:
    found = _ratio_found(num="외국인", den="통근 인구")
    res, trace = run("2020년 서울 통근 인구에 비해 1인 가구 수는 얼마나 되나", found)
    assert res["result"]["value"] == 0.25 and trace["multi"]["ratio_by"] == "default"


NO_YEAR = {"type": "value", "regions": ["서울특별시"], "year": None, "sex": "계"}


def _by_year(name, values, unit="명", as_of=None):
    latest = max(values)

    def got(parsed):
        year = parsed.get("year") or latest
        if year not in values:
            return None
        res = _value(name, values[year], unit, year=year)
        if as_of and not parsed.get("year"):
            res["result"]["as_of"] = as_of
        return res

    return got


def test_different_latest_years_are_aligned_to_the_common_year(world) -> None:
    world["values"] = {
        "외국인 유학생": _by_year("외국인 유학생", {2025: 30, 2024: 20}),
        "다문화 학생": _by_year("다문화 학생", {2024: 5}),
    }
    found = _found([_target("외국인 유학생"), _target("다문화 학생")])
    res, trace = run("서울 외국인 유학생과 다문화 학생 수", found, NO_YEAR)
    assert [(p["year"], p["value"]) for p in res["result"]["split"]] == [(2024, 20), (2024, 5)]
    assert "모두 있는 2024년으로 맞췄습니다" in res["answer"]
    assert trace["multi"]["aligned"] == {"year": 2024, "done": True}


def test_aligned_values_are_then_subtracted(world) -> None:
    world["values"] = {
        "전입": _by_year("전입", {2025: 300, 2024: 250}),
        "전출": _by_year("전출", {2024: 120}),
    }
    res, _ = run(
        "서울 전입과 전출 차이", _found([_target("전입"), _target("전출")], "diff"), NO_YEAR
    )
    assert res["result"]["value"] == 130 and res["result"]["op"] == "−"


def test_latest_month_value_is_aligned_to_the_last_annual_year(world, monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_data_years", lambda: (1992, 2025))
    world["values"] = {
        "주민": _by_year("주민", {2026: 110, 2025: 100}, as_of="2026년 10월 말"),
        "등록 차량": _by_year("등록 차량", {2025: 40}, "대"),
    }
    res, _ = run("서울 주민과 등록 차량", _found([_target("주민"), _target("등록 차량")]), NO_YEAR)
    assert [(p["year"], p["value"]) for p in res["result"]["split"]] == [(2025, 100), (2025, 40)]
    assert all(not p.get("as_of") for p in res["result"]["split"])


def test_values_of_the_same_month_are_not_realigned(world) -> None:
    month = "2026년 10월 말"
    world["values"] = {
        "주민": _by_year("주민", {2026: 110, 2025: 100}, as_of=month),
        "어르신": _by_year("어르신", {2026: 21, 2025: 20}, as_of=month),
    }
    res, trace = run("서울 주민과 어르신", _found([_target("주민"), _target("어르신")]), NO_YEAR)
    assert [(p["year"], p["value"]) for p in res["result"]["split"]] == [(2026, 110), (2026, 21)]
    assert "aligned" not in trace["multi"] and "맞췄습니다" not in res["answer"]


def test_alignment_keeps_first_values_when_recompute_fails(world) -> None:
    world["values"] = {
        "가": _by_year("가", {2025: 30}),
        "나": _by_year("나", {2020: 5}),
    }
    res, trace = run("서울 가와 나", _found([_target("가"), _target("나")]), NO_YEAR)
    assert [p["year"] for p in res["result"]["split"]] == [2025, 2020]
    assert "맞췄습니다" not in res["answer"]
    assert trace["multi"]["aligned"] == {"year": 2020, "done": False}


def test_no_alignment_when_the_question_says_a_year(world) -> None:
    world["values"] = {
        "가": _by_year("가", {2025: 30, 2020: 10}),
        "나": _by_year("나", {2020: 5}),
    }
    found = _found([_target("가"), _target("나")])
    res, trace = run("2020년 서울 가와 나", found, {**NO_YEAR, "year": 2020})
    assert "aligned" not in trace["multi"]
    same = {"가": _value("가", 1, year=2020), "나": _value("나", 2, year=2020)}
    world["values"] = same
    _, trace = run("서울 가와 나", found, NO_YEAR)
    assert "aligned" not in trace["multi"]


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
    assert ("출생아", "value", ("경기도",)) in world["asked"]
    assert "인구가 가장 높은 지역은 경기도입니다" in res["answer"]


def test_filter_then_lookup_lowest_region_says_lowest(world) -> None:
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
    found = _found([_target("노인 1인 가구"), _target("연금 수급자")], "intersect")
    res, _ = run("2020년 서울 노인 1인 가구 중 연금 수급자 수", found)
    assert res["status"] == "no_joint_table" and "value" not in res["result"]
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
    world["values"] = {
        "1인 가구": lambda p: _rank("1인 가구", ["경기도", "서울특별시"]),
        "통근 인구": lambda p: _value("통근 인구", 7, "명", region=p["regions"][0]),
    }
    found = _found([_target("1인 가구"), _target("통근 인구")], "intersect")
    res, trace = run("2020년 1인 가구가 가장 많은 시도의 통근 인구는?", found)
    assert trace["multi"]["op"] == "filter_then_lookup"
    assert ("통근 인구", "value", ("경기도",)) in world["asked"]
    assert "1인 가구가 가장 높은 지역은 경기도입니다" in res["answer"]


@pytest.fixture
def surveys(monkeypatch):
    from app.services import catalog

    by_table: dict = {}

    def surveys(ids):
        return {t: by_table[t] for t in ids if t in by_table}

    monkeypatch.setattr(catalog, "surveys", surveys)
    monkeypatch.setattr(catalog, "_groups", lambda ids: {})
    return by_table


def test_compare_of_different_surveys_has_no_gap_line(world, surveys) -> None:
    surveys.update({"T_주민등록 인구": "주민등록인구현황", "T_인구총조사 인구": "인구총조사"})
    world["values"] = {
        "주민등록 인구": _value("주민등록 인구", 9_386_034, "명"),
        "인구총조사 인구": _value("인구총조사 인구", 9_411_000, "명"),
    }
    found = _found([_target("주민등록 인구"), _target("인구총조사 인구")], "compare")
    res, _ = run("2020년 서울 인구총조사와 주민등록 인구 비교", found)
    assert res["result"]["split_by"] == "indicator"
    assert "많습니다" not in res["answer"]
    said = "주민등록 인구는 주민등록인구현황, 인구총조사 인구는 인구총조사 통계입니다"
    assert said in res["answer"]
    assert "직접 비교하지 않습니다" in res["result"]["basis_gap"]


def test_diff_of_different_surveys_is_side_by_side(world, surveys) -> None:
    surveys.update({"T_가구": "인구총조사", "T_세대": "주민등록인구현황"})
    world["values"] = {"가구": _value("가구", 300), "세대": _value("세대", 120)}
    res, _ = run("2020년 서울 가구와 세대 차이", _found([_target("가구"), _target("세대")], "diff"))
    assert res["result"]["split_by"] == "indicator" and "value" not in res["result"]
    assert "직접 비교하지 않습니다" in res["answer"]
    assert "직접 계산하지 않고 나란히" not in res["answer"]


def test_compare_of_same_survey_keeps_gap_line(world, surveys) -> None:
    surveys.update({"T_가": "인구동향조사", "T_나": "인구동향조사"})
    world["values"] = {"가": _value("가", 300, "명"), "나": _value("나", 120, "명")}
    res, _ = run("질문", _found([_target("가"), _target("나")], "compare"))
    assert "가가 나보다 180명 많습니다" in res["answer"] and "basis_gap" not in res["result"]


def test_ratio_of_different_surveys_still_divides(world, surveys) -> None:
    surveys.update({"T_외국인": "지방자치단체외국인주민현황", "T_인구": "주민등록인구현황"})
    world["values"] = {"외국인": _value("외국인", 50, "명"), "인구": _value("인구", 1000, "명")}
    found = _found([_target("인구"), _target("외국인")], "ratio")
    res, _ = run("2020년 서울 인구 중 외국인 비율", found)
    assert res["result"]["value"] == 5.0


def test_ratio_whole_side_does_not_take_question_age() -> None:
    parsed = {"indicator": "1인 가구 중 70세 이상 비율", "type": "value", "ages": [[70, None]]}
    whole = {"span": "전체 1인 가구", "unsaid_age": True}
    part = {"span": "70세 이상 1인 가구", "unsaid_age": False}
    assert multi_indicator.target_parsed(parsed, whole)["ages"] is None
    assert multi_indicator.target_parsed(parsed, part)["ages"] == [[70, None]]
    assert multi_indicator.says_age("70세 이상 1인 가구")
    assert multi_indicator.says_age("노인 1인 가구")
    assert not multi_indicator.says_age("전체 1인 가구")
    assert multi_indicator.without_unsaid_age(parsed, "전체 1인 가구")["ages"] is None
    assert multi_indicator.without_unsaid_age(parsed, "70세 이상 1인 가구") is parsed
