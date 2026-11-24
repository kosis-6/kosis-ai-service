import asyncio
import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.services import dialog, llm_pick, search
from app.services import slots as slot_parser


class State(TypedDict, total=False):
    question: str
    session_id: str
    user_id: int | None
    selections: dict[str, Any] | None
    prev: dict[str, Any] | None
    search_question: str
    slots: dict[str, Any]
    dense: list[float] | None
    result: dict[str, Any] | None
    first_result: dict[str, Any] | None
    retried: bool
    turns: int


def trace_of(config: RunnableConfig) -> dict[str, Any]:
    trace = (config.get("configurable") or {}).get("trace")
    return trace if trace is not None else {}


def pending(prev: dict[str, Any] | None) -> bool:
    return bool(prev and prev["status"] in ("clarify", "ambiguous"))


async def route(s: State, config: RunnableConfig) -> dict[str, Any]:
    session_id, prev = await dialog.owned_session(s["session_id"], s.get("user_id"))
    trace_of(config)["search_session_id"] = session_id
    result, turns = None, 0
    if pending(prev):
        dense = None
        if prev["status"] == "ambiguous":
            dense = await search.dense_scores(prev["slots"]["question"])
        result = dialog.follow_up(
            await search.load_tables(), prev, s["question"], s.get("selections"), dense
        )
        if result is not None:
            turns = prev["clarify_turns"] + 1
            trace_of(config)["follow_up"] = True
    return {
        "session_id": session_id,
        "prev": prev,
        "result": result,
        "turns": turns,
        "search_question": s["question"],
        "retried": False,
    }


async def understand(s: State, config: RunnableConfig) -> dict[str, Any]:
    trace, question = trace_of(config), s["search_question"]
    start = time.perf_counter()

    async def timed(coro: Any) -> tuple[Any, float]:
        value = await coro
        return value, time.perf_counter()

    (slots, parsed_at), (dense, embedded_at) = await asyncio.gather(
        timed(slot_parser.extract(question, trace)), timed(search.dense_scores(question))
    )
    rewrite = (slots.get("search_query") or "").strip()
    trace["embedding"] = {"question": dense is not None, "rewrite": None}
    if dense is not None and rewrite and rewrite != question.strip():
        dense2, embedded_at = await timed(search.dense_scores(rewrite))
        trace["embedding"]["rewrite"] = dense2 is not None
        if dense2 is not None:
            dense = [(a + b) / 2 for a, b in zip(dense, dense2, strict=True)]
    search.ground_conditions(slots)
    trace["timings"] = {
        "slots_ms": round((parsed_at - start) * 1000),
        "embed_ms": round((embedded_at - start) * 1000),
    }
    return {"slots": slots, "dense": dense}


async def retrieve(s: State, config: RunnableConfig) -> dict[str, Any]:
    start = time.perf_counter()
    tables = await search.load_tables()
    result = search.find(
        tables, s["slots"], True, s.get("dense"), scope_by_llm=get_settings().llm_pick
    )
    timings = trace_of(config).setdefault("timings", {})
    timings["search_ms"] = round((time.perf_counter() - start) * 1000)
    return {"result": result}


async def review(s: State, config: RunnableConfig) -> dict[str, Any]:
    start = time.perf_counter()
    tables = await search.load_tables()
    result = await llm_pick.review(s["search_question"], s["result"], tables, trace_of(config))
    timings = trace_of(config).setdefault("timings", {})
    timings["llm_pick_ms"] = round((time.perf_counter() - start) * 1000)
    return {"result": result}


def after_review(s: State) -> str:
    if s.get("retried"):
        return "settle"
    if pending(s.get("prev")) and s["result"]["status"] in dialog.REJECTED:
        return "merge"
    return "save"


async def merge(s: State) -> dict[str, Any]:
    return {
        "first_result": s["result"],
        "retried": True,
        "search_question": f"{s['prev']['slots']['question']} {s['question']}",
    }


async def settle(s: State, config: RunnableConfig) -> dict[str, Any]:
    if s["result"]["status"] not in dialog.REJECTED:
        trace_of(config)["follow_up_merged"] = True
        return {"turns": s["prev"]["clarify_turns"] + 1}
    return {"result": s["first_result"]}


async def save(s: State) -> dict[str, Any]:
    result, turns = s["result"], s.get("turns", 0)
    waiting = result["status"] in ("clarify", "ambiguous")
    await dialog.save_state(
        s["session_id"], dialog.owned_result(result, s.get("user_id")), turns if waiting else 0
    )
    result["turn"] = turns
    return {"result": result}


NODES = (
    ("route", route),
    ("understand", understand),
    ("retrieve", retrieve),
    ("review", review),
    ("merge", merge),
    ("settle", settle),
    ("save", save),
)


def add_search_flow(g: StateGraph) -> None:
    g.add_edge("understand", "retrieve")
    g.add_edge("retrieve", "review")
    g.add_conditional_edges("review", after_review, ["merge", "settle", "save"])
    g.add_edge("merge", "understand")
    g.add_edge("settle", "save")


def build():
    g = StateGraph(State)
    for name, fn in NODES:
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges(
        "route",
        lambda s: "save" if s.get("result") is not None else "understand",
        ["save", "understand"],
    )
    add_search_flow(g)
    g.add_edge("save", END)
    return g.compile()


GRAPH = build()


async def handle(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    out = await GRAPH.ainvoke(
        {
            "question": question,
            "session_id": session_id,
            "selections": selections,
            "user_id": user_id,
        },
        config={"configurable": {"trace": {} if trace is None else trace}},
    )
    return out["result"]
