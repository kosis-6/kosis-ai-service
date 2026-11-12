"""적재된 표의 KOSIS 갱신 확인: 매일 0시(한국 시간)에 새 시점이 올라온 표를 다시 받게 한다

적재된 표(raw.stat_load)마다 KOSIS 수록 기간(getMeta type=PRD, 요청 1번)을 DB의 마지막 시점과
비교하고, KOSIS가 새로우면 적재 대기열에 갱신 작업(reason=update)을 넣는다.
받기·교체는 적재 프로세스(load_worker)가 한다: 갱신 작업은 받아 둔 파일 대신 항상 KOSIS에서
새로 받고, 검증을 통과해야 표 단위로 바뀐다 (중간에 실패하면 옛 표가 그대로 남는다).

비교 방법
  DB에 있는 주기(raw.stat_value.prd_se: A 연, H 반년, Q 분기, M 월 …)마다 마지막 시점을 KOSIS의
  같은 주기 마지막 시점과 비교한다. KOSIS 표기는 DB 형식으로 바꿔 비교한다
  (연 "2025" → 2025, 월 "2026.09" → 202609, 분기 "2026 3/4" → 202603).
  수집하지 않은 주기는 보지 않는다
  예) DT_1B04006은 연간만 적재 → KOSIS 월별 2026.10이 있어도 연간 2025가 같으면 갱신 아님
  값만 고친 정정(새 시점 없음)은 수록 기간에 드러나지 않아 잡지 못한다.

KOSIS 호출은 분당 PER_MINUTE회 (적재 프로세스·질문 처리 몫을 남긴다). 표 750개면 15분쯤 걸린다.
마지막 실행 결과는 STATE_PATH에 남긴다. 적재 프로세스가 0시에 꺼져 있었으면
다음에 켜질 때 그날 몫을 돌린다.

실행 (backend/ 에서)
  python -m pipeline.refresh_check              지금 확인하고 갱신 작업 넣기
  python -m pipeline.refresh_check --dry-run    확인만 (대기열에 넣지 않음)
  python -m pipeline.refresh_check --tables DT_1B04006 DT_1B8000F
  적재 프로세스(python -m pipeline.load_worker)는 매일 0시에 자동으로 돌린다
"""

import argparse
import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from pipeline.stats_build import conninfo

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "refresh" / "last_run.json"
KST = ZoneInfo("Asia/Seoul")
RUN_AT = time(0, 0)  # 매일 이 시각 (한국 시간)
PER_MINUTE = 50
PRIORITY_UPDATE = 2  # app/services/load_queue.PRIORITY["update"]와 같다 (질문으로 들어온 표가 먼저)


# ---- 비교 (DB·KOSIS 없이 동작) ----------------------------------------------------------------


# KOSIS 수록주기 이름 → DB 주기 코드 (KOSIS 자료 응답의 PRD_SE와 같다)
PERIOD_CODE = {"년": "A", "반년": "H", "분기": "Q", "월": "M", "일": "D", "부정기": "IR"}
MULTI_YEAR = re.compile(r"^\d+년$")  # "2년", "5년" → 다년(F)
KOSIS_DATE = re.compile(r"(\d{4})(?:\D+(\d{1,2}))?(?:\D+(\d{1,2}))?")


def period_code(label: str) -> str | None:
    label = str(label).strip()
    if label in PERIOD_CODE:
        return PERIOD_CODE[label]
    return "F" if MULTI_YEAR.match(label) else None


def db_period(code: str, end: str) -> int | None:
    """KOSIS 수록 시점 표기 → DB period 형식. 모르는 형식이면 None"""
    m = KOSIS_DATE.match(str(end).strip())
    if not m:
        return None
    year, a, b = int(m[1]), m[2], m[3]
    if code in ("M", "Q", "H") and a:  # "2026.09", "2026 3/4", "2026 1/2"
        return year * 100 + int(a)
    if code == "D" and a and b:
        return year * 10000 + int(a) * 100 + int(b)
    return year


