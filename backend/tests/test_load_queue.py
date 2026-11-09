"""적재 안 된 표 → 적재 대기열 + data_loading 답변, 관리자 대기열 API
(DB 없이 load_queue 함수를 바꿔 끼운다)
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api.deps import get_current_user
from app.core.stats_db import StatsTableMissing
from app.services import load_queue, qa

TBL = "DT_1B26001"
INFO = {
    "tbl_id": TBL,
    "org_id": "101",
    "name": "시군구/성/연령(5세)별 이동자수",
    "source": f"국가데이터처, 시군구/성/연령(5세)별 이동자수 ({TBL})",
    "url": f"https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId={TBL}",
}


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> list:
    calls = []

    async def table_info(tbl_id):
        if tbl_id != TBL:
            raise load_queue.UnknownTable(tbl_id)
        return INFO

    async def enqueue(tbl_id, reason):
        calls.append((tbl_id, reason))
        return {"id": 1, "status": "queued", "requested_count": len(calls), "created": True}

    monkeypatch.setattr(load_queue, "table_info", table_info)
    monkeypatch.setattr(load_queue, "enqueue", enqueue)
    return calls


def _ask_with(monkeypatch, error):
    async def understand(question, trace=None):
        return {"type": "value", "indicator": "전입자 수", "regions": ["서울"]}

    def compute(parsed):
        raise error

    monkeypatch.setattr(qa, "understand", understand)
    monkeypatch.setattr(qa, "compute", compute)
    return asyncio.run(qa.ask("서울 전입자 수"))


def test_unloaded_table_is_queued_and_answered_as_loading(monkeypatch, queue) -> None:
    res = _ask_with(monkeypatch, StatsTableMissing("적재 안 됨", TBL))
    assert queue == [(TBL, "question")]
    assert res["status"] == "data_loading"
    assert res["answer"].startswith("이 질문에 필요한 통계표(시군구/성/연령(5세)별 이동자수)는")
    assert "몇 분 뒤 다시 질문해 주세요." in res["answer"]
    assert res["answer"].endswith(f"통계표: {INFO['url']}")
    assert res["sources"][0]["tbl_id"] == TBL
    assert qa.log_missing("서울 전입자 수", res)  # 답하지 못한 질문 기록에도 남는다


def test_missing_schema_still_raises(monkeypatch, queue) -> None:
    # 표를 모르면(DB 자체가 비어 있음) 대기열에 넣지 않고 원래 오류 → /ask 503
    with pytest.raises(StatsTableMissing):
        _ask_with(monkeypatch, StatsTableMissing("통계 테이블이 없습니다"))
    assert queue == []


def test_unknown_table_is_not_reported_as_loading(monkeypatch, queue) -> None:
    # 메타데이터에 없는 표는 적재할 수 없으니 "준비 중"이라고 답하지 않는다
    with pytest.raises(StatsTableMissing):
        _ask_with(monkeypatch, StatsTableMissing("적재 안 됨", "DT_NOPE"))
    assert queue == []


def test_startup_queues_indicator_tables(monkeypatch) -> None:
    seen = []

    async def enqueue_missing(tbl_ids, reason):
        seen.append((tuple(tbl_ids), reason))
        return []

    monkeypatch.setattr(load_queue, "enqueue_missing", enqueue_missing)
    monkeypatch.setattr(main.indicators, "spec_tables", lambda: ["A", "B"])
    asyncio.run(main._queue_indicator_tables())
    assert seen == [(("A", "B"), "indicator")]


def test_startup_survives_db_error(monkeypatch) -> None:
    async def broken(*a):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(load_queue, "enqueue_missing", broken)
    asyncio.run(main._queue_indicator_tables())  # 서버 시작을 막지 않는다


@pytest.mark.parametrize(("is_admin", "code"), [(True, 200), (False, 403)])
def test_admin_load_jobs(monkeypatch, is_admin, code) -> None:
    async def overview(limit):
        return {"counts": {"queued": 1}, "queued": [{"tbl_id": TBL}], "failed": []}

    monkeypatch.setattr(load_queue, "overview", overview)
    main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(is_admin=is_admin)
    try:
        r = TestClient(main.app).get("/admin/load-jobs")
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == code
    if is_admin:
        assert r.json()["counts"] == {"queued": 1}


def test_admin_load_jobs_requires_login() -> None:
    assert TestClient(main.app).get("/admin/load-jobs").status_code == 401
