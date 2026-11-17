"""동적 지표 4단계: 계산 방법 캐시 (plan_cache.py), 서버 메모리 캐시 (memory_cache.py)

DB 없이: plan_cache의 DB 함수를 바꿔 끼우고, 메모리 캐시는 가짜 시계로 만료를 본다.
"""

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

# ---- 메모리 캐시 -------------------------------------------------------------------


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
    assert cache.get("a") == 1  # a를 최근에 씀 → b가 가장 오래됨
    cache.set("c", 3)
    assert cache.get("b") is MISSING and cache.get("a") == 1 and cache.get("c") == 3
    clock.now = 11  # ttl이 지나면 없음
    assert cache.get("a") is MISSING
    stats = cache.stats()
    assert (stats["hits"], stats["misses"], stats["size"]) == (3, 2, 1)
    cache.delete("c")
    assert cache.stats()["size"] == 0


def test_memory_cache_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # 캐시를 켤 때와 끌 때의 응답 시간을 비교하려고 끌 수 있다
    monkeypatch.setenv("MEMORY_CACHE", "0")
    cache = MemoryCache("t")
    cache.set("a", 1)
    assert cache.get("a") is MISSING and cache.stats()["enabled"] is False


# ---- 키 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parsed", "key"),
    [
        ({"term": "1인 가구 중 여성 비율"}, "1인가구중여성비율"),
        (
            {"term": "2020년 서울", "indicator": "1인 가구 수"},
            "1인가구수",
        ),  # 연도 섞인 표현은 건너뜀
        ({"term": "서울 1인 가구", "indicator": None}, None),  # 지역 섞인 표현
        ({"term": "출생아 수"}, None),  # 사전 지표는 캐시하지 않는다
        ({}, None),
    ],
)
def test_key_of(monkeypatch: pytest.MonkeyPatch, parsed: dict, key: str | None) -> None:
    monkeypatch.setattr(indicators, "find_regions", lambda t: ["서울"] if "서울" in t else [])
    found = plan_cache.key_of(parsed)
    assert (found[0] if found else None) == key


# ---- 캐시에서 꺼내 계산하기 ------------------------------------------------------------

PARSED = {"type": "value", "term": "1인 가구 수", "regions": ["서울"], "year": 2020}
PLAN = {"id": 5, "key": "1인가구수", "tbl_id": "DT_1PL1502", "forced": {"3": "2"}}


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"plans": [PLAN], "used": [], "saved": [], "tried": []}
    monkeypatch.setattr(plan_cache, "key_of", lambda parsed: ("1인가구수", "1인 가구 수"))

    async def find(key):
        return state["plans"]

    async def used(plan_id):
        state["used"].append(plan_id)

    async def save(key, term, tbl_id, forced, spec):
        state["saved"].append((key, tbl_id, forced, spec["name"]))

    async def fake_try(tbl_id, org_id, question, parsed, trace, forced):
        state["tried"].append((tbl_id, forced))
        if state.get("fail"):
            return None, None
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
    res = asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, trace))
    assert res["status"] == "ok"
    # 저장한 표와 되묻기에서 고른 칸으로 다시 계산 (칸 고르기·검증은 _try가 다시 한다)
    assert plans["tried"] == [("DT_1PL1502", {3: "2"})]
    assert plans["used"] == [5] and trace["plan_cache"] == {"hit": 5, "tbl_id": "DT_1PL1502"}


def test_cached_plan_that_no_longer_computes_is_a_miss(plans: dict) -> None:
    plans["fail"] = True  # 표가 다시 적재돼 칸이 없어짐 등
    trace: dict = {}
    assert asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", PARSED, trace)) is None
    assert trace["plan_cache"] == "miss" and plans["used"] == []


def test_success_from_search_is_remembered(plans: dict) -> None:
    found = {"status": "ok", "recommendations": [{"tbl_id": "DT_1PL1502"}]}
    asyncio.run(d.answer("2020년 서울 1인 가구 수", PARSED, found, {}))
    assert plans["saved"] == [("1인가구수", "DT_1PL1502", {}, "1인 가구 수")]


def test_choice_is_remembered_with_forced_cell(plans: dict) -> None:
    context = {"tbl_id": "DT_1PL1502", "dim_no": 3, "parsed": PARSED, "forced": {}}
    asyncio.run(d.answer_choice("서울 1인 가구 수", context, "2", {}))
    assert plans["saved"] == [("1인가구수", "DT_1PL1502", {3: "2"}, "1인 가구 수")]


def test_cache_errors_do_not_break_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(parsed):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(plan_cache, "key_of", broken)
    assert asyncio.run(d.cached_answer("질문", PARSED, {})) is None


# ---- 관리자 API --------------------------------------------------------------------


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