def kosis_lasts(periods: list[dict]) -> dict[str, int]:
    """KOSIS 수록 기간 → {주기 코드: 마지막 시점(DB 형식)}"""
    out = {}
    for p in periods:
        code = period_code(p.get("PRD_SE", ""))
        if code and (end := db_period(code, p.get("END_PRD_DE", ""))) is not None:
            out[code] = max(end, out.get(code, end))
    return out


def needs_update(periods: list[dict], db_lasts: dict[str, int]) -> dict[str, tuple[int, int]]:
    """DB에 있는 주기 중 KOSIS가 더 새로운 것 {주기 코드: (DB, KOSIS)}. 비어 있으면 최신"""
    kosis = kosis_lasts(periods)
    return {
        code: (db, kosis[code])
        for code, db in db_lasts.items()
        if db is not None and code in kosis and kosis[code] > db
    }


def seconds_until_next_run(now: datetime | None = None) -> float:
    """다음 RUN_AT(한국 시간)까지 남은 초"""
    now = (now or datetime.now(KST)).astimezone(KST)
    at = datetime.combine(now.date(), RUN_AT, tzinfo=KST)
    if at <= now:
        at += timedelta(days=1)
    return (at - now).total_seconds()


def missed_today(now: datetime | None = None, state_path: Path = STATE_PATH) -> bool:
    """오늘(한국 시간) 확인을 아직 안 했는가 (0시에 적재 프로세스가 꺼져 있던 경우)"""
    today = (now or datetime.now(KST)).astimezone(KST).date()
    try:
        last = date.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["date"])
    except (OSError, ValueError, KeyError):
        return True
    return last < today


# ---- DB ------------------------------------------------------------------------------------


def loaded_tables(pg: psycopg.Connection, only: list[str] | None = None) -> list[tuple]:
    """적재된 표: (tbl_id, org_id, tbl_key)"""
    sql = """
        select t.tbl_id, m.org_id, t.tbl_key
        from raw.stat_load l
        join catalog.stat_table t using (tbl_key)
        join catalog.table_meta m on m.tbl_id = t.tbl_id
    """
    if only:
        return pg.execute(sql + " where t.tbl_id = any(%s) order by 1", [only]).fetchall()
    return pg.execute(sql + " order by 1").fetchall()


def db_lasts(pg: psycopg.Connection, tbl_key: int) -> dict[str, int]:
    """적재된 표의 주기별 마지막 시점 {주기 코드: period}.
    표 키를 값으로 넘겨 그 표의 파티션만 읽는다 (큰 표도 1초 안팎)"""
    rows = pg.execute(
        "select prd_se, max(period) from raw.stat_value where tbl_key = %s group by prd_se",
        [tbl_key],
    ).fetchall()
    return {code: last for code, last in rows}


def enqueue_updates(pg: psycopg.Connection, tables: list[tuple[str, str]]) -> None:
    """갱신 작업 넣기. 같은 표가 이미 대기·진행 중이면 요청 수만 올린다
    (load_queue.enqueue와 같은 규칙)"""
    for tbl_id, org_id in tables:
        pg.execute(
            """
            insert into app.load_jobs (tbl_id, org_id, reason, priority)
            values (%s, %s, 'update', %s)
            on conflict (tbl_id) where status in ('queued', 'running') do update
            set requested_count = app.load_jobs.requested_count + 1, last_requested_at = now()
            """,
            [tbl_id, org_id, PRIORITY_UPDATE],
        )


def _with_pg(fn, *args):
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        return fn(pg, *args)


# ---- 확인 ----------------------------------------------------------------------------------


async def _kosis_periods(client, org_id: str, tbl_id: str) -> list[dict]:
    from scripts.collect_kosis import call

    return await call(
        client,
        "/statisticsData.do",
        {"method": "getMeta", "orgId": org_id, "tblId": tbl_id, "type": "PRD"},
        pause=0,
    )


