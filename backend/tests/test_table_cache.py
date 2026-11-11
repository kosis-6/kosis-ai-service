"""적재 안 된 표를 질문 처리 중에 바로 받아 넣기 (app/services/table_cache.py, qa._compute_fetching)
(DB·KOSIS 없이 받기·적재 함수를 바꿔 끼운다)
"""

import asyncio

import pytest

from app.core.stats_db import StatsTableMissing
from app.services import load_queue, lookup, qa, table_cache
from pipeline import load_worker
from scripts import collect_all, collect_kosis

TBL = "DT_1SA6006"
INFO = {
    "tbl_id": TBL,
    "org_id": "101",
    "name": "시험 표",
    "source": f"국가데이터처, 시험 표 ({TBL})",
    "url": f"https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId={TBL}",
}
OK = {"status": "ok", "value": 1}


# ---- table_cache.fetch_now -------------------------------------------------------------------


@pytest.fixture
def kosis(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict:
    """받기·적재를 바꿔 끼운다. state["requests"]로 요청 수, state["fail"]로 받기 오류를 정한다"""
    state = {"requests": 1, "fail": None, "slow": 0.0, "loaded": set(), "downloads": 0,
             "loads": [], "cleared": 0}  # fmt: skip

    async def loaded(ids):
        return {t for t in ids if t in state["loaded"]}

    async def fetch_meta(client, org_id, tbl_id, pause=1.0):
        assert pause == 0  # 질문 처리 중에는 요청마다 쉬지 않는다
        return {"tbl_nm": "시험 표", "items": [], "classes": [], "periods": []}

    def plan_table(meta):
        return [{"requests": [{}] * state["requests"]}]

    async def collect_table(client, org_id, tbl_id, out, **kw):
        assert kw["meta"]["tbl_nm"] == "시험 표" and kw["pause"] == 0 and kw["limiter"]
        state["downloads"] += 1
        await asyncio.sleep(state["slow"])
        if state["fail"]:
            raise state["fail"]
        return {"requests": state["requests"], "rows": 3, "sent": state["requests"]}

    def load(path, tbl_id):
        state["loads"].append((path.name, tbl_id))
        state["loaded"].add(tbl_id)

    def clear():
        state["cleared"] += 1

    monkeypatch.setattr(load_queue, "loaded", loaded)
    monkeypatch.setattr(collect_kosis, "fetch_meta", fetch_meta)
    monkeypatch.setattr(collect_all, "plan_table", plan_table)
    monkeypatch.setattr(collect_all, "collect_table", collect_table)
    monkeypatch.setattr(load_worker, "_load", load)
    monkeypatch.setattr(load_worker, "TABLE_DIR", tmp_path)
    monkeypatch.setattr(lookup, "clear_table_caches", clear)
    monkeypatch.setattr(table_cache, "INLINE_MAX_REQUESTS", 8)
    monkeypatch.setattr(table_cache, "INLINE_TIMEOUT", 5.0)
    monkeypatch.setattr(table_cache, "_locks", {})
    monkeypatch.setattr(table_cache, "_limiter", None)  # asyncio.run마다 새 이벤트 루프
    return state


def test_token_bucket_sends_burst_then_waits(monkeypatch) -> None:
    """쉬고 있었으면 burst회는 바로, 그 뒤로는 분당 횟수에 맞춰 기다린다"""
    slept = []

    async def sleep(seconds):
        slept.append(round(seconds, 2))

    monkeypatch.setattr(table_cache.asyncio, "sleep", sleep)
    now = [100.0]
    monkeypatch.setattr(table_cache.time, "monotonic", lambda: now[0])

    async def run():
        bucket = table_cache.TokenBucket(per_minute=60, burst=3)  # 1초에 1회
        for _ in range(5):
            await bucket.wait()
        now[0] += 10  # 10초 쉬면 다시 burst만큼 바로
        for _ in range(3):
            await bucket.wait()

    asyncio.run(run())
    assert slept == [1.0, 2.0]  # 4번째는 1초, 5번째는 2초 뒤. 쉰 뒤 3번은 바로


def test_small_table_is_fetched_and_loaded(kosis) -> None:
    trace = {}
    assert asyncio.run(table_cache.fetch_now(TBL, "101", trace)) == "loaded"
    assert kosis["loads"] == [(f"{TBL}.csv", TBL)]
    assert kosis["cleared"] == 1  # 새 표를 바로 읽도록 표 정보 캐시를 비운다
    assert trace["fetch"] == "loaded" and trace["fetch_requests"] == 1
    assert isinstance(trace["fetch_ms"], int)


def test_request_limit_is_inclusive(kosis) -> None:
    kosis["requests"] = 8
    assert asyncio.run(table_cache.fetch_now(TBL, "101")) == "loaded"


def test_big_table_is_not_fetched(kosis) -> None:
    kosis["requests"] = 9
    trace = {}
    assert asyncio.run(table_cache.fetch_now(TBL, "101", trace)) == "too_big"
    assert kosis["downloads"] == 0 and kosis["loads"] == []
    assert trace["fetch_requests"] == 9


def test_already_loaded_table_is_not_fetched_again(kosis) -> None:
    kosis["loaded"].add(TBL)
    assert asyncio.run(table_cache.fetch_now(TBL, "101")) == "loaded"
    assert kosis["downloads"] == 0


def test_disabled(kosis, monkeypatch) -> None:
    monkeypatch.setattr(table_cache, "INLINE_MAX_REQUESTS", 0)
    assert asyncio.run(table_cache.fetch_now(TBL, "101")) == "disabled"
    assert kosis["downloads"] == 0


def test_failure_hides_api_key(kosis) -> None:
    kosis["fail"] = collect_kosis.KosisError("20: 인증 오류 (https://kosis.kr/x?apiKey=SECRET&a=1)")
    trace = {}
    assert asyncio.run(table_cache.fetch_now(TBL, "101", trace)) == "failed"
    assert "SECRET" not in trace["fetch_error"] and "apiKey=***" in trace["fetch_error"]
    assert kosis["loads"] == []


def test_validation_failure_is_failed(kosis, monkeypatch) -> None:
    def load(path, tbl_id):
        raise SystemExit(f"{tbl_id}: 행 수가 다릅니다")  # load_raw의 검증 실패

    monkeypatch.setattr(load_worker, "_load", load)
    assert asyncio.run(table_cache.fetch_now(TBL, "101")) == "failed"


def test_slow_download_times_out(kosis, monkeypatch) -> None:
    monkeypatch.setattr(table_cache, "INLINE_TIMEOUT", 0.05)
    kosis["slow"] = 1.0
    assert asyncio.run(table_cache.fetch_now(TBL, "101")) == "timeout"
    assert kosis["loads"] == []


def test_same_table_is_fetched_once_for_concurrent_questions(kosis) -> None:
    kosis["slow"] = 0.05

    async def both():
        return await asyncio.gather(
            table_cache.fetch_now(TBL, "101"), table_cache.fetch_now(TBL, "101")
        )

    assert asyncio.run(both()) == ["loaded", "loaded"]
    assert kosis["downloads"] == 1


# ---- qa: 계산 → 바로 받기 → 다시 계산 --------------------------------------------------------


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> dict:
    """compute가 state["missing"]에 남은 표마다 StatsTableMissing을 낸다"""
    state = {"missing": [TBL], "outcome": "loaded", "fetch_calls": [], "queued": []}

    async def understand(question, trace=None):
        return {"type": "value", "indicator": "시험 지표", "regions": ["서울"]}

    def compute(parsed):
        if state["missing"]:
            raise StatsTableMissing("적재 안 됨", state["missing"][0])
        return OK

    async def table_info(tbl_id):
        if not tbl_id.startswith("DT_"):
            raise load_queue.UnknownTable(tbl_id)
        return {**INFO, "tbl_id": tbl_id}

    async def fetch_now(tbl_id, org_id, trace=None):
        state["fetch_calls"].append(tbl_id)
        trace["fetch"] = state["outcome"]
        if state["outcome"] == "loaded" and tbl_id in state["missing"]:
            state["missing"].remove(tbl_id)
        return state["outcome"]

    async def enqueue(tbl_id, reason):
        state["queued"].append((tbl_id, reason))
        return {"id": 7, "status": "queued", "requested_count": 1, "created": True}

    monkeypatch.setattr(qa, "understand", understand)
    monkeypatch.setattr(qa, "compute", compute)
    # 답변 문장은 이 흐름과 상관없다 (가짜 계산 결과 OK로는 만들 수 없음)
    real_summary = qa.summary
    monkeypatch.setattr(qa, "summary", lambda r: "요약" if r is OK else real_summary(r))
    monkeypatch.setattr(qa, "render", lambda r: "")
    monkeypatch.setattr(load_queue, "table_info", table_info)
    monkeypatch.setattr(load_queue, "enqueue", enqueue)
    monkeypatch.setattr(table_cache, "fetch_now", fetch_now)
    return state


def test_missing_small_table_is_answered_in_the_same_request(flow) -> None:
    res = asyncio.run(qa.ask("서울 시험 지표"))
    assert res["status"] == "ok"
    assert flow["fetch_calls"] == [TBL] and flow["queued"] == []
    assert res["trace"]["fetched"] == [{"tbl_id": TBL, "fetch": "loaded"}]


def test_two_missing_tables_are_both_fetched(flow) -> None:
    flow["missing"] = [TBL, "DT_OTHER"]
    res = asyncio.run(qa.ask("서울 시험 지표"))
    assert res["status"] == "ok"
    assert flow["fetch_calls"] == [TBL, "DT_OTHER"]


@pytest.mark.parametrize("outcome", ["too_big", "failed", "timeout", "disabled"])
def test_not_fetched_table_goes_to_queue(flow, outcome) -> None:
    flow["outcome"] = outcome
    res = asyncio.run(qa.ask("서울 시험 지표"))
    assert res["status"] == "data_loading"
    assert flow["queued"] == [(TBL, "question")]
    assert res["trace"]["fetched"] == [{"tbl_id": TBL, "fetch": outcome}]


def test_table_still_missing_after_load_is_not_fetched_again(flow, monkeypatch) -> None:
    def compute(parsed):
        raise StatsTableMissing("적재 안 됨", TBL)  # 넣었는데도 계속 없다고 나옴

    monkeypatch.setattr(qa, "compute", compute)
    res = asyncio.run(qa.ask("서울 시험 지표"))
    assert flow["fetch_calls"] == [TBL]
    assert res["status"] == "data_loading"


def test_unknown_table_still_raises(flow) -> None:
    flow["missing"] = ["NOPE"]
    with pytest.raises(StatsTableMissing):
        asyncio.run(qa.ask("서울 시험 지표"))
    assert flow["fetch_calls"] == []
