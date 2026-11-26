import argparse
import asyncio
import contextlib
import gzip
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psycopg

from pipeline import population_mart
from pipeline.load_raw import load_table
from pipeline.region_map import map_table
from pipeline.stats_build import conninfo

ROOT = Path(__file__).resolve().parent.parent
TABLE_DIR = ROOT / os.environ.get("KOSIS_TABLE_DIR", "data/tables")
LOAD_SOURCE = os.environ.get("LOAD_SOURCE", "auto")
UPDATE_DIR = "_updates"
PER_MINUTE = 100
CONCURRENCY = 2
IDLE_SECONDS = 10
STALE_AFTER = timedelta(hours=2)
MAX_ATTEMPTS = 3
RETRY_AFTER = [timedelta(minutes=10), timedelta(hours=1)]
LOCK_PREFIX = "table_load:"
TRANSIENT = (httpx.HTTPError, TimeoutError, ConnectionError, psycopg.OperationalError)

logger = logging.getLogger("load_worker")


@dataclass
class Job:
    id: int
    tbl_id: str
    org_id: str
    reason: str
    attempts: int


AFTER_LOAD: list[tuple[str, Callable[[psycopg.Connection, str], object]]] = [
    ("지역 매핑", map_table),
    ("인구 집계 표", population_mart.rebuild),
]


def claim(pg: psycopg.Connection) -> Job | None:
    row = pg.execute(
        """
        update app.load_jobs set status = 'running', started_at = now(), attempts = attempts + 1
        where id = (
            select id from app.load_jobs
            where status = 'queued' and (run_after is null or run_after <= now())
            order by priority, requested_at
            for update skip locked
            limit 1
        )
        returning id, tbl_id, org_id, reason, attempts
        """
    ).fetchone()
    return Job(*row) if row else None


def finish(pg: psycopg.Connection, job: Job) -> None:
    pg.execute(
        "update app.load_jobs set status = 'done', finished_at = now(), error = null where id = %s",
        [job.id],
    )


def retry_delay(error: BaseException, attempts: int) -> timedelta | None:
    if not isinstance(error, TRANSIENT) or attempts >= MAX_ATTEMPTS:
        return None
    return RETRY_AFTER[min(attempts, len(RETRY_AFTER)) - 1]


def fail(pg: psycopg.Connection, job: Job, error: BaseException) -> str:
    from app.services.kosis_client import mask_key

    message = mask_key(f"{type(error).__name__}: {error}".replace("\n", " "))[:1000]
    delay = retry_delay(error, job.attempts)
    if delay is None:
        pg.execute(
            "update app.load_jobs set status = 'failed', finished_at = now(), error = %s"
            " where id = %s",
            [message, job.id],
        )
        return "failed"
    pg.execute(
        "update app.load_jobs set status = 'queued', run_after = now() + %s, error = %s"
        " where id = %s",
        [delay, message, job.id],
    )
    return "queued"


def recover_stale(pg: psycopg.Connection) -> int:
    cur = pg.execute(
        "update app.load_jobs set status = 'queued', error = 'running 상태로 멈춰 다시 시도'"
        " where status = 'running' and started_at < now() - %s",
        [STALE_AFTER],
    )
    return cur.rowcount


def enqueue(pg: psycopg.Connection, tbl_ids: list[str]) -> list[str]:
    loaded = {
        r[0]
        for r in pg.execute(
            "select t.tbl_id from catalog.stat_table t join raw.stat_load using (tbl_key)"
            " where t.tbl_id = any(%s)",
            [tbl_ids],
        )
    }
    known = dict(
        pg.execute(
            "select tbl_id, org_id from catalog.table_meta where tbl_id = any(%s)", [tbl_ids]
        ).fetchall()
    )
    queued = []
    for tbl_id in tbl_ids:
        if tbl_id in loaded:
            logger.info("%s: 이미 적재됨, 건너뜀", tbl_id)
        elif tbl_id not in known:
            logger.warning("%s: 메타데이터(catalog.table_meta)에 없는 표, 건너뜀", tbl_id)
        else:
            pg.execute(
                """
                insert into app.load_jobs (tbl_id, org_id, reason, priority)
                values (%s, %s, 'manual', 1)
                on conflict (tbl_id) where status in ('queued', 'running') do update
                set requested_count = app.load_jobs.requested_count + 1, last_requested_at = now()
                """,
                [tbl_id, known[tbl_id]],
            )
            queued.append(tbl_id)
    return queued


