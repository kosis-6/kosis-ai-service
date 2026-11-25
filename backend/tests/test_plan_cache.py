import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.db import get_session
from app.main import app
from app.services import dynamic_indicator as d
from app.services import indicators, plan_cache
from app.services.memory_cache import MISSING, MemoryCache


@pytest.fixture(autouse=True)
def plan_cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "plan_cache", True)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_memory_cache_lru_and_ttl() -> None:
    clock = Clock()
    cache = MemoryCache("t", maxsize=2, ttl=10, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is MISSING and cache.get("a") == 1 and cache.get("c") == 3
    clock.now = 11
    assert cache.get("a") is MISSING
    stats = cache.stats()
    assert (stats["hits"], stats["misses"], stats["size"]) == (3, 2, 1)
    cache.delete("c")
    assert cache.stats()["size"] == 0


def test_memory_cache_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_CACHE", "0")
    cache = MemoryCache("t")
    cache.set("a", 1)
    assert cache.get("a") is MISSING and cache.stats()["enabled"] is False


@pytest.mark.parametrize(
    ("parsed", "key"),
    [
        ({"term": "1인 가구 중 여성 비율"}, "1인가구중여성비율"),
        (
            {"term": "2020년 서울", "indicator": "다문화가구 수"},
            "다문화가구수",
        ),  # 연도 섞인 표현은 건너뜀 (사전에 없는 예문: 1인 가구는 11/25 사전에 들어갔다)
        ({"term": "서울 1인 가구", "indicator": None}, None),  # 지역 섞인 표현
        # 전국 범위 말은 지표가 아니다: "1인 가구 수"와 같은 계획을 쓴다 (전에는 전국1인가구수)
        ({"term": "전국 다문화가구 수"}, "다문화가구수"),
        ({"term": "우리나라 다문화가구 수"}, "다문화가구수"),
        ({"term": "국내이동자 수"}, "국내이동자수"),  # 국내이동은 지표 이름이다
        # '국내에서 태어난'은 조건이다: "태어난 다문화가구 자녀 수"와 같은 키가 되면 안 된다
        ({"term": "국내에서 태어난 다문화가구 자녀 수"}, "국내에서태어난다문화가구자녀수"),
        ({"term": "국내에서 출생한 외국인 자녀 수"}, "국내에서출생한외국인자녀수"),
        ({"term": "출생아 수"}, None),
        ({}, None),
    ],
)
def test_key_of(monkeypatch: pytest.MonkeyPatch, parsed: dict, key: str | None) -> None:
    monkeypatch.setattr(indicators, "find_regions", lambda t: ["서울"] if "서울" in t else [])
    found = plan_cache.key_of(parsed)
    assert (found[0] if found else None) == key


PARSED = {"type": "value", "term": "1인 가구 수", "regions": ["서울"], "year": 2020}
PLAN = {"id": 5, "key": "1인가구수", "tbl_id": "DT_1PL1502", "forced": {"0": "2"}}


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"plans": [PLAN], "used": [], "saved": [], "tried": []}
    monkeypatch.setattr(plan_cache, "key_of", lambda parsed: ("1인가구수", "1인 가구 수"))

    async def find(key):
        return state["plans"]

    async def used(plan_id):
        state["used"].append(plan_id)

    async def save(key, term, tbl_id, forced, spec, words=()):
        state["saved"].append((key, tbl_id, forced, spec["name"]))
        state.setdefault("words", []).append(list(words or []))

    def schema(tbl_id):
        dims = [
            d.Dim(0, "항목", {"1": "1인가구", "3": "남자", "2": "여자"}),
            d.Dim(1, "행정구역별", {"00": "전국", "11": "서울특별시"}),
            d.Dim(4, "혼인상태별", {"0": "계", "M": "유배우", "S": "미혼"}),
        ]
        return dims, 1, "1인가구"

    monkeypatch.setattr(d, "schema", schema)

    async def fake_try(tbl_id, org_id, question, parsed, trace, forced, slots=None, notice=None):
        state["tried"].append((tbl_id, forced))
        trace.setdefault("dynamic", []).append({"tbl_id": tbl_id})
        if state.get("fail"):
            return None, None
        if state.get("status"):
            return {"status": state["status"], "parsed": parsed, "result": {}}, None
        spec = {"name": "1인 가구 수"}
        res = {"status": "ok", "parsed": {**parsed, "dynamic_spec": spec}, "result": {"value": 1}}
        return res, None

    monkeypatch.setattr(plan_cache, "find", find)
    monkeypatch.setattr(plan_cache, "used", used)
    monkeypatch.setattr(plan_cache, "save", save)
    monkeypatch.setattr(d, "_try", fake_try)
    return state


