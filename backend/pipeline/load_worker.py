"""통계표 적재 프로세스 (기본 틀): app.load_jobs 대기열의 표를 하나씩 raw.stat_value에 적재

표 2,364개를 미리 다 넣지 않고, 질문·지표 사전·수동 등록으로 대기열에 들어온 표만 넣는다.
표 하나를 처리하는 순서:

  1. 원본 찾기  TABLE_DIR의 {tbl_id}.csv(.gz) + {tbl_id}.meta.json
                (구글 드라이브에서 받아 둔 일부 표). 없으면 KOSIS에서 받는다
                (scripts/collect_all.collect_table. LOAD_SOURCE=file이면 받지 않음)
  2. 적재      pipeline/load_raw.load_table: 중복·행 수·합계 검증 후 표 단위 교체,
                raw.stat_load 기록
  3. 적재 후   AFTER_LOAD 단계
                지역 매핑 mart.region_code_map (pipeline/region_map.map_table): 이게 있어야
                지역 조건으로 조회된다. 지역 분류가 없는 표는 전국 값만 나온다
  4. 기록      done / 일시 오류면 run_after 뒤 다시 시도 / 그 밖의 오류는 failed + 오류 내용

약속 (app/services/load_queue.py와 함께 지킨다)
  - 적재 여부는 raw.stat_load로만 판단한다. 표 전체가 검증을 통과해야 stat_value가 바뀐다
  - 작업은 FOR UPDATE SKIP LOCKED로 꺼내 여러 일꾼이 같은 표를 두 번 처리하지 않는다
  - running으로 STALE_AFTER 넘게 멈춘 작업(프로세스가 죽은 경우)은 queued로 되돌린다.
    KOSIS에서 받던 조각은 _work/에 남아 있어 이어 받는다
  - KOSIS 호출은 분당 PER_MINUTE회까지만 쓴다 (KOSIS 제한 200회 중 실시간 조회 몫을 남김)

실행 (backend/ 에서)
  python -m pipeline.load_worker                       대기열을 계속 처리
  python -m pipeline.load_worker --once                작업 하나만 처리하고 끝
  python -m pipeline.load_worker enqueue DT_1B26001 …  표를 대기열에 넣기 (reason=manual)
  python -m pipeline.load_worker enqueue --from-dir    TABLE_DIR에 받아 둔 표 중 적재 안 된 표 전부
  docker compose --profile worker up -d worker         컨테이너로 띄우기
  python -m pipeline.load_worker --no-refresh          매일 0시 갱신 확인 없이
                                                       (pipeline/refresh_check.py)
"""

import argparse
import asyncio
import contextlib
import gzip
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg

from pipeline.load_raw import load_table
from pipeline.region_map import map_table
from pipeline.stats_build import conninfo

ROOT = Path(__file__).resolve().parent.parent
# 구글 드라이브에서 받은 표를 두는 곳. data/processed(서비스 기본 20개 표, run_all.py가 전처리)와
# 나눠 두어 전처리가 2,364개 표를 다 읽지 않게 한다
TABLE_DIR = ROOT / os.environ.get("KOSIS_TABLE_DIR", "data/tables")
# auto: 파일이 있으면 파일, 없으면 KOSIS / file: 파일만 (없으면 실패) / kosis: 항상 KOSIS에서 받기
LOAD_SOURCE = os.environ.get("LOAD_SOURCE", "auto")
PER_MINUTE = 100
CONCURRENCY = 2  # 동시에 처리하는 표 수 (큰 표 하나가 작은 표를 막지 않게)
IDLE_SECONDS = 10
STALE_AFTER = timedelta(hours=2)
MAX_ATTEMPTS = 3
RETRY_AFTER = [timedelta(minutes=10), timedelta(hours=1)]  # 1번째, 2번째 실패 뒤 기다리는 시간
# 표마다 거는 PostgreSQL 잠금 키 (pg_advisory_lock(hashtext(키))). 서버의 바로 받기
# (table_cache.py)도 같은 키를 써서, 같은 {tbl_id}.csv·_work/{tbl_id}/를 동시에 쓰지 않는다
LOCK_PREFIX = "table_load:"
# 다시 해 볼 만한 일시 오류. 검증 실패(행 수·합계·중복)나 데이터 형식 문제는 다시 해도 같다
TRANSIENT = (httpx.HTTPError, TimeoutError, ConnectionError, psycopg.OperationalError)

logger = logging.getLogger("load_worker")


@dataclass
class Job:
    id: int
    tbl_id: str
    org_id: str
    reason: str
    attempts: int


# 적재 후 단계: (이름, 함수(pg, tbl_id)). 표 하나 단위로 나눈 전처리를 여기에 넣는다
AFTER_LOAD: list[tuple[str, Callable[[psycopg.Connection, str], object]]] = [
    ("지역 매핑", map_table),
]


# ---- 대기열 ---------------------------------------------------------------------------------


def claim(pg: psycopg.Connection) -> Job | None:
    """먼저 처리할 작업 하나를 running으로 바꿔 가져온다. 없으면 None"""
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
    """다시 시도할 때까지 기다릴 시간. 다시 하지 않으면 None"""
    if not isinstance(error, TRANSIENT) or attempts >= MAX_ATTEMPTS:
        return None
    return RETRY_AFTER[min(attempts, len(RETRY_AFTER)) - 1]


