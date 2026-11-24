import asyncio
import contextlib
import logging
import os
import time
from typing import Literal

from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger("table_cache")

INLINE_MAX_REQUESTS = int(os.environ.get("INLINE_MAX_REQUESTS", "8"))
INLINE_TIMEOUT = float(os.environ.get("INLINE_TIMEOUT", "20"))
PARTIAL_FETCH = os.environ.get("PARTIAL_FETCH", "1") != "0"
PER_MINUTE = 90
BURST = 12
PER_TABLE = 4

Outcome = Literal["loaded", "too_big", "failed", "timeout", "disabled", "busy"]


class TokenBucket:
    def __init__(self, per_minute: float, burst: int) -> None:
        self.rate = per_minute / 60.0
        self.burst = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            self.tokens -= 1
            delay = -self.tokens / self.rate if self.tokens < 0 else 0.0
        if delay:
            await asyncio.sleep(delay)


_locks: dict[str, asyncio.Lock] = {}
_limiter: TokenBucket | None = None


def _lock(tbl_id: str) -> asyncio.Lock:
    return _locks.setdefault(tbl_id, asyncio.Lock())


def _rate_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(PER_MINUTE, BURST)
    return _limiter


async def fetch_now(tbl_id: str, org_id: str, trace: dict | None = None) -> Outcome:
    trace = trace if trace is not None else {}
    if INLINE_MAX_REQUESTS <= 0:
        return "disabled"
    start = time.perf_counter()
    async with _lock(tbl_id):
        outcome = await _guarded_fetch(tbl_id, org_id, trace, force=False)
    trace["fetch_ms"] = round((time.perf_counter() - start) * 1000)
    trace["fetch"] = outcome
    return outcome


@contextlib.asynccontextmanager
async def _db_lock(tbl_id: str):
    from sqlalchemy import text

    from app.core.db import engine
    from pipeline.load_worker import LOCK_PREFIX

    key = {"k": LOCK_PREFIX + tbl_id}
    async with engine.connect() as conn:
        got = await conn.scalar(text("select pg_try_advisory_lock(hashtext(:k))"), key)
        try:
            yield bool(got)
        finally:
            if got:
                try:
                    await conn.scalar(text("select pg_advisory_unlock(hashtext(:k))"), key)
                    await conn.commit()
                except Exception:  # noqa: BLE001
                    await conn.invalidate()


async def _guarded_fetch(tbl_id: str, org_id: str, trace: dict, force: bool) -> Outcome:
    try:
        async with _db_lock(tbl_id) as got:
            if not got:
                logger.info("%s: 적재 프로세스가 받는 중이라 바로 받지 않음", tbl_id)
                return "busy"
            return await _fetch(tbl_id, org_id, trace, force)
    except TimeoutError:
        return "timeout"
    except (Exception, SystemExit) as e:
        from app.services.kosis_client import mask_key

        trace["fetch_error"] = mask_key(f"{type(e).__name__}: {e}")[:300]
        logger.warning("%s: 바로 받기 실패 → 대기열: %s", tbl_id, trace["fetch_error"])
        return "failed"


async def _fetch(tbl_id: str, org_id: str, trace: dict, force: bool = False) -> Outcome:
    from app.services import load_queue, lookup
    from pipeline import load_worker

    if not force and tbl_id in await load_queue.loaded([tbl_id]):
        return "loaded"
    table_dir = load_worker.TABLE_DIR / load_worker.UPDATE_DIR if force else load_worker.TABLE_DIR
    got = await asyncio.wait_for(
        _download(tbl_id, org_id, trace, table_dir, delta=force), INLINE_TIMEOUT
    )
    if got == "nothing":
        return "loaded"
    if not got:
        return "too_big"
    path = table_dir / f"{tbl_id}.csv"
    try:
        merge = (True,) if force else ()
        await asyncio.to_thread(load_worker._load, path, tbl_id, *merge)
    finally:
        if force:
            for leftover in (path, path.with_suffix(".meta.json")):
                leftover.unlink(missing_ok=True)
    lookup.clear_table_caches()
    return "loaded"


async def _download(tbl_id: str, org_id: str, trace: dict, table_dir, delta: bool = False):
    from app.services.kosis_client import KosisClient
    from pipeline import load_worker, refresh_check
    from scripts.collect_all import collect_table, plan_table
    from scripts.collect_kosis import fetch_meta

    client = KosisClient(timeout=INLINE_TIMEOUT)
    limiter = _rate_limiter()
    for _ in range(3):
        await limiter.wait()
    meta = await fetch_meta(client, org_id, tbl_id, pause=0)
    _METAS.set(tbl_id, meta)
    if delta:
        lasts, _ = await asyncio.to_thread(load_worker._with_pg, load_worker._db_state_of, tbl_id)
        periods = refresh_check.delta_periods(meta["periods"], lasts)
        if not periods:
            return "nothing"
        meta = {**meta, "periods": periods}
        trace["fetch_delta"] = [
            f"{p['PRD_SE']} {p['STRT_PRD_DE']}~{p['END_PRD_DE']}" for p in periods
        ]
    n_requests = sum(len(j["requests"]) for j in plan_table(meta))
    trace["fetch_requests"] = n_requests
    if n_requests > INLINE_MAX_REQUESTS:
        return False
    table_dir.mkdir(parents=True, exist_ok=True)
    await collect_table(
        client, org_id, tbl_id, table_dir, per_table=PER_TABLE,
        meta=meta, limiter=limiter, pause=0,
    )  # fmt: skip
    return True


