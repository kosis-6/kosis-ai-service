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
  - 받다가 실패하거나 INLINE_TIMEOUT을 넘기면 "failed"·"timeout". 질문 처리는 대기열로 넘긴다.
    받던 조각은 TABLE_DIR/_work에 남아 적재 프로세스가 이어 받는다
  - KOSIS 호출은 분당 PER_MINUTE회까지 (적재 프로세스 100회와 합쳐 KOSIS 제한 200회 아래)

설정 (환경변수)
  INLINE_MAX_REQUESTS   이 요청 수 이하면 바로 받는다 (기본 8, 0이면 끄고 항상 대기열)
  INLINE_TIMEOUT        바로 받기를 기다리는 최대 초 (기본 20)
"""

import asyncio
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

Outcome = Literal["loaded", "too_big", "failed", "timeout", "disabled"]


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
    """
    trace = trace if trace is not None else {}
    if INLINE_MAX_REQUESTS <= 0:
        return "disabled"
    start = time.perf_counter()
    async with _lock(tbl_id):
        try:
            outcome = await _fetch(tbl_id, org_id, trace)
        except TimeoutError:
            outcome = "timeout"
        except (Exception, SystemExit) as e:  # load_raw는 검증 실패에 SystemExit을 쓴다
            from app.services.kosis_client import mask_key

            trace["fetch_error"] = mask_key(f"{type(e).__name__}: {e}")[:300]
            logger.warning("%s: 바로 받기 실패 → 대기열: %s", tbl_id, trace["fetch_error"])
            outcome = "failed"
    trace["fetch_ms"] = round((time.perf_counter() - start) * 1000)
    trace["fetch"] = outcome
    return outcome


async def _fetch(tbl_id: str, org_id: str, trace: dict) -> Outcome:
    from app.services import load_queue, lookup
    from pipeline import load_worker

    # 잠금을 기다리는 동안 다른 질문이 넣었을 수 있다
    if tbl_id in await load_queue.loaded([tbl_id]):
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
