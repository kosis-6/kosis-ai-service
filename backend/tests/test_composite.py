"""복합 질문 (composite): 판별 · 답 모양 · 계획 → 실행 → 답 (LLM · DB 없이 가짜 계획 · 가짜 get)."""

import asyncio

from app.services import composite, qa
from app.services.plan_exec import Table


def test_gate_reads_the_llm_verdict_and_fails_closed(monkeypatch) -> None:
    async def yes(question):
        return {"composite": True, "why": "두 시점 변화 + 상위 N%"}

    async def broken(question):
        raise RuntimeError("down")

    trace = {}
    monkeypatch.setattr(composite, "_gate", yes)
    assert asyncio.run(composite.is_composite("질문", trace)) is True
    assert trace["composite_gate"]["why"].startswith("두 시점")
    monkeypatch.setattr(composite, "_gate", broken)
    assert asyncio.run(composite.is_composite("질문", trace)) is False
    assert "error" in trace["composite_gate"]


def test_simple_plan_goes_back_to_the_usual_path() -> None:
    one_sum = [
        {"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]},
        {"op": "get", "indicator": 1, "regions": "all", "years": ["latest"]},
        {"op": "combine", "a": 0, "b": 1, "how": "sum"},
    ]
    assert composite.simple_plan(one_sum) is True
    two = one_sum + [{"op": "rank", "of": 2, "top": 3}]
    assert composite.simple_plan(two) is False
    back = [
        {"op": "get", "indicator": 0, "regions": "all", "years": ["latest"]},
        {"op": "rank", "of": 0, "top": 3},
        {"op": "get", "indicator": 1, "regions": {"from_step": 1}, "years": ["latest"]},
    ]
    assert composite.simple_plan(back) is False


def test_result_shapes_render_with_the_existing_summary() -> None:
    class G:
        sources = [{"source": "A", "basis": "B", "tbl_id": "DT_1", "url": "u"}]

    ranked = Table(
        {"서울": {"2024": 2.0}, "부산": {"2024": 1.5}},
        unit="%p",
        label="상승폭",
        order=["서울", "부산"],
    )
    ranked.meta["total_regions"] = 17
    r = composite._result(ranked, G(), "시도")
    assert r["ranking"][0] == {"rank": 1, "region": "서울", "value": 2.0} and r["level"] == "시도"
    assert "서울" in qa.summary(r) and "출처: A" in qa.render(r)
    one = Table({"서울": {"2019→2024": 3.0}}, unit="명", label="차이")
    r1 = composite._result(one, G(), "시도")
    assert r1["value"] == 3.0 and r1["period_label"] == "2019→2024" and "서울" in qa.summary(r1)


def test_answer_runs_plan_and_falls_back_to_none_on_failure(monkeypatch) -> None:
    targets = [{"span": "A 비중", "scope": "in"}, {"span": "B 비중", "scope": "in"}]
    found = {"handoff": {"status": "ok", "targets": targets}}
    data = {
        0: {
            "서울": {"2019": 1.0, "2024": 4.0},
            "부산": {"2019": 2.0, "2024": 3.0},
            "전국": {"2019": 1.5, "2024": 3.0},
        },
        1: {"서울": {"2019": 5.0, "2024": 6.0}, "부산": {"2019": 5.0, "2024": 5.5}},
    }

    async def fake_plan(question, spans, record):
        return {
            "steps": [
                {
                    "op": "get",
                    "indicator": 0,
                    "regions": "all",
                    "years": ["2019", "2024"],
                    "why": "A",
                },
                {
                    "op": "change",
                    "of": 0,
                    "from": "2019",
                    "to": "2024",
                    "how": "diff",
                    "why": "상승폭",
                },
                {"op": "rank", "of": 1, "top": 1, "why": "상위 1"},
                {
                    "op": "get",
                    "indicator": 1,
                    "regions": {"from_step": 2},
                    "years": ["2019", "2024"],
                    "why": "B",
                },
                {
                    "op": "change",
                    "of": 3,
                    "from": "2019",
                    "to": "2024",
                    "how": "diff",
                    "why": "변화폭",
                },
            ],
            "answer": {"step": 4, "with": [2]},
        }

    class FakeGetter(composite.Getter):
        async def __call__(self, ind, regions, years):
            self.calls += 1
            self.sources.append(
                {"source": f"출처{ind}", "basis": "b", "tbl_id": f"DT_{ind}", "url": "u"}
            )
            rows = {r: {y: v.get(y) for y in years} for r, v in data[ind].items()}
            if isinstance(regions, list):
                rows = {r: v for r, v in rows.items() if r in regions}
            return Table(rows, unit="%", label=f"지표{ind}")

    monkeypatch.setattr(composite, "plan", fake_plan)
    monkeypatch.setattr(composite, "Getter", FakeGetter)
    trace = {}
    out = asyncio.run(composite.answer("질문", {"level": "시도"}, found, trace))
    assert (
        out["status"] == "ok"
        and out["result"]["value"] == 1.0
        and out["result"]["region"] == "서울"
    )
    assert "상위 1: 서울" in out["answer"] and out["parsed"]["composite"] is True
    assert [s["tbl_id"] for s in out["sources"]] == ["DT_0", "DT_1"]
    assert trace["composite"]["gets"] == 2

    async def no_plan(question, spans, record):
        record["problems"] = [["연도 아님"]]
        return None

    monkeypatch.setattr(composite, "plan", no_plan)
    assert asyncio.run(composite.answer("질문", {}, found, {})) is None
    assert (
        asyncio.run(
            composite.answer("질문", {}, {"handoff": {"status": "out_of_scope", "targets": []}}, {})
        )
        is None
    )
