"""통계표 찾기에 계획과 지표별 검색을 얹은 흐름

graph.py의 노드를 그대로 쓰고, 새 질문일 때만 계획 가지를 함께 돌린다.

    route ─ 되묻기 답으로 풀림 ───────────────────────────────────────────► save
      │ 되묻기 대기 중인데 답으로 못 풂 → understand → … → save   [graph.py와 같음]
      │ 새 질문: 두 가지를 동시에
      ├─ understand(조건 추출) → retrieve(질문 전체 검색) → review(후보 비교) → save ─────┐
      └─ plan(계획 LLM) → fix_plan(코드 보정) ─ 지표 2개 이상 → indicator_search × N ─┐   ├→ handoff
                                              └ 지표 1개 이하 ─────────────────→ ind_done ─┘

- 지표별 검색은 LLM을 부르지 않는다. 계획이 만든 문장(검색어 + 조건)을 원문 규칙으로만 읽는다.
  그래서 새 질문 하나에 HCX는 기존 2회(조건 추출, 최종 검토)에 계획 1회가 더해진다.
- 지표별 검색은 되묻지 않고 표 묶음 상위 5개를 넘긴다. 되묻기는 질문 전체 검색에서만 한다.
- 나눈 지표들의 1위 후보가 같은 표 묶음이면 지표 하나였다고 보고 합친다.
- 계획 가지가 실패해도 질문 전체 검색 결과는 그대로 나간다.

결과: 기존 결과(result)는 그대로 두고 result["multi"]를 덧붙인다 (되묻기 답 턴에는 없다).
    multi = {mode: "single" | "multi", n_planned, n_indicators, merged, plan, indicators, whole}
지표가 1개 이하면 지표별 검색을 하지 않으므로 mode는 "single"이고 indicators·whole은 비어 있다.

측정 (2026-11-12, 여러 표 질문 56문항의 필요한 표 125개):
  질문 전체 검색 상위 5개 77% → 지표별 상위 5개 91% → 지표별 5개 + 전체 5개 95%.
  표 하나 질문 408문항의 최종 정답은 그대로 (323, 기존 322).
"""

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

TOP_K = 5  # 지표마다 넘기는 후보 수


class State(base.State, total=False):
    plan: dict[str, Any]
    # 지표별 검색 결과. 여러 갈래가 동시에 쓰므로 이어 붙인다
    ind_results: Annotated[list[dict[str, Any]], operator.add]


def brief(families: list[dict[str, Any]], k: int = TOP_K) -> list[dict[str, Any]]:
    """표 묶음 순위 → 넘겨줄 후보 목록"""
    return [
        {
            "tbl_id": f["table"]["tbl_id"],
            "table_name": f["table"]["table_name"],
            "family_id": f["table"]["family_id"],
            "family_name": f["table"].get("family_name"),
            "score": round(f["score"], 3),
        }
        for f in families[:k]
    ]


def in_scope(p: dict[str, Any]) -> list[dict[str, Any]]:
    """검색할 지표: 범위 안이고 검색어가 있는 것"""
    return [
        i
        for i in p.get("indicators") or []
        if i.get("scope") != "out" and (i.get("search") or "").strip()
    ]


def empty_plan(error: str) -> dict[str, Any]:
    return {**planner.parse(""), "_error": error[:200]}


# ---- 계획 가지 -------------------------------------------------------------------


async def plan(s: State) -> dict[str, Any]:
    """계획: 질문을 지표로 나누고 공통 조건·지표별 조건·결합 방식을 정한다 (HCX-007 1회)"""
    start = time.perf_counter()
    try:
        p = await planner.call(s["question"])
    except HcxError as e:  # 계획이 실패해도 질문 전체 검색은 그대로 답한다
        p = empty_plan(f"{type(e).__name__}: {e}")
    p["_ms"] = round((time.perf_counter() - start) * 1000)
    return {"plan": p}