def fail(pg: psycopg.Connection, job: Job, error: BaseException) -> str:
    """일시 오류면 queued로 되돌려 나중에 다시, 아니면 failed. 반환: 바뀐 상태"""
    from app.services.kosis_client import mask_key

    # 오류 내용은 관리자 API로 보이므로 KOSIS 키가 들어 있으면 가린다
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
    """오래 멈춘 running 작업을 queued로 되돌린다. 되돌린 수"""
    cur = pg.execute(
        "update app.load_jobs set status = 'queued', error = 'running 상태로 멈춰 다시 시도'"
        " where status = 'running' and started_at < now() - %s",
        [STALE_AFTER],
    )
    return cur.rowcount


def enqueue(pg: psycopg.Connection, tbl_ids: list[str]) -> list[str]:
    """수동 등록 (reason=manual). 적재된 표·메타데이터에 없는 표는 건너뛴다. 넣은 표 목록"""
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


# ---- 표 하나 처리 ----------------------------------------------------------------------------


def find_source(tbl_id: str, table_dir: Path = TABLE_DIR) -> Path | None:
    """받아 둔 원본 CSV. .csv.gz면 옆에 풀어 둔 .csv 경로를 돌려준다 (적재 뒤 지움). 없으면 None"""
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


async def collect(job: Job, table_dir: Path = TABLE_DIR) -> Path:
    """KOSIS에서 표 전체를 받아 table_dir에 {tbl_id}.csv + .meta.json으로 둔다"""
    from app.services.kosis_client import KosisClient
    from scripts import collect_all

    # 분당 호출 수를 이 프로세스 몫으로 낮춘다 (collect_all 기본값은 전체 수집용 150회)
    if collect_all.LIMITER.gap < 60.0 / PER_MINUTE:
        collect_all.LIMITER = collect_all.RateLimiter(PER_MINUTE)
    table_dir.mkdir(parents=True, exist_ok=True)
    client = KosisClient(timeout=180.0)
    await collect_all.collect_table(client, job.org_id, job.tbl_id, table_dir, per_table=2)
    return table_dir / f"{job.tbl_id}.csv"


async def process(job: Job, source: str = LOAD_SOURCE, table_dir: Path = TABLE_DIR) -> None:
    """원본 찾기(없으면 KOSIS) → 적재 → 적재 후 단계. 실패하면 예외를 그대로 올린다"""
    had_csv = (table_dir / f"{job.tbl_id}.csv").exists()
    # 갱신은 받아 둔 파일이 옛 자료라 항상 KOSIS에서 새로 받는다
    fresh = source == "kosis" or job.reason == "update"
    path = None if fresh else find_source(job.tbl_id, table_dir)
    unpacked = path is not None and not had_csv  # .gz를 풀어 만든 CSV
    if path is None:
        if source == "file":
            raise FileNotFoundError(f"{table_dir}에 {job.tbl_id}.csv와 .meta.json이 없습니다")
        logger.info("%s: 받아 둔 파일이 없어 KOSIS에서 받습니다", job.tbl_id)
        path = await collect(job, table_dir)
    try:
        await asyncio.to_thread(_load, path, job.tbl_id)
    finally:
        if unpacked:
            path.unlink(missing_ok=True)  # 풀어 둔 CSV만 지운다. 원본 .gz는 남긴다


def _load(path: Path, tbl_id: str) -> None:
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        load_table(pg, path)
        for name, step in AFTER_LOAD:
            logger.info("%s: 적재 후 단계 %s", tbl_id, name)
            step(pg, tbl_id)


# ---- 반복 실행 -------------------------------------------------------------------------------


def _with_pg(fn, *args):
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        return fn(pg, *args)


@contextlib.asynccontextmanager
async def table_lock(tbl_id: str):
    """표 하나를 받는 동안 잡는 잠금. 서버가 같은 표를 바로 받는 중이면 끝날 때까지 기다린다"""
    pg = await asyncio.to_thread(psycopg.connect, conninfo(), autocommit=True)
    try:
        await asyncio.to_thread(
            pg.execute, "select pg_advisory_lock(hashtext(%s))", [LOCK_PREFIX + tbl_id]
        )
        yield
    finally:
        await asyncio.to_thread(pg.close)  # 연결을 닫으면 잠금도 풀린다


def is_loaded(pg: psycopg.Connection, tbl_id: str) -> bool:
    return bool(
        pg.execute(
            "select 1 from catalog.stat_table t join raw.stat_load using (tbl_key)"
            " where t.tbl_id = %s",
            [tbl_id],
        ).fetchone()
    )


async def run_one() -> bool:
    """작업 하나 처리. 처리할 작업이 없었으면 False"""
    job = await asyncio.to_thread(_with_pg, claim)
    if job is None:
        return False
    logger.info("시작 #%d %s (%s, %d번째 시도)", job.id, job.tbl_id, job.reason, job.attempts)
    try:
        async with table_lock(job.tbl_id):
            # 기다리는 동안 서버가 바로 받아 넣었으면 다시 받지 않는다 (갱신 작업은 항상 받는다)
            if job.reason != "update" and await asyncio.to_thread(_with_pg, is_loaded, job.tbl_id):
                logger.info("%s: 그사이 적재됨, 건너뜀", job.tbl_id)
            else:
                await process(job)
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
    """대기열을 계속 처리한다. refresh면 매일 0시에 적재된 표의 KOSIS 갱신도 확인한다
    (pipeline/refresh_check.py: 새 시점이 있는 표를 갱신 작업으로 넣으면 이 일꾼들이 받는다)"""
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
    # httpx는 INFO에서 요청 주소를 통째로 남긴다. KOSIS 주소에 apiKey가 있어 로그에 키가 남는다
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
