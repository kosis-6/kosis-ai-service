"""연도 되묻기(ask_followup)와 최근·최신 질문의 갱신 확인(table_cache.refresh_if_stale)
(DB·KOSIS·HCX 없이 바꿔 끼운다)
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api.routes import ask as ask_route
from app.services import ask_followup, indicators, load_queue, lookup, qa, table_cache

# ---- 되묻기 판정 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["과거와 최근 인구수 비교해줘", "예전보다 출생아 수 늘었어?", "옛날 서울 인구는?"],
)
def test_vague_past_without_year_is_asked_back(question) -> None:
    assert ask_followup.needs_year(question)


@pytest.mark.parametrize(
    "question",
    [
        "2000년과 최근 인구수 비교",  # 연도가 있음
        "'95년이랑 예전 비교",  # 두 자리 연도
        "과거 5년 전과 비교",  # 상대 시점
        "최근 10년 과거 추이",  # 최근 N년
        "최근 서울 인구수",  # 최근·최신은 되묻지 않는다
        "서울 인구수는?",  # 시점 표현 없음 → 가장 최근
        "과거 5년간 출생아 수 추이",  # 과거 N년 = 최근 N년
        "과거 10년 동안 서울 인구 변화",
        "과거부터 지금까지 합계출산율 추이",  # 수록 기간 전체
    ],
)
def test_explicit_or_recent_is_not_asked_back(question) -> None:
    assert not ask_followup.needs_year(question)


@pytest.mark.parametrize(
    ("pending", "reply", "merged"),
    [
        ("과거와 최근 인구수 비교해줘", "2000년이랑", "2000년과 최근 인구수 비교해줘"),
        ("예전보다 출생아 수 늘었어?", "2000년", "2000년보다 출생아 수 늘었어?"),
        ("옛날이랑 지금 미혼 비율", "'95년", "1995년이랑 지금 미혼 비율"),
        ("과거와 최근 비교", "2000년이랑 2010년", "과거와 최근 비교 (2000년이랑 2010년)"),
        ("과거와 최근 비교", "2000년, 2010년", "과거와 최근 비교 (2000년, 2010년)"),
        ("과거와 최근 비교", "2000년이요", "2000년과 최근 비교"),
    ],
)
def test_reply_is_merged_into_pending_question(pending, reply, merged) -> None:
    assert ask_followup.merge(pending, reply) == merged


@pytest.mark.parametrize(
    "reply",
    [
        "응",
        "부산 2010년 출생아 수 알려줘 그리고 서울도 같이 보여줘",
        # 짧고 연도가 있어도 연도·조사 말고 다른 말이 남으면 새 질문
        "2020년 출생아 수",
        "아니 됐고 2024년 혼인 건수",
        "작년 서울 인구",
    ],
)
def test_non_year_or_long_reply_is_a_new_question(reply) -> None:
    assert ask_followup.merge("과거와 최근 인구수 비교해줘", reply) is None


def test_missing_year_lists_db_and_kosis_years(monkeypatch) -> None:
    spec = {
        "name": "미혼 비율",
        "method": "lookup",
        "sources": [
            {"table": "T_NEW", "level": "시군구"},
            {"table": "T_OLD", "years": [1980, 1980]},
        ],
    }
    monkeypatch.setattr(indicators, "spec", lambda name: spec)
    monkeypatch.setattr(lookup, "_item_years", lambda tbl, items: (2022, 2025))
    monkeypatch.setattr(indicators, "_catalog_rows", lambda sql, params: [{"lo": 1970}])
    r = ask_followup.missing_year({"indicator": "미혼 비율"})
    assert r["status"] == "missing_year" and r["available"] == "1980년, 2022~2025년"
    text = qa._refusal_text(r)
    assert "미혼 비율 자료는 1980년, 2022~2025년이 있습니다." in text
    assert "KOSIS에는 1970년부터" in text and "(예: 1980년과 2025년)" in text


def test_adjacent_spans_are_merged() -> None:
    """같은 조사의 과거 표(1970~1989)와 지금 표(1990~)는 한 기간으로 알린다"""
    assert ask_followup._span_text([(1990, 2025), (1970, 1989)]) == "1970~2025년"
    assert ask_followup._span_text([(1980, 1980), (1985, 1985), (2022, 2025)]) == (
        "1980년, 1985년, 2022~2025년"
    )
    census = [(y, y) for y in range(1970, 2021, 5)]
    assert ask_followup._span_text([*census, (2022, 2025)]) == "1970~2020년(5년마다), 2022~2025년"


def test_missing_year_without_indicator_only_asks_year(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "spec", lambda name: None)
    r = ask_followup.missing_year({})
    assert qa._refusal_text(r).endswith("(예: 2010년과 2025년).")


# ---- qa.ask 연결 -------------------------------------------------------------------------


@pytest.fixture
def ask_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"computed": 0, "refresh": [], "queued": [], "outcome": "fresh"}

    async def understand(question, trace=None):
        return {"type": "value", "indicator": "평균 초혼연령", "regions": ["전국"]}

    async def compute_fetching(parsed, trace):
        state["computed"] += 1
        return {"status": "ok", "notes": []}

    async def table_info(tbl_id):
        return {"org_id": "101"}

    async def refresh(tbl_id, org_id, step=None):
        state["refresh"].append(tbl_id)
        step.update(refresh_db_year=2024, refresh_kosis_year=2025)
        return state["outcome"]

    async def enqueue(tbl_id, reason):
        state["queued"].append((tbl_id, reason))
        return {"id": 1, "status": "queued"}

    monkeypatch.setattr(qa, "understand", understand)
    monkeypatch.setattr(qa, "_compute_fetching", compute_fetching)
    monkeypatch.setattr(qa, "summary", lambda r: "요약")
    monkeypatch.setattr(qa, "render", lambda r: "")
    monkeypatch.setattr(load_queue, "table_info", table_info)
    monkeypatch.setattr(load_queue, "enqueue", enqueue)
    monkeypatch.setattr(table_cache, "refresh_if_stale", refresh)
    monkeypatch.setattr(
        ask_followup, "missing_year", lambda parsed: {"status": "missing_year", "reason": "?"}
    )
    return state


def test_vague_past_question_is_not_computed(ask_env) -> None:
    res = asyncio.run(qa.ask("예전보다 평균 초혼연령이 늦어졌어?"))
    assert res["status"] == "missing_year" and ask_env["computed"] == 0


def test_unknown_indicator_is_not_asked_back(ask_env, monkeypatch) -> None:
    """지표 사전에 없는 지표는 연도를 되묻지 않고 계산(지원하지 않는 지표 안내)으로 간다"""

    async def understand(question, trace=None):
        return {"type": "compare", "indicator": "반려동물 수", "regions": ["전국"]}

    monkeypatch.setattr(qa, "understand", understand)
    asyncio.run(qa.ask("과거와 최근 반려동물 수 비교해줘"))
    assert ask_env["computed"] == 1


def test_recent_question_checks_kosis_for_newer_year(ask_env) -> None:
    res = asyncio.run(qa.ask("최근 평균 초혼연령은?"))
    assert ask_env["refresh"] == ["DT_1B83A05"] and ask_env["queued"] == []
    assert res["trace"]["refreshed"][0]["refresh"] == "fresh"


def test_question_without_recent_word_is_not_checked(ask_env) -> None:
    asyncio.run(qa.ask("2024년 평균 초혼연령은?"))
    assert ask_env["refresh"] == []


@pytest.mark.parametrize("outcome", ["too_big", "failed", "timeout", "busy"])
def test_newer_year_not_fetched_now_is_queued_as_update(ask_env, outcome) -> None:
    ask_env["outcome"] = outcome
    res = asyncio.run(qa.ask("최신 평균 초혼연령"))
    assert ask_env["queued"] == [("DT_1B83A05", "update")]
    assert any("KOSIS에 2025년 자료가 새로 있어 받고 있습니다" in n for n in res["result"]["notes"])


def test_computed_indicator_is_not_refreshed(ask_env, monkeypatch) -> None:
    async def understand(question, trace=None):
        return {"type": "value", "indicator": "총인구", "regions": ["전국"]}

    monkeypatch.setattr(qa, "understand", understand)
    asyncio.run(qa.ask("최근 인구수"))
    assert ask_env["refresh"] == []  # mart.population은 표를 다시 받아도 반영되지 않는다


# ---- table_cache.refresh_if_stale --------------------------------------------------------


@pytest.fixture
def stale(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"db": 2024, "kosis": 2025, "fetched": []}

    async def db_last(tbl_id):
        return state["db"]

    async def kosis_last(tbl_id, org_id):
        return state["kosis"]

    async def guarded(tbl_id, org_id, trace, force):
        state["fetched"].append((tbl_id, force))
        return "loaded"

    monkeypatch.setattr(table_cache, "_db_last_year", db_last)
    monkeypatch.setattr(table_cache, "_kosis_last_year", kosis_last)
    monkeypatch.setattr(table_cache, "_guarded_fetch", guarded)
    monkeypatch.setattr(table_cache, "_checked", {})
    monkeypatch.setattr(table_cache, "_locks", {})
    monkeypatch.setattr(table_cache, "INLINE_MAX_REQUESTS", 8)
    return state


def test_newer_kosis_year_is_fetched_again(stale) -> None:
    trace = {}
    assert asyncio.run(table_cache.refresh_if_stale("T", "101", trace)) == "loaded"
    assert stale["fetched"] == [("T", True)]  # 이미 적재된 표라도 다시 받는다
    assert (trace["refresh_db_year"], trace["refresh_kosis_year"]) == (2024, 2025)


def test_up_to_date_table_is_not_fetched(stale) -> None:
    stale["kosis"] = 2024
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "fresh"
    assert stale["fetched"] == []


def test_checked_once_a_day(stale) -> None:
    stale["kosis"] = 2024
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "fresh"
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "skipped"


def test_not_loaded_table_is_left_to_inline_fetch(stale) -> None:
    stale["db"] = None
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "skipped"


def test_kosis_check_failure_does_not_raise(stale, monkeypatch) -> None:
    async def boom(tbl_id, org_id):
        raise RuntimeError("40: 호출 초과 (https://x?apiKey=SECRET)")

    monkeypatch.setattr(table_cache, "_kosis_last_year", boom)
    trace = {}
    assert asyncio.run(table_cache.refresh_if_stale("T", "101", trace)) == "failed"
    assert "SECRET" not in trace["refresh_error"]


def test_failed_check_is_retried_next_question(stale, monkeypatch) -> None:
    """KOSIS 확인이 잠깐 실패해도 하루를 기다리지 않고 다음 질문에서 다시 확인한다"""
    calls = []

    async def flaky(tbl_id, org_id):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("일시 오류")
        return 2024

    monkeypatch.setattr(table_cache, "_kosis_last_year", flaky)
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "failed"
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "fresh"
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "skipped"


def test_newer_year_not_fetched_now_is_still_checked_once(stale, monkeypatch) -> None:
    """새 연도를 확인했지만 바로 못 받으면(대기열이 맡음) 그날은 다시 확인하지 않는다"""

    async def busy(tbl_id, org_id, trace, force):
        return "busy"

    monkeypatch.setattr(table_cache, "_guarded_fetch", busy)
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "busy"
    assert asyncio.run(table_cache.refresh_if_stale("T", "101")) == "skipped"


# ---- /ask 대화 이어 받기 ------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {"asked": [], "saved": [], "pending": None}

    async def fake_ask(question):
        state["asked"].append(question)
        status = "missing_year" if ask_followup.needs_year(question) else "ok"
        return {"status": status, "answer": "답", "parsed": {}, "result": {}, "sources": []}

    async def load_pending(session_id):
        return state["pending"]

    async def save(session_id, status, question):
        state["saved"].append((session_id, status, question))

    monkeypatch.setattr(qa, "ask", fake_ask)
    monkeypatch.setattr(qa, "log_request", lambda *a, **k: None)
    monkeypatch.setattr(qa, "log_missing", lambda *a, **k: None)
    monkeypatch.setattr(ask_route.ask_followup, "load_pending", load_pending)
    monkeypatch.setattr(ask_route.ask_followup, "save", save)
    return state


def test_ask_back_returns_session_and_merges_reply(api) -> None:
    client = TestClient(main.app)
    first = client.post("/ask", json={"question": "과거와 최근 인구수 비교해줘"}).json()
    assert first["status"] == "missing_year" and first["session_id"].startswith("A_")
    assert api["saved"][-1][1:] == ("missing_year", "과거와 최근 인구수 비교해줘")

    api["pending"] = "과거와 최근 인구수 비교해줘"
    second = client.post(
        "/ask", json={"question": "2000년", "session_id": first["session_id"]}
    ).json()
    assert second["question"] == "2000년과 최근 인구수 비교해줘" and second["status"] == "ok"
    assert api["saved"][-1] == (first["session_id"], "ok", "2000년과 최근 인구수 비교해줘")


def test_ordinary_question_without_session_saves_nothing(api) -> None:
    res = TestClient(main.app).post("/ask", json={"question": "2025년 서울 인구수"}).json()
    assert res["session_id"] is None and api["saved"] == []