def find_source(tbl_id: str, table_dir: Path = TABLE_DIR) -> Path | None:
    meta = table_dir / f"{tbl_id}.meta.json"
    csv_path = table_dir / f"{tbl_id}.csv"
    gz_path = table_dir / f"{tbl_id}.csv.gz"
    if not meta.exists():
        return None
    if csv_path.exists():
        return csv_path
    if gz_path.exists():
        with gzip.open(gz_path, "rb") as src, csv_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return csv_path
    return None


class NothingToFetch(Exception):
    pass


async def collect(job: Job, table_dir: Path = TABLE_DIR, delta: bool = False) -> Path:
    from app.services.kosis_client import KosisClient
    from pipeline import refresh_check
    from scripts import collect_all
    from scripts.collect_kosis import fetch_meta

    if collect_all.LIMITER.gap < 60.0 / PER_MINUTE:
        collect_all.LIMITER = collect_all.RateLimiter(PER_MINUTE)
    table_dir.mkdir(parents=True, exist_ok=True)
    client = KosisClient(timeout=180.0)
    meta = None
    if delta:
        meta = await fetch_meta(client, job.org_id, job.tbl_id)
        lasts, when = await asyncio.to_thread(_with_pg, _db_state_of, job.tbl_id)
        revised = {}
        if when is not None:
            try:
                sent = await refresh_check.kosis_sent(client, job.org_id, job.tbl_id)
                revised = refresh_check.revised_periods(sent, lasts, when)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s: 값 수정 확인 실패 (%s)", job.tbl_id, type(e).__name__)
        periods = refresh_check.delta_periods(meta["periods"], lasts, revised)
        if not periods:
            raise NothingToFetch(job.tbl_id)
        text = ", ".join(f"{p['PRD_SE']} {p['STRT_PRD_DE']}~{p['END_PRD_DE']}" for p in periods)
        logger.info("%s: 가져올 부분만 받습니다 (%s)", job.tbl_id, text)
        meta = {**meta, "periods": periods}
    await collect_all.collect_table(
        client, job.org_id, job.tbl_id, table_dir, per_table=2, meta=meta
    )
    return table_dir / f"{job.tbl_id}.csv"


def _db_state_of(pg: psycopg.Connection, tbl_id: str) -> tuple[dict[str, int], datetime | None]:
    from pipeline import refresh_check

    row = pg.execute(
        "select tbl_key from catalog.stat_table where tbl_id = %s", [tbl_id]
    ).fetchone()
    if not row:
        return {}, None
    return refresh_check.db_lasts(pg, row[0]), refresh_check.loaded_at(pg, row[0])


async def process(job: Job, source: str = LOAD_SOURCE, table_dir: Path = TABLE_DIR) -> None:
    if job.reason == "update":
        await process_update(job, table_dir / UPDATE_DIR)
        return
    had_csv = (table_dir / f"{job.tbl_id}.csv").exists()
    fresh = source == "kosis"
    path = None if fresh else find_source(job.tbl_id, table_dir)
    unpacked = path is not None and not had_csv
    if path is None:
        if source == "file":
            raise FileNotFoundError(f"{table_dir}에 {job.tbl_id}.csv와 .meta.json이 없습니다")
        logger.info("%s: 받아 둔 파일이 없어 KOSIS에서 받습니다", job.tbl_id)
        path = await collect(job, table_dir)
    try:
        await asyncio.to_thread(_load, path, job.tbl_id)
    finally:
        if unpacked:
            path.unlink(missing_ok=True)


async def process_update(job: Job, delta_dir: Path) -> None:
    try:
        path = await collect(job, delta_dir, delta=True)
    except NothingToFetch:
        logger.info("%s: KOSIS에서 가져올 부분이 없습니다", job.tbl_id)
        return
    try:
        await asyncio.to_thread(_load, path, job.tbl_id, True)
    finally:
        for leftover in (path, path.with_suffix(".meta.json")):
            leftover.unlink(missing_ok=True)


