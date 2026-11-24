import copy
import logging
import operator
import time
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent import graph as base
from app.agent import plan as planner
from app.services import search
from app.services import slots as slot_parser
from app.services.hcx_client import HcxError

logger = logging.getLogger(__name__)

TOP_K = 5


class State(base.State, total=False):
    plan: dict[str, Any]
    ind_results: Annotated[list[dict[str, Any]], operator.add]


def brief(families: list[dict[str, Any]], k: int = TOP_K) -> list[dict[str, Any]]:
    return [
        {
            "tbl_id": f["table"]["tbl_id"],
            "table_name": f["table"]["table_name"],
            "family_id": f["table"]["family_id"],
            "family_name": f["table"].get("family_name"),
            "score": round(f["score"], 3),
            "checks": dict(f.get("checks") or {}),
        }
        for f in families[:k]
    ]


def in_scope(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        i
        for i in p.get("indicators") or []
        if i.get("scope") != "out" and (i.get("search") or "").strip()
    ]


def empty_plan(error: str) -> dict[str, Any]:
    return {**planner.parse(""), "_error": error[:200]}


async def plan(s: State) -> dict[str, Any]:
    """계획: 질문을 지표로 나누고 공통 조건·지표별 조건·결합 방식을 정한다 (HCX-007 1회).
    계획이 미리 들어와 있으면(재계획, agent/replan.py) LLM 을 부르지 않고 그대로 쓴다"""
    if s.get("plan"):
        return {"plan": {**s["plan"], "_given": True}}
    start = time.perf_counter()
    try:
        p = await planner.call(s["question"])
    except HcxError as e:
        p = empty_plan(f"{type(e).__name__}: {e}")
    p["_ms"] = round((time.perf_counter() - start) * 1000)
    return {"plan": p}


async def fix_plan(s: State) -> dict[str, Any]:
    try:
        fixed = planner.correct(s["question"], copy.deepcopy(s["plan"]))
    except Exception as e:
        logger.exception("계획 보정 실패")
        fixed = {**empty_plan(f"{type(e).__name__}: {e}"), "_ms": s["plan"].get("_ms")}
    return {"plan": fixed}


def fan_out(s: State) -> list[Send] | str:
    p = s["plan"]
    inds = in_scope(p)
    if len(inds) < 2:
        return "ind_done"
    sends = []
    for order, ind in enumerate(inds):
        cond = planner.merged_conditions(
            p.get("shared"), ind.get("override"), ind.get("no_inherit") or ()
        )
        payload = {**ind, "order": order, "conditions": cond, "question": planner.render(ind, cond)}
        sends.append(Send("indicator_search", {"ind": payload}))
    return sends


async def indicator_search(s: dict[str, Any]) -> dict[str, Any]:
    ind = s["ind"]
    question = ind["question"]
    start = time.perf_counter()
    out = {
        k: ind.get(k)
        for k in ("order", "span", "search", "scope", "role", "conditions", "question")
    }
    try:
        tables = await search.load_tables()
        slots = slot_parser.correct(question, slot_parser.empty_slots(question))
        dense = await search.dense_scores(question)
        search.ground_conditions(slots)
        out["candidates"] = brief(search.fold(search.rank(tables, slots, dense)))
    except Exception as e:
        logger.exception("지표별 검색 실패: %s", question)
        out["candidates"] = []
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    out["ms"] = round((time.perf_counter() - start) * 1000)
    return {"ind_results": [out]}


async def ind_done(s: State) -> dict[str, Any]:
    return {}


def _years(ind: dict[str, Any]) -> tuple[Any, Any]:
    cond = ind.get("conditions") or {}
    return cond.get("year_from"), cond.get("year_to")


def merge_same(inds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    kept: list[dict[str, Any]] = []
    by_family: dict[str, dict[str, Any]] = {}
    for ind in inds:
        top = (ind.get("candidates") or [{}])[0].get("family_id")
        first = by_family.get(top) if top else None
        if first is not None and _years(first) != _years(ind):
            first = None
        if first is None:
            if top and top not in by_family:
                by_family[top] = ind
            kept.append(ind)
        else:
            first.setdefault("merged_spans", [first.get("span")]).append(ind.get("span"))
    merged = [ind["merged_spans"] for ind in kept if ind.get("merged_spans")]
    return kept, merged


async def handoff(s: State, config: RunnableConfig) -> dict[str, Any]:
    result = s["result"]
    p = s.get("plan") or {}
    n_planned = len(in_scope(p))
    inds = sorted(s.get("ind_results") or [], key=lambda x: x.get("order", 0))
    inds, merged = merge_same(inds)
    is_multi = len(inds) >= 2
    whole: list[dict[str, Any]] = []
    if s.get("slots"):
        tables = await search.load_tables()
        ranked = search.rank(tables, copy.deepcopy(s["slots"]), s.get("dense"))
        whole = brief(search.fold(ranked))
    multi = {
        "mode": "multi" if is_multi else "single",
        "n_planned": n_planned,
        "n_indicators": len(inds) if inds else min(n_planned, 1),
        "merged": merged,
        "plan": {
            "shared": p.get("shared") or {},
            "combine": p.get("combine") or {},
            "out_of_scope": [
                i.get("span") for i in p.get("indicators") or [] if i.get("scope") == "out"
            ],
            "dropped": p.get("_dropped") or [],
            "fixed": p.get("_fixed") or [],
            "error": p.get("_error"),
            "ms": p.get("_ms"),
            "given": bool(p.get("_given")),
            # 지표 목록까지 남긴다: 재계획(agent/replan.py)이 처음 계획으로 쓴다
            "indicators": [
                {k: i.get(k) for k in planner.IND_KEYS if i.get(k) is not None}
                for i in p.get("indicators") or []
            ],
        },
        "indicators": inds if is_multi else [],
        "whole": whole,
    }
    base.trace_of(config)["multi"] = {
        "mode": multi["mode"],
        "n_planned": n_planned,
        "n_indicators": multi["n_indicators"],
        "merged": merged,
        "plan_ms": p.get("_ms"),
        "plan_error": p.get("_error"),
    }
    return {"result": {**result, "multi": multi}}


def after_route(s: State) -> str | list[str]:
    if s.get("result") is not None:
        return "save"
    if base.pending(s.get("prev")):
        return "understand"
    return ["understand", "plan"]


def build():
    g = StateGraph(State)
    for name, fn in (
        *base.NODES,
        ("plan", plan),
        ("fix_plan", fix_plan),
        ("indicator_search", indicator_search),
        ("ind_done", ind_done),
        ("handoff", handoff),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, ["save", "understand", "plan"])
    base.add_search_flow(g)
    g.add_edge("plan", "fix_plan")
    g.add_conditional_edges("fix_plan", fan_out, ["indicator_search", "ind_done"])
    g.add_edge("indicator_search", "ind_done")
    g.add_edge(["save", "ind_done"], "handoff")
    g.add_edge("handoff", END)
    return g.compile()


GRAPH = build()


async def handle(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    user_id: int | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """dialog.handle과 같은 인자. 새 질문이면 반환값에 multi가 덧붙는다.
    plan: 미리 세운 계획(재계획). 있으면 계획 LLM 을 건너뛰고 이 계획으로 지표를 찾는다"""
    state: dict[str, Any] = {
        "question": question,
        "session_id": session_id,
        "selections": selections,
        "user_id": user_id,
    }
    if plan:
        state["plan"] = plan
    out = await GRAPH.ainvoke(
        state,
        config={"configurable": {"trace": {} if trace is None else trace}},
    )
    return out["result"]