async def check_all(
    only: list[str] | None = None, dry_run: bool = False, state_path: Path = STATE_PATH
) -> dict:
    """적재된 표를 모두 확인하고 새 시점이 있는 표를 갱신 대기열에 넣는다 → 결과 요약"""
    from app.services.kosis_client import KosisClient, mask_key
    from scripts.collect_all import RateLimiter

    tables = await asyncio.to_thread(_with_pg, loaded_tables, only)
    client = KosisClient(timeout=30.0)
    limiter = RateLimiter(PER_MINUTE)
    started = datetime.now(KST)
    updated, failed = [], []
    logger.info("갱신 확인 시작: 적재된 표 %d개", len(tables))
    for i, (tbl_id, org_id, tbl_key) in enumerate(tables, start=1):
        await limiter.wait()
        try:
            periods = await _kosis_periods(client, org_id, tbl_id)
            lasts = await asyncio.to_thread(_with_pg, db_lasts, tbl_key)
        except Exception as e:  # noqa: BLE001 - 표 하나가 실패해도 나머지는 확인한다
            failed.append({"tbl_id": tbl_id, "error": mask_key(f"{type(e).__name__}: {e}")[:200]})
            continue
        if newer := needs_update(periods, lasts):
            changes = {code: {"db": db, "kosis": k} for code, (db, k) in newer.items()}
            updated.append({"tbl_id": tbl_id, "org_id": org_id, "periods": changes})
            text = ", ".join(f"{code} {db}→{k}" for code, (db, k) in newer.items())
            logger.info("%s: 새 시점 %s", tbl_id, text)
        if i % 100 == 0:
            logger.info("갱신 확인 %d/%d", i, len(tables))

    if updated and not dry_run:
        pairs = [(u["tbl_id"], u["org_id"]) for u in updated]
        await asyncio.to_thread(_with_pg, enqueue_updates, pairs)
    summary = {
        "date": started.date().isoformat(),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(KST).isoformat(timespec="seconds"),
        "checked": len(tables) - len(failed),
        "failed": failed,
        "updated": updated,
        "dry_run": dry_run,
    }
    if not dry_run and not only:  # 일부 표만 본 수동 실행은 그날 확인으로 치지 않는다
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "갱신 확인 끝: 확인 %d개, 갱신 대기열 %d개%s, 실패 %d개",
        summary["checked"],
        len(updated),
        " (넣지 않음, --dry-run)" if dry_run else "",
        len(failed),
    )
    return summary


async def run_nightly() -> None:
    """적재 프로세스 안에서 계속 돈다: 오늘 확인을 놓쳤으면 바로, 그 뒤로는 매일 RUN_AT에 확인"""
    if missed_today():
        logger.info("오늘 갱신 확인을 아직 안 해서 지금 확인합니다")
        await _safe_check()
    while True:
        wait = seconds_until_next_run()
        logger.info("다음 갱신 확인: %.1f시간 뒤 (매일 %s)", wait / 3600, RUN_AT.strftime("%H:%M"))
        await asyncio.sleep(wait)
        await _safe_check()


async def _safe_check() -> None:
    try:
        await check_all()
    except Exception:  # noqa: BLE001 - 확인이 실패해도 적재 프로세스는 계속 돈다
        logger.exception("갱신 확인 실패")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 주소(apiKey 포함)를 남기지 않게
    parser = argparse.ArgumentParser(description="적재된 표의 KOSIS 갱신 확인")
    parser.add_argument("--dry-run", action="store_true", help="확인만 하고 대기열에 넣지 않음")
    parser.add_argument("--tables", nargs="*", help="이 표만 확인")
    args = parser.parse_args()
    summary = asyncio.run(check_all(args.tables or None, args.dry_run))
    for u in summary["updated"]:
        text = ", ".join(f"{c} {v['db']}→{v['kosis']}" for c, v in u["periods"].items())
        print(f"{u['tbl_id']}: {text}")
    print(
        f"확인 {summary['checked']}개, 갱신 {len(summary['updated'])}개, "
        f"실패 {len(summary['failed'])}개"
    )


if __name__ == "__main__":
    main()
