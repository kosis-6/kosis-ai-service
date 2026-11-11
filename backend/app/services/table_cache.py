"""적재 안 된 통계표를 질문 처리 중에 바로 받아 넣는다 (캐시 모듈)

전체 2,364개 표를 미리 적재하지 않는다. 질문에 필요한 표가 DB에 없으면:

  작은 표   KOSIS 요청이 INLINE_MAX_REQUESTS번 이하 → 이 요청 안에서 받고(collect_table)
            검증·적재(load_table + 적재 후 단계)한 뒤 "loaded"를 돌려준다. 질문 처리는 다시 계산한다
  큰 표     그보다 많음 → 받지 않고 "too_big". 질문 처리가 적재 대기열(app.load_jobs)에 넣고
            적재 프로세스(pipeline/load_worker.py)가 뒤에서 받는다

남은 1,640개 표 중 약 91%가 작은 표다 (카탈로그 예상 크기 기준, 2026-11-11).
실측: 요청 1번 표 받기 + 적재 약 1초, 3~7번 약 2~5초 (요청 하나 0.2초 안팎, 간격은 LIMITER가 맞춤).

약속 (load_queue.py, load_worker.py와 함께 지킨다)
  - 적재 여부는 raw.stat_load로만 판단한다. 다 받고 검증을 통과해야 stat_value가 바뀐다
  - 같은 표를 여러 질문이 동시에 요청하면 한 번만 받는다 (표마다 잠금)
  - 적재 프로세스가 같은 표를 받는 중이면 받지 않고 "busy" (프로세스 사이 잠금,
    load_worker.LOCK_PREFIX). 같은 {tbl_id}.csv와 _work/{tbl_id}/를 두 곳에서 쓰지 않는다
  - 받다가 실패하거나 INLINE_TIMEOUT을 넘기면 "failed"·"timeout". 질문 처리는 대기열로 넘긴다.
    받던 조각은 TABLE_DIR/_work에 남아 적재 프로세스가 이어 받는다
  - KOSIS 호출은 분당 PER_MINUTE회까지 (적재 프로세스 100회와 합쳐 KOSIS 제한 200회 아래)

설정 (환경변수)
  INLINE_MAX_REQUESTS   이 요청 수 이하면 바로 받는다 (기본 8, 0이면 끄고 항상 대기열)
  INLINE_TIMEOUT        바로 받기를 기다리는 최대 초 (기본 20)
"""

import asyncio
import contextlib
import logging
import os
import time
from typing import Literal

logger = logging.getLogger("table_cache")

INLINE_MAX_REQUESTS = int(os.environ.get("INLINE_MAX_REQUESTS", "8"))
INLINE_TIMEOUT = float(os.environ.get("INLINE_TIMEOUT", "20"))
PER_MINUTE = 90  # 메타 요청까지 포함한 서비스 몫 (적재 프로세스 100회와 합쳐 200회 아래)
BURST = 12  # 쉬고 있었으면 이만큼은 바로 보낸다 (표 하나: 메타 3 + 데이터 최대 8 = 11)
PER_TABLE = 4  # 표 하나 안에서 동시에 보내는 요청 수

Outcome = Literal["loaded", "too_big", "failed", "timeout", "disabled", "busy"]


class TokenBucket:
    """분당 PER_MINUTE회를 넘지 않되, 쉬고 있었으면 BURST회까지는 기다리지 않고 보낸다

    collect_all.RateLimiter는 요청마다 간격을 벌려(90회면 0.67초) 요청 8번짜리 표가 5초 넘게
    걸린다. 질문 처리 중에는 한 번에 몰아 보내고, 질문이 몰릴 때만 기다리게 한다.
    """

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
    """서버 프로세스에 하나 (uvicorn은 이벤트 루프 하나로 돈다)"""
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(PER_MINUTE, BURST)
    return _limiter


async def fetch_now(tbl_id: str, org_id: str, trace: dict | None = None) -> Outcome:
    """표를 지금 받아 적재한다. 반환값으로 질문 처리가 다음 할 일을 정한다

    loaded    적재됨 (다른 질문이 먼저 넣은 경우 포함) → 다시 계산
    too_big   요청 수가 INLINE_MAX_REQUESTS를 넘음 → 대기열
    failed    KOSIS 오류·검증 실패 → 대기열 (적재 프로세스가 다시 시도하고 실패를 기록한다)
    timeout   INLINE_TIMEOUT 초과 → 대기열 (받은 조각부터 이어 받는다)
    disabled  INLINE_MAX_REQUESTS=0
    busy      적재 프로세스가 같은 표를 받는 중 → 대기열 (이미 들어 있는 작업의 요청 수만 오른다)
    """
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
    """적재 프로세스와 함께 쓰는 표 잠금을 기다리지 않고 잡아 본다. 잡았으면 True

    잠금은 연결에 붙으므로 끝날 때 풀고, 풀지 못하면 연결을 버린다 (풀에 잠금이 남지 않게)"""
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
    """_fetch의 오류를 결과값으로 바꾼다 (KOSIS 키가 오류 문구에 남지 않게 가린다)"""
    try:
        async with _db_lock(tbl_id) as got:
            if not got:
                logger.info("%s: 적재 프로세스가 받는 중이라 바로 받지 않음", tbl_id)
                return "busy"
            return await _fetch(tbl_id, org_id, trace, force)
    except TimeoutError:
        return "timeout"
    except (Exception, SystemExit) as e:  # load_raw는 검증 실패에 SystemExit을 쓴다
        from app.services.kosis_client import mask_key

        trace["fetch_error"] = mask_key(f"{type(e).__name__}: {e}")[:300]
        logger.warning("%s: 바로 받기 실패 → 대기열: %s", tbl_id, trace["fetch_error"])
        return "failed"


