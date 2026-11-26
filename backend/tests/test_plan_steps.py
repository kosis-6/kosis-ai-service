"""계산 계획 steps 의 스키마 · 정리 · 검증 · combine 호환 변환 (LLM 없음)."""

from app.agent import plan_steps as ps
from app.prompts import plan as plan_prompt
from app.services import plan_exec as pe


def test_schema_adds_steps_and_answer_to_the_plan_schema() -> None:
    s = ps.schema(plan_prompt.SCHEMA)
    assert "steps" in s["properties"] and "answer" in s["properties"]
    assert s["properties"]["steps"]["maxItems"] == pe.MAX_STEPS
    assert set(s["required"]) >= {"shared", "indicators", "combine", "steps", "answer"}
    assert plan_prompt.SCHEMA["properties"].get("steps") is None  # 원본은 그대로


def test_normalize_strips_nulls_and_fills_defaults() -> None:
    raw = [
        {
            "op": "get",
            "indicator": 0,
            "regions": None,
            "years": ["latest"],
            "of": None,
            "why": "값",
        },
        {"op": "change", "of": 0, "from": "latest-5", "to": "latest", "how": None, "why": "변화"},
        {"op": "rank", "of": 1, "top": 3, "top_pct": None, "ascending": None, "why": "상위"},
    ]
    steps, problems = ps.normalize(raw, 1, {"step": 2, "with": []})
    assert problems == []
    assert steps[0] == {
        "op": "get",
        "indicator": 0,
        "regions": "all",
        "years": ["latest"],
        "why": "값",
    }
    assert steps[1]["how"] == "diff" and "of" in steps[1]
    assert steps[2] == {"op": "rank", "of": 1, "top": 3, "why": "상위"}


def test_normalize_rejects_made_up_years_and_bad_conditions() -> None:
    raw = [
        {"op": "get", "indicator": 0, "regions": "all", "years": ["작년"], "why": ""},
        {"op": "filter", "of": 0, "cond": {"above": 100}, "why": ""},
        {"op": "filter", "of": 0, "cond": {"ge": "많이"}, "why": ""},
    ]
    steps, problems = ps.normalize(raw, 1, {"step": 7, "with": [9]})
    assert steps is None
    assert any("latest" in p for p in problems)
    assert any("비교 기준 지역 이름" in p for p in problems)
    assert any("값은 숫자" in p for p in problems)
    assert any("answer.step 7" in p for p in problems) and any(
        "answer.with 9" in p for p in problems
    )


def _plan(op, inds, **combine):
    return {"indicators": inds, "combine": {"op": op, **combine}}


def test_from_combine_ratio_uses_span_references() -> None:
    inds = [{"span": "결혼이민자", "scope": "in"}, {"span": "등록외국인", "scope": "in"}]
    steps = ps.from_combine(_plan("ratio", inds, num="결혼이민자", den="등록외국인"), ["2024"])
    assert steps == [
        {"op": "get", "indicator": 0, "regions": "all", "years": ["2024"]},
        {"op": "get", "indicator": 1, "regions": "all", "years": ["2024"]},
        {"op": "combine", "a": 0, "b": 1, "how": "ratio", "scale": 100},
    ]
    assert pe.validate(steps, 2) == []


def test_from_combine_filter_then_lookup_and_intersect() -> None:
    inds = [{"span": "A", "role": "measure"}, {"span": "B", "role": "filter"}]
    steps = ps.from_combine(_plan("filter_then_lookup", inds))
    assert steps[0]["indicator"] == 1 and steps[1]["op"] == "rank" and steps[1]["top"] == 1
    assert steps[2] == {
        "op": "get",
        "indicator": 0,
        "regions": {"from_step": 1},
        "years": ["latest"],
    }
    steps = ps.from_combine(_plan("intersect", inds, top=3))
    assert [s["op"] for s in steps] == ["get", "rank", "get", "rank", "pick"]
    assert steps[4] == {"op": "pick", "of": 3, "regions": {"from_step": 1}}
    assert pe.validate(steps, 2) == []


def test_from_combine_skips_out_of_scope_and_unconvertible() -> None:
    inds = [{"span": "A", "scope": "in"}, {"span": "장애인", "scope": "out"}]
    assert ps.from_combine(_plan("none", inds)) == [
        {"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]}
    ]
    assert ps.from_combine(_plan("not_computable", inds)) is None
    assert ps.from_combine(_plan("none", [{"span": "x", "scope": "out"}])) is None