def _load(path: Path, tbl_id: str, merge: bool = False) -> None:
    """원본 저장과 적재 후 단계(지역 매핑 · 인구 집계 표)를 한 트랜잭션으로. 어느 단계든 실패하면
    원본도 되돌린다. 원본 기록(raw.stat_load)이 있으면 '적재됨'으로 보므로(is_loaded ·
    load_queue.loaded) 둘이 따로 확정되면 매핑 없이 적재됨이 됐다: 재시도는 건너뛰어 done이
    됐고 앱은 그 표를 다시 적재하지 않았다 (11/26 리뷰). 안쪽 transaction()은 저장점이 된다"""
    with psycopg.connect(conninfo(), autocommit=True) as pg, pg.transaction():
        load_table(pg, path, merge=merge)
        for name, step in AFTER_LOAD:
            logger.info("%s: 적재 후 단계 %s", tbl_id, name)
            step(pg, tbl_id)


def _with_pg(fn, *args):
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        return fn(pg, *args)


@contextlib.asynccontextmanager
async def table_lock(tbl_id: str):
    pg = await asyncio.to_thread(psycopg.connect, conninfo(), autocommit=True)
    try:
        await asyncio.to_thread(
            pg.execute, "select pg_advisory_lock(hashtext(%s))", [LOCK_PREFIX + tbl_id]
        )
        yield
    finally:
        await asyncio.to_thread(pg.close)


def is_loaded(pg: psycopg.Connection, tbl_id: str) -> bool:
    return bool(
        pg.execute(
            "select 1 from catalog.stat_table t join raw.stat_load using (tbl_key)"
            " where t.tbl_id = %s",
            [tbl_id],
        ).fetchone()
    )


async def handle(job: Job) -> None:
    # 원본 저장과 적재 후 단계는 한 트랜잭션이라(_load) 원본이 있으면 매핑까지 끝난 표다
    if job.reason != "update" and await asyncio.to_thread(_with_pg, is_loaded, job.tbl_id):
        logger.info("%s: 그사이 적재됨, 건너뜀", job.tbl_id)
    else:
        await process(job)


async def run_one() -> bool:
    job = await asyncio.to_thread(_with_pg, claim)
    if job is None:
        return False
    logger.info("시작 #%d %s (%s, %d번째 시도)", job.id, job.tbl_id, job.reason, job.attempts)
    try:
        async with table_lock(job.tbl_id):
            await handle(job)
    except BaseException as e:  # noqa: BLE001 - load_raw는 분류 수 초과에 SystemExit을 쓴다
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        status = await asyncio.to_thread(_with_pg, fail, job, e)
        logger.error("실패 #%d %s → %s: %s: %s", job.id, job.tbl_id, status, type(e).__name__, e)
    else:
        await asyncio.to_thread(_with_pg, finish, job)
        logger.info("완료 #%d %s", job.id, job.tbl_id)
    return True


async def run_forever(concurrency: int = CONCURRENCY, refresh: bool = True) -> None:
    from pipeline import refresh_check

    recovered = await asyncio.to_thread(_with_pg, recover_stale)
    if recovered:
        logger.warning("멈춰 있던 작업 %d개를 대기로 되돌렸습니다", recovered)

    async def lane() -> None:
        while True:
            if not await run_one():
                await asyncio.sleep(IDLE_SECONDS)

    tasks = [lane() for _ in range(concurrency)]
    if refresh:
        tasks.append(refresh_check.run_nightly())
    await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="통계표 적재 대기열 처리")
    sub = parser.add_subparsers(dest="command")
    add = sub.add_parser("enqueue", help="표를 대기열에 넣기")
    add.add_argument("tables", nargs="*", help="표 ID")
    add.add_argument("--from-dir", action="store_true", help=f"{TABLE_DIR}에 받아 둔 표 전부")
    parser.add_argument("--once", action="store_true", help="작업 하나만 처리하고 끝")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="매일 0시 KOSIS 갱신 확인을 하지 않음 (REFRESH_NIGHTLY=0과 같음)",
    )
    args = parser.parse_args()

    if args.command == "enqueue":
        tables = list(args.tables)
        if args.from_dir:
            tables += sorted(
                p.name.removesuffix(".meta.json") for p in TABLE_DIR.glob("*.meta.json")
            )
        queued = _with_pg(enqueue, tables)
        print(f"대기열에 넣음 {len(queued)}개 / 요청 {len(tables)}개")
        return
    if args.once:
        if not asyncio.run(run_one()):
            print("처리할 작업이 없습니다")
        return
    refresh = not args.no_refresh and os.environ.get("REFRESH_NIGHTLY", "1") != "0"
    asyncio.run(run_forever(args.concurrency, refresh))


if __name__ == "__main__":
    main()