async def fix_plan(s: State) -> dict[str, Any]:
    """코드 보정: 근거 없는 조건 삭제, 시점·지역 단위 규칙, 빠진 지표 채우기, 파생 지표 풀기"""
    try:
        fixed = planner.correct(s["question"], copy.deepcopy(s["plan"]))
    except Exception as e:  # LLM 출력 모양이 예상 밖이어도 질문 전체 검색은 답해야 한다
        logger.exception("계획 보정 실패")
        fixed = {**empty_plan(f"{type(e).__name__}: {e}"), "_ms": s["plan"].get("_ms")}
    return {"plan": fixed}


def fan_out(s: State) -> list[Send] | str:
    """범위 안 지표가 2개 이상이면 지표마다 검색 갈래를 만든다"""
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
    """지표 하나를 표 하나 찾는 질문으로 검색한다. 되묻지 않고 표 묶음 상위 5개를 돌려준다"""
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
    except Exception as e:  # 지표 하나가 실패해도 나머지 지표와 질문 전체 검색은 답한다
        logger.exception("지표별 검색 실패: %s", question)
        out["candidates"] = []
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    out["ms"] = round((time.perf_counter() - start) * 1000)
    return {"ind_results": [out]}


async def ind_done(s: State) -> dict[str, Any]:
    """지표별 검색이 모두 끝나는 자리 (지표가 1개 이하면 바로 여기로 온다)"""
    return {}


# ---- 합치기·넘기기 ----------------------------------------------------------------


def _years(ind: dict[str, Any]) -> tuple[Any, Any]:
    cond = ind.get("conditions") or {}
    return cond.get("year_from"), cond.get("year_to")


def merge_same(inds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """1위 후보가 같은 표 묶음인 지표들은 지표 하나였다고 보고 합친다
    ('합의 이혼'·'재판 이혼' → 이혼 종류별 이혼). 합친 구절들은 merged_spans에 남긴다.
    연도 조건이 다르면 합치지 않는다 ('2050년 총인구 전망'과 '지금 주민등록인구'는 1위 묶음이
    같아도 다른 표가 필요하다, 2026-11-13 결함 3. 측정 512문항에서는 이 조건으로 바뀐 결과 없음)"""
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
    """넘기기: 기존 결과에 계획과 지표별 후보를 덧붙인다"""
    result = s["result"]
    p = s.get("plan") or {}
    n_planned = len(in_scope(p))
    inds = sorted(s.get("ind_results") or [], key=lambda x: x.get("order", 0))
    inds, merged = merge_same(inds)
    is_multi = len(inds) >= 2
    whole: list[dict[str, Any]] = []
    if is_multi and s.get("slots"):
        # 지표를 잘못 나눴을 때를 대비해 질문 전체 검색의 상위 후보도 함께 넘긴다
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


# ---- 그래프 ----------------------------------------------------------------------


def after_route(s: State) -> str | list[str]:
    if s.get("result") is not None:
        return "save"  # 되묻기 답으로 풀림
    if base.pending(s.get("prev")):
        return "understand"  # 되묻기 대기 중: 계획 없이 기존 흐름 그대로
    return ["understand", "plan"]  # 새 질문: 조건 추출과 계획을 동시에


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
    base.add_search_flow(g)  # 질문 전체 검색 가지
    g.add_edge("plan", "fix_plan")
    g.add_conditional_edges("fix_plan", fan_out, ["indicator_search", "ind_done"])
    g.add_edge("indicator_search", "ind_done")
    # 두 가지가 모두 끝나면 넘기기. 계획 가지가 없던 흐름(되묻기 답)은 save에서 끝난다
    g.add_edge(["save", "ind_done"], "handoff")
    g.add_edge("handoff", END)
    return g.compile()


GRAPH = build()


async def handle(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """dialog.handle과 같은 인자. 새 질문이면 반환값에 multi가 덧붙는다"""
    out = await GRAPH.ainvoke(
        {"question": question, "session_id": session_id, "selections": selections},
        config={"configurable": {"trace": {} if trace is None else trace}},
    )
    return out["result"]
