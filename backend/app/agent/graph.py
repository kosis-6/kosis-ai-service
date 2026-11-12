"""통계표 찾기 한 턴을 LangGraph 노드로 나눈 흐름 (동작은 dialog.handle과 같다)

검색 규칙·점수·프롬프트는 건드리지 않고 기존 함수를 그대로 부른다. 같은 입력이면 같은 결과가 나온다.

    route(갈래) ─ 되묻기 답으로 풀림 ──────────────────────────────► save(상태 저장)
       │ 새 질문
       ▼
    understand(질문 이해) → retrieve(탐색·조건 검증) → review(후보 비교)
       ▲                                                   │
       └── merge(앞 질문 + 답으로 다시) ◄── 되묻기 중인데 거절 ──┤
                                                           ├─ 다시 찾은 뒤 → settle → save
                                                           └─ 그 밖 ────────────► save

노드와 기존 코드
  route       dialog.load_state + dialog.follow_up   되묻기 답이면 검색 없이 멤버·판만 고른다
  understand  search.search의 앞부분                  슬롯 추출 ∥ 질문 임베딩 → 재작성 검색어 평균
  retrieve    search.find                            후보 검색·조건 검증·표 묶음 접기·상태 판단
  review      llm_pick.review                        HCX-007 최종 검토
  merge       dialog.handle의 재시도                  짧은 답이 거절되면 앞 질문에 붙여 다시
  save        dialog.save_state                      되묻기 대기 상태 저장

trace(요청 기록용 dict)는 상태에 넣지 않고 config["configurable"]["trace"]로 넘긴다.
"""

import asyncio
import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.services import dialog, llm_pick, search
from app.services import slots as slot_parser


class State(TypedDict, total=False):
    question: str  # 이번 턴에 사용자가 쓴 말
    session_id: str
    selections: dict[str, Any] | None
    prev: dict[str, Any] | None  # 저장돼 있던 대화 상태 (되묻기 대기 중이면 clarify/ambiguous)
    search_question: str  # 검색에 쓰는 문장 (재시도 때는 앞 질문 + 답)
    slots: dict[str, Any]
    dense: list[float] | None
    result: dict[str, Any] | None
    first_result: dict[str, Any] | None  # 재시도 전 결과 (재시도도 거절이면 이걸 쓴다)
    retried: bool
    turns: int


def trace_of(config: RunnableConfig) -> dict[str, Any]:
    trace = (config.get("configurable") or {}).get("trace")
    return trace if trace is not None else {}  # 빈 dict도 그대로 써야 기록이 호출한 쪽에 남는다


def pending(prev: dict[str, Any] | None) -> bool:
    """되묻고 답을 기다리는 중인가"""
    return bool(prev and prev["status"] in ("clarify", "ambiguous"))


async def route(s: State, config: RunnableConfig) -> dict[str, Any]:
    """갈래: 되묻기 답으로 풀리면 result를 채워 바로 저장으로, 아니면 새 검색"""
    prev = await dialog.load_state(s["session_id"])
    result, turns = None, 0
    if pending(prev):
        # ambiguous에 조건만 답하면 앞 질문으로 다시 검색하므로 앞 질문의 임베딩이 필요하다
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
        "prev": prev,
        "result": result,
        "turns": turns,
        "search_question": s["question"],
        "retried": False,
    }


async def understand(s: State, config: RunnableConfig) -> dict[str, Any]:
    """질문 이해: 슬롯 추출(HCX)과 질문 임베딩을 함께 보내고, 재작성 검색어 임베딩과 평균한다"""
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
    search.ground_conditions(slots)  # 추측 단어 거르기
    trace["timings"] = {
        "slots_ms": round((parsed_at - start) * 1000),
        "embed_ms": round((embedded_at - start) * 1000),
    }
    return {"slots": slots, "dense": dense}


async def retrieve(s: State, config: RunnableConfig) -> dict[str, Any]:
    """탐색: 후보 검색 → 조건 검증·재순위 → 표 묶음 접기 → 멤버·판 선택 → 상태 판단"""
    start = time.perf_counter()
    tables = await search.load_tables()
    result = search.find(
        tables, s["slots"], True, s.get("dense"), scope_by_llm=get_settings().llm_pick
    )
    timings = trace_of(config).setdefault("timings", {})
    timings["search_ms"] = round((time.perf_counter() - start) * 1000)
    return {"result": result}


async def review(s: State, config: RunnableConfig) -> dict[str, Any]:
    """후보 비교: HCX-007이 상위 후보를 보고 맞는 표인지 검토한다 (없으면 범위 밖)"""
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
        # 되묻기에 '초혼'처럼 짧게 답했는데 선택지와 못 맞춰 답만으로 찾다 거절된 경우
        return "merge"
    return "save"


async def merge(s: State) -> dict[str, Any]:
    """앞 질문에 이번 답을 붙여 다시 찾는다"""
    return {
        "first_result": s["result"],
        "retried": True,
        "search_question": f"{s['prev']['slots']['question']} {s['question']}",
    }


async def settle(s: State, config: RunnableConfig) -> dict[str, Any]:
    """다시 찾은 결과가 거절이 아니면 그것을, 거절이면 처음 결과를 쓴다"""
    if s["result"]["status"] not in dialog.REJECTED:
        trace_of(config)["follow_up_merged"] = True
        return {"turns": s["prev"]["clarify_turns"] + 1}
    return {"result": s["first_result"]}


async def save(s: State) -> dict[str, Any]:
    result, turns = s["result"], s.get("turns", 0)
    waiting = result["status"] in ("clarify", "ambiguous")
    await dialog.save_state(s["session_id"], result, turns if waiting else 0)
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
    """understand → retrieve → review → (merge → understand …) → save 까지의 선 (multi.py도 쓴다)"""
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
) -> dict[str, Any]:
    """dialog.handle과 같은 인자·같은 반환값"""
    out = await GRAPH.ainvoke(
        {"question": question, "session_id": session_id, "selections": selections},
        config={"configurable": {"trace": {} if trace is None else trace}},
    )
    return out["result"]
