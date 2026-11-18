"""LangGraph 흐름(app/agent): 기존 통계표 찾기와 결과가 같은지, 지표별 검색이 덧붙는지

HCX·임베딩·DB 없이 돈다. 조건 추출은 원문 규칙만, 최종 검토는 그대로 통과, 세션은 메모리.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import graph, multi
from app.agent import plan as planner
from app.api.deps import get_optional_user
from app.main import app
from app.services import dialog as dl
from app.services import llm_pick, search_log, table_guide
from app.services import search as se
from app.services import slots as sl
from app.services.hcx_client import HcxError
from tests.test_search import _naturalized, _t

TARGET_Q = "한국국적을 취득한 자 통계표"


def _tables() -> list[dict]:
    rows = [
        *_naturalized(),
        _t("W1", "외국인 근로자 현황", 2015, 2024, "FW", survey_name="외국인조사"),
        _t("S1", "외국인 유학생 현황", 2015, 2024, "FS", survey_name="외국인조사"),
        _t("M1", "시도별 혼인", 2015, 2024, "FM", survey_name="혼인조사"),
        _t("D1", "시도별 이혼", 2015, 2024, "FD", survey_name="혼인조사"),
    ]
    for row in rows:
        row["org_id"] = "101"  # 표 카드에 쓰는 칸
    return se.prepare(rows)


@pytest.fixture
def world(monkeypatch):
    """HCX·임베딩·DB를 뺀 세계. plans에 질문별 계획을 넣으면 계획 LLM이 그것을 돌려준다"""
    tables = _tables()
    sessions: dict[str, str] = {}
    plans: dict[str, dict] = {}
    planned: list[str] = []

    async def load_state(session_id):
        saved = sessions.get(session_id)
        return json.loads(saved) if saved else None

    async def save_state(session_id, result, turns):
        waiting = result["status"] == "clarify"
        sessions[session_id] = json.dumps(
            {
                "status": result["status"],
                "family_id": (result.get("family") or {}).get("family_id"),
                "slots": result["slots"],
                "asked": result.get("clarify_axes") if waiting else result.get("options"),
                "clarify_turns": turns,
            },
            ensure_ascii=False,
        )

    async def load_tables():
        return tables

    async def no_dense(question):
        return None

    async def extract(question, trace=None):
        return sl.correct(question, sl.empty_slots(question))

    async def review(question, result, tables, trace=None):
        return result

    async def call(question):
        planned.append(question)
        if isinstance(plans.get(question), Exception):
            raise plans[question]
        return planner.parse(json.dumps(plans.get(question) or {}))

    monkeypatch.setattr(dl, "load_state", load_state)
    monkeypatch.setattr(dl, "save_state", save_state)
    monkeypatch.setattr(se, "load_tables", load_tables)
    monkeypatch.setattr(se, "dense_scores", no_dense)
    monkeypatch.setattr(sl, "extract", extract)
    monkeypatch.setattr(llm_pick, "review", review)
    monkeypatch.setattr(planner, "call", call)

    class World:
        pass

    w = World()
    w.tables, w.plans, w.planned, w.sessions = tables, plans, planned, sessions
    return w


@pytest.mark.parametrize("handle", [dl.handle, graph.handle, multi.handle])
@pytest.mark.parametrize("other_user", [2, None])
def test_search_session_cannot_be_read_or_overwritten_by_another_user(world, handle, other_user):
    trace = {}
    asyncio.run(handle(TARGET_Q, "S_owner", trace=trace, user_id=1))
    original = world.sessions["S_owner"]
    # 다른 계정과 로그아웃한 손님 모두 앞 질문을 붙여 검색하거나 선택지를 읽으면 안 된다.
    found = asyncio.run(handle("여자", "S_owner", trace=trace, user_id=other_user))
    new_id = trace["search_session_id"]
    assert new_id != "S_owner" and world.sessions["S_owner"] == original
    assert found["slots"]["question"] == "여자" and found["turn"] == 0
    assert not trace.get("follow_up") and not trace.get("follow_up_merged")
    assert json.loads(world.sessions[new_id])["slots"][dl.OWNER_KEY] == other_user
    # 원래 계정은 기존 선택지에 계속 답할 수 있다.
    reply = asyncio.run(handle("혼인 귀화자", "S_owner", user_id=1))
    assert reply["table"]["tbl_id"].startswith("혼인 귀화자")


@pytest.mark.parametrize("handle", [dl.handle, graph.handle, multi.handle])
def test_guest_search_session_is_restarted_on_login(world, handle):
    asyncio.run(handle(TARGET_Q, "S_guest"))
    original = world.sessions["S_guest"]
    trace = {}
    found = asyncio.run(handle("여자", "S_guest", trace=trace, user_id=1))
    assert trace["search_session_id"] != "S_guest"
    assert world.sessions["S_guest"] == original and found["slots"]["question"] == "여자"


@pytest.mark.parametrize("handle", [dl.handle, graph.handle, multi.handle])
def test_search_sessions_without_owner_metadata_are_not_reused(world, handle):
    asyncio.run(handle(TARGET_Q, "S_old"))
    legacy = json.loads(world.sessions["S_old"])
    legacy["slots"].pop(dl.OWNER_KEY)
    world.sessions["S_old"] = json.dumps(legacy)
    original = world.sessions["S_old"]
    trace = {}
    found = asyncio.run(handle("여자", "S_old", trace=trace))
    assert trace["search_session_id"] != "S_old"
    assert world.sessions["S_old"] == original and found["slots"]["question"] == "여자"


def test_search_api_returns_and_logs_the_new_session_id(world, monkeypatch):
    logged = []

    async def write(*args, **kwargs):
        logged.append((args[1], kwargs["user_id"]))

    monkeypatch.setattr(search_log, "write", write)
    who = SimpleNamespace(id=1)
    monkeypatch.setitem(app.dependency_overrides, get_optional_user, lambda: who)
    client = TestClient(app)
    first = client.post("/search", json={"question": TARGET_Q})
    assert first.status_code == 200
    original_id = first.json()["session_id"]
    original = world.sessions[original_id]
    who.id = 2
    second = client.post("/search", json={"question": "여자", "session_id": original_id})
    assert second.status_code == 200
    new_id = second.json()["session_id"]
    assert new_id != original_id and world.sessions[original_id] == original
    assert logged == [(original_id, 1), (new_id, 2)]
    assert dl.OWNER_KEY not in second.json()["parsed"]


@pytest.mark.parametrize("search_plan", [False, True])
def test_ask_table_search_returns_and_logs_the_new_session_id(world, monkeypatch, search_plan):
    logged = []

    async def write(*args, **kwargs):
        logged.append((args[1], kwargs["user_id"]))

    monkeypatch.setattr(search_log, "write", write)
    monkeypatch.setattr(table_guide.get_settings(), "search_plan", search_plan)
    first = asyncio.run(table_guide.find_tables(TARGET_Q, "S_ask", None, 1))
    original = world.sessions[first["session_id"]]
    second = asyncio.run(table_guide.find_tables("여자", first["session_id"], None, 2))
    assert second["session_id"] != first["session_id"]
    assert world.sessions[first["session_id"]] == original
    assert logged == [(first["session_id"], 1), (second["session_id"], 2)]


def _run(handle, turns: list, session: str) -> list[dict]:
    out = []
    for turn in turns:
        text, selections = turn if isinstance(turn, tuple) else (turn, None)
        out.append(asyncio.run(handle(text, session, selections)))
    return out


SCENARIOS = [
    ["시도별 혼인"],
    [TARGET_Q, "혼인 귀화자"],
    [TARGET_Q, "혼인 귀화자, 여성, 2010년"],
    [TARGET_Q, "아무거나"],
    [TARGET_Q, ("선택", {"target": "혼인 귀화자", "sex": "여"})],
    [TARGET_Q, "여자", "2010년"],  # 두 번 묻고 기본값
    [TARGET_Q, "여자", "남자"],  # 선택지와 안 맞는 답은 새 질문
    [TARGET_Q, "시도별 혼인"],  # 되묻는 중에 새 질문
    [TARGET_Q, "혼인 귀화자", "시도별 혼인"],
    ["외국인 현황", "2번"],  # 비슷한 표 묶음 고르기
    ["오늘 점심 메뉴"],
]


@pytest.mark.parametrize("turns", SCENARIOS)
def test_graph_gives_the_same_result_as_dialog_handle(world, turns) -> None:
    expected = _run(dl.handle, turns, "dialog")
    got = _run(graph.handle, turns, "graph")
    assert json.dumps(got, ensure_ascii=False, default=str) == json.dumps(
        expected, ensure_ascii=False, default=str
    )


@pytest.mark.parametrize("turns", SCENARIOS)
def test_plan_branch_does_not_change_the_existing_result(world, turns) -> None:
    """계획 가지를 얹어도 기존 결과는 그대로다 (multi만 덧붙는다)"""
    expected = _run(dl.handle, turns, "dialog")
    got = _run(multi.handle, turns, "multi")
    for g in got:
        g.pop("multi", None)
    assert json.dumps(got, ensure_ascii=False, default=str) == json.dumps(
        expected, ensure_ascii=False, default=str
    )


TWO = "혼인이랑 외국인 근로자 현황 같이 보여 줘"
TWO_PLAN = {
    "shared": {},
    "indicators": [
        {"span": "혼인", "search": "시도별 혼인", "scope": "in"},
        {"span": "외국인 근로자 현황", "search": "외국인 근로자 현황", "scope": "in"},
        {"span": "집값", "search": "시도별 주택 가격", "scope": "out"},
    ],
    "combine": {"op": "compare"},
}


def test_several_indicators_each_get_their_own_tables(world) -> None:
    world.plans[TWO] = TWO_PLAN
    result = asyncio.run(multi.handle(TWO, "S"))
    found = result["multi"]
    assert (found["mode"], found["n_planned"], found["n_indicators"]) == ("multi", 2, 2)
    tops = [ind["candidates"][0]["tbl_id"] for ind in found["indicators"]]
    assert tops == ["M1", "W1"]  # 계획이 나눈 순서대로
    assert found["plan"]["out_of_scope"] == ["집값"]
    assert found["whole"]  # 지표를 잘못 나눴을 때를 대비한 질문 전체 검색 후보


def test_one_indicator_runs_no_indicator_search(world) -> None:
    world.plans["시도별 혼인"] = {
        "indicators": [{"span": "시도별 혼인", "search": "시도별 혼인", "scope": "in"}]
    }
    found = asyncio.run(multi.handle("시도별 혼인", "S"))["multi"]
    assert (found["mode"], found["indicators"]) == ("single", [])
    # 지표가 하나여도 질문 전체 검색의 표 묶음 상위 후보는 넘긴다 (뒤 단계가 계산해 볼 재료)
    assert found["whole"][0]["tbl_id"] == "M1"
    assert all("checks" in c for c in found["whole"])


def test_indicators_landing_on_the_same_table_are_one_indicator(world) -> None:
    question = "시도별 혼인 건수와 시도별 혼인 추이"
    world.plans[question] = {
        "indicators": [
            {"span": "시도별 혼인 건수", "search": "시도별 혼인", "scope": "in"},
            {"span": "시도별 혼인 추이", "search": "시도별 혼인", "scope": "in"},
        ]
    }
    found = asyncio.run(multi.handle(question, "S"))["multi"]
    assert (found["mode"], found["n_planned"], found["n_indicators"]) == ("single", 2, 1)
    assert found["merged"] == [["시도별 혼인 건수", "시도별 혼인 추이"]]


def test_failed_plan_still_answers(world) -> None:
    world.plans["시도별 혼인"] = HcxError("42901", "호출 한도 초과", 429)
    result = asyncio.run(multi.handle("시도별 혼인", "S"))
    assert result["table"]["tbl_id"] == "M1"
    assert result["multi"]["mode"] == "single" and "42901" in result["multi"]["plan"]["error"]


def test_reply_to_a_choice_does_not_call_the_plan(world) -> None:
    asyncio.run(multi.handle(TARGET_Q, "S"))
    done = asyncio.run(multi.handle("혼인 귀화자", "S"))
    assert done["table"]["tbl_id"].startswith("혼인 귀화자")
    assert "multi" not in done
    assert world.planned == [TARGET_Q]  # 되묻기 답 턴에는 계획 LLM을 부르지 않는다


def test_merge_same_keeps_first_and_records_spans() -> None:
    inds = [
        {"span": "합의 이혼", "candidates": [{"family_id": "F1"}]},
        {"span": "재판 이혼", "candidates": [{"family_id": "F1"}]},
        {"span": "혼인 건수", "candidates": [{"family_id": "F2"}]},
        {"span": "못 찾음", "candidates": []},
    ]
    kept, merged = multi.merge_same(inds)
    assert [i["span"] for i in kept] == ["합의 이혼", "혼인 건수", "못 찾음"]
    assert merged == [["합의 이혼", "재판 이혼"]]


def test_response_gets_cards_per_indicator_only_for_several_indicators(world) -> None:
    world.plans[TWO] = TWO_PLAN
    result = asyncio.run(multi.handle(TWO, "S"))
    view = table_guide.indicator_tables(result["multi"], world.tables)
    assert [i["indicator"] for i in view["indicators"]] == ["혼인", "외국인 근로자 현황"]
    first = view["indicators"][0]["tables"][0]
    assert (first["rank"], first["tbl_id"], first["tbl_nm"]) == (1, "M1", "시도별 혼인")
    assert view["combine"] == {"op": "compare"} and view["out_of_scope"] == ["집값"]

    assert table_guide.indicator_tables({"mode": "single"}, world.tables) is None
    assert table_guide.indicator_tables(None, world.tables) is None