def test_cached_plan_skips_table_search(plans: dict) -> None:
    trace: dict = {}
    res = asyncio.run(d.cached_answer("2020년 서울 여자 1인 가구 수", PARSED, trace))
    assert res["status"] == "ok"
    assert plans["tried"] == [("DT_1PL1502", {0: "2"})]
    assert plans["used"] == [5] and trace["plan_cache"] == {"hit": 5, "tbl_id": "DT_1PL1502"}


def test_cached_cell_is_not_used_when_new_question_names_another_cell(plans: dict) -> None:
    trace: dict = {}
    asyncio.run(d.cached_answer("2020년 서울 남자 1인 가구 수", PARSED, trace))
    assert plans["tried"] == [("DT_1PL1502", {})]
    assert trace["dynamic"][-1]["cache_dropped"] == {0: "2"}


def test_cached_cell_without_grounds_in_new_question_is_dropped(plans: dict) -> None:
    asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, {}))
    assert plans["tried"] == [("DT_1PL1502", {})]


def test_cached_llm_cell_kept_when_its_words_are_in_new_question(plans: dict) -> None:
    plans["plans"] = [{**PLAN, "forced": {"4": "M"}, "words": [["결혼한", "유배우"]]}]
    asyncio.run(d.cached_answer("2020년 서울 결혼한 1인 가구 수", PARSED, {}))
    assert plans["tried"] == [("DT_1PL1502", {4: "M"})]
    plans["tried"].clear()
    asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, {}))
    assert plans["tried"] == [("DT_1PL1502", {})]


def test_cached_total_cell_is_kept(plans: dict) -> None:
    plans["plans"] = [{**PLAN, "forced": {"4": "0"}}]
    asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, {}))
    assert plans["tried"] == [("DT_1PL1502", {4: "0"})]


def test_cached_plan_that_no_longer_computes_is_a_miss(plans: dict) -> None:
    plans["fail"] = True
    trace: dict = {}
    assert asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, trace)) is None
    assert trace["plan_cache"] == "miss" and plans["used"] == []


def test_success_from_search_is_remembered(plans: dict) -> None:
    found = {"status": "ok", "recommendations": [{"tbl_id": "DT_1PL1502"}]}
    asyncio.run(d.answer("2020년 서울 1인 가구 수", PARSED, found, {}))
    assert plans["saved"] == [("1인가구수", "DT_1PL1502", {}, "1인 가구 수")]


def test_choice_is_remembered_without_picked_cell(plans: dict) -> None:
    context = {"tbl_id": "DT_1PL1502", "dim_no": 3, "parsed": PARSED, "forced": {}}
    asyncio.run(d.answer_choice("서울 1인 가구 수", context, "2", {}))
    assert plans["tried"] == [("DT_1PL1502", {3: "2"})]
    assert plans["saved"] == [("1인가구수", "DT_1PL1502", {}, "1인 가구 수")]


def test_cached_plan_without_data_falls_back_to_search(plans: dict) -> None:
    plans["status"] = "out_of_range"
    trace: dict = {}
    assert asyncio.run(d.cached_answer("2010년 서울 1인 가구 수", PARSED, trace)) is None
    assert "out_of_range" in trace["dynamic"][0]["skip"] and plans["used"] == []


def test_cache_errors_do_not_break_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(parsed):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(plan_cache, "key_of", broken)
    assert asyncio.run(d.cached_answer("질문", PARSED, {})) is None


@pytest.fixture
def admin() -> TestClient:
    async def no_db():
        yield None

    app.dependency_overrides[get_session] = no_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, is_admin=True)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_lists_and_drops_plans(admin: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def listing(limit, offset):
        return {"total": 1, "items": [{"id": 5, "term": "1인 가구 수"}], "memory": {}}

    async def drop(plan_id):
        return plan_id == 5

    monkeypatch.setattr(plan_cache, "listing", listing)
    monkeypatch.setattr(plan_cache, "drop", drop)
    res = admin.get("/admin/dynamic-plans")
    assert res.status_code == 200 and res.json()["items"][0]["term"] == "1인 가구 수"
    assert res.headers["cache-control"] == "no-store"
    assert admin.delete("/admin/dynamic-plans/5").status_code == 204
    assert admin.delete("/admin/dynamic-plans/6").status_code == 404


def test_plans_are_admin_only(admin: TestClient) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, is_admin=False)
    assert admin.get("/admin/dynamic-plans").status_code == 403
