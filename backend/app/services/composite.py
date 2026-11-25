"""복합 질문 (A_20 ③): 복합 판별 → 계산 계획(steps, LLM) → plan_exec 실행 → 답.

복합 = 지표 둘 · 시점 둘(상승폭 · 변화폭) · 걸러내기(이상인 지역 · 이면서) · 되가리킴(그 지역의) ·
상위 N% 중 둘 이상. 단순 질문은 지금 경로 그대로다.

get 은 지금 경로(multi_indicator._one: 사전 → qa.answer, 표 → dynamic_indicator)를 지역 전체
순위(type=rank, top 없음)로 불러 지역 × 시점 표를 만든다. 시점 'latest-N' 은 먼저 latest 를 가져와
그 해를 읽은 뒤 N 년 전으로 푼다 (연도는 LLM 이 아니라 자료가 정한다).

답을 못 내면(계획 틀림 · 어느 step 값 없음) None 을 돌려주고 지금 경로의 답이 그대로 나간다."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent import plan_steps
from app.prompts import composite as prompt
from app.services import flow, llm_calls, plan_exec, search_llm
from app.services.plan_exec import PlanError, Table

logger = logging.getLogger(__name__)

MODEL = "HCX-007"  # GemmaClient 는 GEMMA_MODEL 로 바꿔 보낸다
MAX_TOKENS = 700
NATION = "전국"


# ---------------------------------------------------------------- 복합 판별 (Gemma)
@llm_calls.staged("composite_gate")
async def _gate(question: str) -> dict | None:
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        MODEL,
        [
            {"role": "system", "content": prompt.GATE_SYSTEM},
            {"role": "user", "content": f"질문: {question}"},
        ],
        thinking={"effort": "none"},
        maxCompletionTokens=120,
        temperature=0,
        responseFormat={"type": "json", "schema": prompt.GATE_SCHEMA},
    )
    try:
        out = json.loads(result.content)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


async def is_composite(question: str, trace: dict | None = None) -> bool:
    """복합 질문인가 (Gemma 판별, 11/26 비교: 복합 13/13 · 단순 40 중 2 잘못). 판별이 실패하면 아님.
    잘못 잡은 것은 계획 뒤 simple_plan 이 지금 경로로 돌려보낸다"""
    try:
        out = await _gate(question)
    except Exception as e:
        if trace is not None:
            trace["composite_gate"] = {"error": f"{type(e).__name__}: {e}"[:160]}
        return False
    if trace is not None:
        trace["composite_gate"] = out
    return bool(out and out.get("composite"))


def simple_plan(steps: list[dict]) -> bool:
    """get 말고 연산이 하나뿐이고 앞 step 의 지역을 되가리키지 않으면 지금 경로가 하는 일이다"""
    others = [s for s in steps if s.get("op") != "get"]
    back = any(isinstance(s.get("regions"), dict) for s in steps)
    return len(others) <= 1 and not back


# ---------------------------------------------------------------- 계획 (LLM)
def user_text(question: str, spans: list[str], problems: list[str] | None = None) -> str:
    lines = [f"질문: {question}", "지표 목록:"]
    lines += [f"  {i}: {s}" for i, s in enumerate(spans)]
    if problems:
        lines.append("앞서 낸 계획의 틀린 점 (고쳐서 다시 낸다): " + " · ".join(problems))
    return "\n".join(lines)


@llm_calls.staged("composite_plan")
async def _plan(text: str) -> dict | None:
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        MODEL,
        [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": text}],
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        temperature=0,
        responseFormat={"type": "json", "schema": prompt.SCHEMA},
    )
    try:
        out = json.loads(result.content)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


async def plan(question: str, spans: list[str], record: dict) -> dict | None:
    """계획 1회 + 틀리면 1회 다시. 쓸 수 있는 {"steps", "answer"} 또는 None"""
    problems: list[str] = []
    for _ in range(2):
        raw = await _plan(user_text(question, spans, problems or None))
        record.setdefault("raw", []).append(raw)
        if raw is None:
            problems = ["JSON 이 아님"]
            continue
        steps, problems = plan_steps.normalize(raw.get("steps"), len(spans), raw.get("answer"))
        if steps is not None:
            return {"steps": steps, "answer": raw.get("answer") or {}}
        record.setdefault("problems", []).append(problems)
    return None


# ---------------------------------------------------------------- get (지금 경로로 값 가져오기)
class Getter:
    def __init__(self, question: str, parsed: dict, found: dict, targets: list[dict], trace: dict):
        self.question, self.parsed, self.found, self.targets = question, parsed, found, targets
        self.trace = trace
        self.latest: dict[int, int] = {}
        self.sources: list[dict] = []
        self.calls = 0

    def _level(self) -> str:
        return self.parsed.get("level") or ("시군구" if self.parsed.get("sido") else "시도")

    async def _rank(self, ind: int, year: int | None) -> dict:
        from app.services import multi_indicator

        target = self.targets[ind]
        p = {
            **self.parsed,
            "regions": [],
            "top": None,
            "target": None,
            "year": year,
            "start_year": None,
            "end_year": None,
            "level": self._level(),
            "ascending": False,
            "asked": {**(self.parsed.get("asked") or {}), "latest": year is None},
        }
        asked = self.question
        if year is not None:
            # 표 경로는 질문 글에서 연도를 읽으므로 연도를 질문 앞에 붙이고 target 조건에도 둔다
            asked = f"{year}년 {self.question}"
            target = {
                **target,
                "question": asked,
                "conditions": {**(target.get("conditions") or {}), "year": year},
            }
        self.calls += 1
        res, sub = await multi_indicator._one(asked, p, self.found, target, "rank")
        self.trace.setdefault("dynamic", []).extend(sub.get("dynamic") or [])
        if not multi_indicator._ok(res) or not (res["result"].get("ranking")):
            why = (res or {}).get("reason") or ((res or {}).get("result") or {}).get("reason") or ""
            raise PlanError(
                (res or {}).get("status") or "missing_data", None, f"{_span(target)}: {why}"
            )
        r = res["result"]
        self.sources.append({k: r.get(k) for k in ("source", "basis", "tbl_id", "url") if r.get(k)})
        return r

    async def _year(self, ind: int, expr: str) -> int | None:
        if expr == "latest":
            return None
        if expr.startswith("latest-"):
            if ind not in self.latest:
                r = await self._rank(ind, None)
                self.latest[ind] = int(r["year"])
            return self.latest[ind] - int(expr.split("-", 1)[1])
        return int(expr)

    async def __call__(self, ind: int, regions: Any, years: list[str]) -> Table:
        rows: dict[str, dict[str, float | None]] = {}
        unit, name = None, _span(self.targets[ind])
        for expr in years:
            year = await self._year(ind, expr)
            r = await self._rank(ind, year)
            period = str(r.get("year") or expr)
            if year is None and str(r.get("year", "")).isdigit():
                self.latest[ind] = int(r["year"])
            unit = unit or r.get("unit")
            for x in r.get("ranking") or []:
                rows.setdefault(x["region"], {})[period] = x.get("value")
            if r.get("national") is not None:
                rows.setdefault(NATION, {})[period] = r["national"]
        if isinstance(regions, list):
            rows = {k: v for k, v in rows.items() if k in regions}
        return Table(rows, indicator=name, unit=unit, label=name)


def _span(target: dict) -> str:
    return target.get("span") or target.get("question") or "그 지표"


# ---------------------------------------------------------------- 답 모양
def _result(table: Table, getter: Getter, level: str) -> dict:
    period = table.periods()[-1] if table.periods() else "latest"
    src = getter.sources[0] if getter.sources else {}
    extra = [s for s in getter.sources[1:] if s.get("tbl_id") != src.get("tbl_id")]
    base: dict[str, Any] = {
        "status": "ok",
        "indicator": table.label or table.indicator or "",
        "unit": table.unit or "",
        "year": int(period) if period.isdigit() else None,
        "period_label": period if not period.isdigit() else None,
        "kind": "composite",
        "notes": [],
        **src,
    }
    seen = set()
    base["extra_sources"] = [
        s for s in extra if s.get("tbl_id") not in seen and not seen.add(s.get("tbl_id"))
    ]
    regions = table.regions()
    if len(regions) == 1 and table.order is None:
        r = regions[0]
        return {**base, "region": r, "value": table.single(r)}
    items = [{"rank": i + 1, "region": r, "value": table.single(r)} for i, r in enumerate(regions)]
    return {
        **base,
        "ranking": items,
        "order": "낮은 순" if table.meta.get("ascending") else "높은 순",
        "level": level,
        "total_regions": table.meta.get("total_regions") or len(items),
    }


def _lead(all_tables: list[Table], steps: list[dict], with_: list[int]) -> list[str]:
    out = []
    for k in with_:
        if not (0 <= k < len(all_tables)):
            continue
        t, s = all_tables[k], steps[k]
        regs = t.regions()
        if not regs:
            continue
        what = s.get("why") or f"{s.get('op')} 결과"
        shown = ", ".join(
            f"{r}({t.single(r):,.1f}{t.unit or ''})" if t.single(r) is not None else r
            for r in regs[:10]
        )
        out.append(f"{what}: {shown}")
    return out


# ---------------------------------------------------------------- 들어가는 곳
async def answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    """복합 질문의 답. 계획을 못 세우거나 값을 못 내면 None (지금 경로의 답이 그대로 나간다)"""
    from app.services import qa

    h = (found or {}).get("handoff") or {}
    targets = [t for t in h.get("targets") or [] if (t.get("scope") or "in") == "in"]
    if not targets or h.get("status") not in ("ok", "partial_match"):
        return None
    spans = [_span(t) for t in targets]
    record: dict[str, Any] = {"spans": spans}
    trace["composite"] = record
    try:
        p = await plan(question, spans, record)
    except Exception as e:  # LLM 실패
        record["error"] = f"{type(e).__name__}: {e}"[:200]
        flow.add("복합 계획", "실패", record["error"])
        return None
    if p is None:
        flow.add("복합 계획", "계획 없음", " · ".join((record.get("problems") or [[]])[-1])[:200])
        return None
    record["steps"] = p["steps"]
    if simple_plan(p["steps"]):
        flow.add("복합 계획", "단순", "연산이 하나뿐 → 지금 경로")
        return None
    flow.add("복합 계획", "steps", " → ".join(s["op"] for s in p["steps"]))
    getter = Getter(question, parsed, found, targets, trace)
    try:
        out, all_ = await plan_exec.run({"indicators": targets, **p}, getter, trace)
    except PlanError as e:
        record["failed"] = {"step": e.step, "code": e.code, "why": e.why}
        flow.add("복합 계산", e.code, f"step {e.step}: {e.why}"[:200])
        return None
    except Exception:
        logger.exception("복합 계산 실패")
        return None
    record["gets"] = getter.calls
    level = getter._level()
    result = _result(out, getter, level)
    leads = _lead(all_, p["steps"], (p.get("answer") or {}).get("with") or [])
    result["notes"] = [*leads, "질문을 계산 순서로 나눠 차례로 계산했습니다"]
    flow.add("복합 계산", "ok", f"get {getter.calls}회 · step {len(p['steps'])}")
    text = f"{qa.summary(result)}\n\n{qa.render(result)}".strip()
    return {
        "status": "ok",
        "answer": text,
        "parsed": {**parsed, "indicators": spans, "composite": True, "grounded": True},
        "result": result,
        "sources": qa.sources(result),
        "trace": trace,
    }