_METAS = MemoryCache("table_metas", maxsize=64, ttl=600)


async def table_meta(tbl_id: str, org_id: str) -> dict:
    from app.services.kosis_client import KosisClient
    from scripts.collect_kosis import fetch_meta

    meta = _METAS.get(tbl_id)
    if meta is not MISSING:
        return meta
    limiter = _rate_limiter()
    for _ in range(3):
        await limiter.wait()
    meta = await asyncio.wait_for(
        fetch_meta(KosisClient(timeout=INLINE_TIMEOUT), org_id, tbl_id, pause=0), INLINE_TIMEOUT
    )
    _METAS.set(tbl_id, meta)
    return meta


async def fetch_cells(table, fetch) -> int:
    from app.services import partial_table
    from app.services.kosis_client import KosisClient
    from scripts.collect_all import DATA_PATH
    from scripts.collect_kosis import call

    client = KosisClient(timeout=INLINE_TIMEOUT)
    limiter = _rate_limiter()

    async def one(params: dict) -> list:
        await limiter.wait()
        query = {"method": "getList", "orgId": table.org_id, "tblId": table.tbl_id, **params}
        return await call(client, DATA_PATH, query, pause=0)

    got = await asyncio.wait_for(asyncio.gather(*(one(p) for p in fetch.requests)), INLINE_TIMEOUT)
    for params, records in zip(fetch.requests, got, strict=True):
        partial_table.add_records(table, params, records)
    partial_table.done(table, fetch)
    return len(fetch.requests)


REFRESH_EVERY = 24 * 3600.0
_checked: dict[str, float] = {}

Refresh = Literal["fresh", "skipped", "loaded", "too_big", "failed", "timeout", "disabled", "busy"]


async def refresh_if_stale(tbl_id: str, org_id: str, trace: dict | None = None) -> Refresh:
    trace = trace if trace is not None else {}
    if _checked_today(tbl_id):
        return "skipped"
    async with _lock(tbl_id):
        if _checked_today(tbl_id):
            return "skipped"
        outcome = await _check_and_refresh(tbl_id, org_id, trace)
        if "refresh_error" not in trace:
            _checked[tbl_id] = time.monotonic()
        return outcome


def _checked_today(tbl_id: str) -> bool:
    last = _checked.get(tbl_id)
    return last is not None and time.monotonic() - last < REFRESH_EVERY


async def _check_and_refresh(tbl_id: str, org_id: str, trace: dict) -> Refresh:
    try:
        db_year = await _db_last_year(tbl_id)
        if db_year is None:
            return "skipped"
        kosis_year = await _kosis_last_year(tbl_id, org_id)
    except Exception as e:  # noqa: BLE001 - 확인 실패가 답변을 막지 않게
        from app.services.kosis_client import mask_key

        trace["refresh_error"] = mask_key(f"{type(e).__name__}: {e}")[:300]
        logger.warning("%s: 최신 연도 확인 실패: %s", tbl_id, trace["refresh_error"])
        return "failed"
    trace["refresh_db_year"], trace["refresh_kosis_year"] = db_year, kosis_year
    if kosis_year is None or kosis_year <= db_year:
        return "fresh"
    if INLINE_MAX_REQUESTS <= 0:
        return "disabled"
    logger.info("%s: KOSIS에 %d년 자료가 있어 다시 받습니다 (DB %d년)", tbl_id, kosis_year, db_year)
    return await _guarded_fetch(tbl_id, org_id, trace, force=True)


async def _db_last_year(tbl_id: str) -> int | None:
    from sqlalchemy import text

    from app.core.db import SessionLocal

    async with SessionLocal() as s:
        key = (
            await s.execute(
                text(
                    "select t.tbl_key from catalog.stat_table t"
                    " join raw.stat_load l using (tbl_key) where t.tbl_id = :t"
                ),
                {"t": tbl_id},
            )
        ).scalar()
        if key is None:
            return None
        return (
            await s.execute(
                text(
                    "select max(period) from raw.stat_value"
                    " where tbl_key = :k and prd_se in ('A', 'F')"
                ),
                {"k": key},
            )
        ).scalar()


async def _kosis_last_year(tbl_id: str, org_id: str) -> int | None:
    from app.services.kosis_client import KosisClient
    from scripts.collect_all import period_code
    from scripts.collect_kosis import call

    await _rate_limiter().wait()
    periods = await call(
        KosisClient(timeout=INLINE_TIMEOUT),
        "/statisticsData.do",
        {"method": "getMeta", "orgId": org_id, "tblId": tbl_id, "type": "PRD"},
        pause=0,
    )
    years = [
        int(str(p["END_PRD_DE"])[:4]) for p in periods if period_code(p["PRD_SE"]) in ("Y", "F")
    ]
    return max(years) if years else None