async def _fetch(tbl_id: str, org_id: str, trace: dict, force: bool = False) -> Outcome:
    """force: 이미 적재된 표도 다시 받는다 (KOSIS에 새 연도가 올라온 경우, refresh_if_stale)"""
    from app.services import load_queue, lookup
    from pipeline import load_worker

    # 잠금을 기다리는 동안 다른 질문이 넣었을 수 있다
    if not force and tbl_id in await load_queue.loaded([tbl_id]):
        return "loaded"
    table_dir = load_worker.TABLE_DIR
    # 시간 제한은 받는 단계에만 건다. 적재는 스레드에서 돌아 중간에 멈출 수 없으므로 끝까지 기다린다
    if not await asyncio.wait_for(_download(tbl_id, org_id, trace, table_dir), INLINE_TIMEOUT):
        return "too_big"
    # 적재(load_table)와 적재 후 단계(지역 매핑 등)는 적재 프로세스와 같은 함수를 쓴다
    await asyncio.to_thread(load_worker._load, table_dir / f"{tbl_id}.csv", tbl_id)
    lookup.clear_table_caches()  # 30초 주기 확인을 기다리지 않고 바로 새 표를 읽게 한다
    return "loaded"


async def _download(tbl_id: str, org_id: str, trace: dict, table_dir) -> bool:
    """KOSIS에서 받아 table_dir에 {tbl_id}.csv + .meta.json으로 둔다. 큰 표면 받지 않고 False"""
    from app.services.kosis_client import KosisClient
    from scripts.collect_all import collect_table, plan_table
    from scripts.collect_kosis import fetch_meta

    client = KosisClient(timeout=INLINE_TIMEOUT)
    limiter = _rate_limiter()
    for _ in range(3):  # 메타 요청 3번(TBL·ITM·PRD)도 분당 호출 수에 넣는다
        await limiter.wait()
    meta = await fetch_meta(client, org_id, tbl_id, pause=0)
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


# ---- 최신 갱신 확인 ("최근·최신" 질문) -----------------------------------------------------

REFRESH_EVERY = 24 * 3600.0  # 초. 표마다 KOSIS 수록 기간을 하루에 한 번만 확인한다
_checked: dict[str, float] = {}  # 표 → 마지막으로 확인한 시각 (time.monotonic)

Refresh = Literal["fresh", "skipped", "loaded", "too_big", "failed", "timeout", "disabled", "busy"]


async def refresh_if_stale(tbl_id: str, org_id: str, trace: dict | None = None) -> Refresh:
    """KOSIS에 DB보다 새 연도가 올라와 있으면 표를 다시 받아 교체한다

    fresh     DB가 최신 (KOSIS 마지막 연도 ≤ DB 마지막 연도)
    skipped   오늘 이미 확인함, 또는 아직 적재 안 된 표 (계산 중 바로 받기가 맡는다)
    loaded    새 연도까지 다시 받아 교체함
    too_big · failed · timeout · disabled
              교체 못 함 → 질문 처리가 갱신 작업(update)을 대기열에 넣고 지금 있는 자료로 답한다
    trace: refresh_db_year, refresh_kosis_year (+ fetch_* 칸)
    """
    trace = trace if trace is not None else {}
    if _checked_today(tbl_id):
        return "skipped"
    async with _lock(tbl_id):
        if _checked_today(tbl_id):  # 잠금을 기다리는 동안 다른 질문이 확인함
            return "skipped"
        outcome = await _check_and_refresh(tbl_id, org_id, trace)
        # KOSIS 확인 자체가 실패하면 다음 질문에서 다시 본다. 확인은 됐지만 바로 못 받은 경우는
        # 대기열의 갱신 작업이 맡으므로 그날은 다시 보지 않는다
        if "refresh_error" not in trace:
            _checked[tbl_id] = time.monotonic()
        return outcome


def _checked_today(tbl_id: str) -> bool:
    last = _checked.get(tbl_id)
    return last is not None and time.monotonic() - last < REFRESH_EVERY


async def _check_and_refresh(tbl_id: str, org_id: str, trace: dict) -> Refresh:
    """DB와 KOSIS의 마지막 연도를 비교해 KOSIS가 새로우면 다시 받는다 (refresh_if_stale)"""
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
    """적재된 표의 마지막 연 단위 시점 (월·분기 행은 빼고). 적재 안 됐으면 None"""
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
        # 표 키를 값으로 넘겨야 그 표의 파티션만 읽는다 (조인으로 거르면 4억 행 전체를 훑어 16초)
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
    """KOSIS 수록 기간 중 연 단위(년·다년) 주기의 마지막 연도. 연 단위 주기가 없으면 None"""
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
