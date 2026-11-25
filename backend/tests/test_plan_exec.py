"""복합 질문 계산 계획 실행기 (plan_exec): LLM 없이 가짜 get 으로 연산 6개와 검증을 확인한다."""

import asyncio

import pytest

from app.services import plan_exec as pe
from app.services.plan_exec import PlanError, Table

# 가짜 자료: 지표 0 = 어떤 비중(%), 지표 1 = 다른 비중(%). 지역 4 + 전국, 시점 2019 · 2024
DATA = {
    0: {
        "전국": {"2019": 30.0, "2024": 36.0},
        "서울": {"2019": 32.0, "2024": 40.0},  # +8
        "부산": {"2019": 28.0, "2024": 35.0},  # +7
        "대구": {"2019": 30.0, "2024": 33.0},  # +3
        "인천": {"2019": 25.0, "2024": 26.0},  # +1
    },
    1: {
        "전국": {"2019": 5.0, "2024": 6.0},
        "서울": {"2019": 5.0, "2024": 7.0},
        "부산": {"2019": 6.0, "2024": 6.5},
        "대구": {"2019": 4.0, "2024": 4.2},
        "인천": {"2019": 5.5, "2024": 5.0},
    },
}


async def fake_get(ind: int, regions, years: list[str]) -> Table:
    src = DATA[ind]
    regs = list(src) if regions == "all" else [r for r in regions if r in src]
    all_years = sorted({y for r in src.values() for y in r})

    def year(expr: str) -> str:
        if expr == "latest":
            return all_years[-1]
        if expr.startswith("latest-"):
            return str(int(all_years[-1]) - int(expr.split("-")[1]))
        return expr

    ys = [year(y) for y in years]
    rows = {r: {y: src[r].get(y) for y in ys} for r in regs}
    return Table(rows, indicator=f"지표{ind}", unit="%")


def _run(plan):
    trace = {}
    out, all_ = asyncio.run(pe.run(plan, fake_get, trace))
    return out, all_, trace


def test_change_then_rank_then_get_from_step_then_change() -> None:
    # "A 상승폭 상위 2 지역의 B 변화폭" (V3-004 모양)
    plan = {
        "indicators": [{}, {}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": "all", "years": ["2019", "latest"]},
            {"op": "change", "of": 0, "from": "2019", "to": "latest", "how": "diff"},
            {"op": "rank", "of": 1, "top": 2},
            {"op": "get", "indicator": 1, "regions": {"from_step": 2}, "years": ["2019", "2024"]},
            {"op": "change", "of": 3, "from": "2019", "to": "2024"},
        ],
        "answer": {"step": 4},
    }
    out, all_, trace = _run(plan)
    assert all_[2].order == ["서울", "부산"] and all_[2].meta["total_regions"] == 4
    assert out.rows == {"서울": {"2019→2024": 2.0}, "부산": {"2019→2024": 0.5}}
    assert [s["status"] for s in trace["plan_exec"]] == ["ok"] * 5


def test_combine_diff_and_which_larger() -> None:
    plan = {
        "indicators": [{}, {}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": ["서울", "인천"], "years": ["2024"]},
            {"op": "get", "indicator": 1, "regions": ["서울", "인천"], "years": ["2024"]},
            {"op": "combine", "a": 0, "b": 1, "how": "which_larger"},
            {"op": "combine", "a": 0, "b": 1, "how": "ratio", "scale": 100},
        ],
        "answer": {"step": 2},
    }
    out, all_, _ = _run(plan)
    assert out.rows == {"서울": {"2024": 33.0}, "인천": {"2024": 21.0}}
    assert out.meta["larger"] == {"서울": "a", "인천": "a"}
    assert all_[3].rows["인천"]["2024"] == pytest.approx(520.0) and all_[3].unit == "%"


def test_filter_above_nation_then_filter_top_pct_on_other_indicator() -> None:
    # "A 가 전국보다 높고 B 상위 50% 인 지역" (V3-005 모양)
    plan = {
        "indicators": [{}, {}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]},
            {"op": "filter", "of": 0, "cond": {"above": "전국"}},
            {"op": "get", "indicator": 1, "regions": {"from_step": 1}, "years": ["latest"]},
            {"op": "filter", "of": 2, "cond": {"top_pct": 50}},
        ],
    }
    out, all_, _ = _run(plan)
    assert all_[1].order == ["서울"]  # 전국 36 보다 큰 곳은 서울(40)뿐
    assert out.order == ["서울"] and out.meta["cond"] == {"top_pct": 50}


def test_filter_ge_and_rate_change_and_bottom_rank() -> None:
    plan = {
        "indicators": [{}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": "all", "years": ["latest-5", "latest"]},
            {"op": "change", "of": 0, "from": "latest-5", "to": "latest", "how": "rate"},
            {"op": "filter", "of": 1, "cond": {"ge": 10}},
            {"op": "rank", "of": 1, "top": 1, "ascending": True},
        ],
        "answer": {"step": 2, "with": [3]},
    }
    out, all_, _ = _run(plan)
    assert all_[1].unit == "%" and all_[1].rows["서울"]["2019→2024"] == pytest.approx(25.0)
    assert out.order == ["서울", "부산", "대구"]  # 전국(20%)은 걸러내기 대상이 아님
    assert all_[3].order == ["인천"]


def test_pick_keeps_only_regions_from_an_earlier_step() -> None:
    plan = {
        "indicators": [{}, {}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]},
            {"op": "rank", "of": 0, "top_pct": 50},
            {"op": "get", "indicator": 1, "regions": "all", "years": ["latest"]},
            {"op": "pick", "of": 2, "regions": {"from_step": 1}},
        ],
    }
    out, _, _ = _run(plan)
    assert out.order == ["서울", "부산"] and set(out.rows) == {"서울", "부산"}


def test_validation_catches_bad_plans() -> None:
    assert pe.validate([], 1) == ["steps 가 비어 있음"]
    bad = [
        {"op": "change", "of": 0, "from": "2019", "to": "2024"},  # 앞 step 없음
        {"op": "get", "indicator": 3, "regions": "all", "years": []},
        {"op": "sort", "of": 0},
        {"op": "rank", "of": 1},
        {"op": "filter", "of": 1, "cond": {"between": 1}},
    ]
    problems = pe.validate(bad, 2)
    assert any("앞 step" in p for p in problems)
    assert any("indicator 번호 3" in p for p in problems) and any("years" in p for p in problems)
    assert any("모르는 연산" in p for p in problems)
    assert any("top 또는 top_pct" in p for p in problems)
    assert any("filter.cond" in p for p in problems)
    too_many = [{"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]}] * 5
    assert any("get 이 5개" in p for p in pe.validate(too_many, 1))


def test_run_reports_the_failing_step() -> None:
    plan = {
        "indicators": [{}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": ["없는곳"], "years": ["latest"]},
            {"op": "rank", "of": 0, "top": 1},
        ],
    }
    trace = {}
    with pytest.raises(PlanError) as e:
        asyncio.run(pe.run(plan, fake_get, trace))
    assert e.value.step == 0 and e.value.code == "missing_data"
    assert trace["plan_exec"][-1]["status"] == "missing_data"

    plan2 = {
        "indicators": [{}],
        "steps": [
            {"op": "get", "indicator": 0, "regions": "all", "years": ["2019", "2024"]},
            {"op": "change", "of": 0, "from": "2010", "to": "2024"},
        ],
    }
    with pytest.raises(PlanError) as e2:
        asyncio.run(pe.run(plan2, fake_get))
    assert e2.value.step == 1 and e2.value.code == "out_of_range"
