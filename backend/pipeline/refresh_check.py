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
TRIED_PATH = ROOT / "data" / "refresh" / "tried.json"
RETRY_AFTER = 600.0
TICK = 60.0
KST = ZoneInfo("Asia/Seoul")
RUN_AT = time(0, 0)
PER_MINUTE = 50
PRIORITY_UPDATE = 2


PERIOD_CODE = {"년": "A", "반년": "H", "분기": "Q", "월": "M", "일": "D", "부정기": "IR"}
MULTI_YEAR = re.compile(r"^\d+년$")
COMPACT_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})?")
KOSIS_DATE = re.compile(r"(\d{4})(?:\D+(\d{1,2}))?(?:\D+(\d{1,2}))?")


def period_code(label: str) -> str | None:
    label = str(label).strip()
    if label in PERIOD_CODE:
        return PERIOD_CODE[label]
    return "F" if MULTI_YEAR.match(label) else None


def db_period(code: str, end: str) -> int | None:
    text = str(end).strip()
    if compact := COMPACT_DATE.fullmatch(text):
        text = ".".join(part for part in compact.groups() if part)
    m = KOSIS_DATE.match(text)
    if not m:
        return None
    year, a, b = int(m[1]), m[2], m[3]
    if code in ("M", "Q", "H") and a:
        return year * 100 + int(a)
    if code == "D" and a and b:
        return year * 10000 + int(a) * 100 + int(b)
    return year


def kosis_lasts(periods: list[dict]) -> dict[str, int]:
    out = {}
    for p in periods:
        code = period_code(p.get("PRD_SE", ""))
        if code and (end := db_period(code, p.get("END_PRD_DE", ""))) is not None:
            out[code] = max(end, out.get(code, end))
    return out


def needs_update(
    periods: list[dict], db_lasts: dict[str, int]
) -> dict[str, tuple[int | None, int]]:
    if not db_lasts:
        return {}
    out = {}
    for code, last in kosis_lasts(periods).items():
        db = _db_last(db_lasts, code)
        if db is None or last > db:
            out[code] = (db, last)
    return out


YEARLY = ("A", "F")


def _db_last(db_lasts: dict[str, int], code: str) -> int | None:
    if code in YEARLY:
        years = [db_lasts[c] for c in YEARLY if db_lasts.get(c) is not None]
        return max(years) if years else None
    return db_lasts.get(code)


def next_period(code: str, last: int) -> str:
    per_year = {"M": 12, "Q": 4, "H": 2}.get(code)
    if per_year:
        year, sub = divmod(last, 100)
        return f"{year + 1}01" if sub >= per_year else f"{year}{sub + 1:02d}"
    if code == "D":
        day = date(last // 10000, last // 100 % 100, last % 100) + timedelta(days=1)
        return day.strftime("%Y%m%d")
    return str(last + 1)


def delta_periods(
    periods: list[dict], db_lasts: dict[str, int], revised: dict[str, int] | None = None
) -> list[dict]:
    out = []
    for p in periods:
        code = period_code(p.get("PRD_SE", ""))
        end = db_period(code, p.get("END_PRD_DE", "")) if code else None
        if end is None:
            continue
        db = _db_last(db_lasts, code)
        fixed = _revised_from(revised or {}, code)
        if db is None:
            out.append(p)
        elif fixed is not None:
            out.append({**p, "STRT_PRD_DE": str(fixed)})
        elif end > db:
            out.append({**p, "STRT_PRD_DE": next_period(code, db)})
    return out


REVISE_LAST = 3


def revised_periods(
    sent: list[dict], db_lasts: dict[str, int], loaded_at: datetime, last_n: int = REVISE_LAST
) -> dict[str, int]:
    loaded = loaded_at.astimezone(KST).date()
    by_code: dict[str, list[tuple[int, date]]] = {}
    for r in sent:
        code = period_code(r.get("PRD_SE", ""))
        period = db_period(code, r.get("PRD_DE", "")) if code else None
        db = _db_last(db_lasts, code) if code else None
        if period is None or db is None or period > db:
            continue
        try:
            sent_on = date.fromisoformat(str(r.get("SEND_DE", ""))[:10])
        except ValueError:
            continue
        by_code.setdefault(code, []).append((period, sent_on))
    out = {}
    for code, rows in by_code.items():
        changed = [period for period, on in sorted(rows)[-last_n:] if on > loaded]
        if changed:
            out[code] = min(changed)
    return out


def _revised_from(revised: dict[str, int], code: str) -> int | None:
    keys = YEARLY if code in YEARLY else (code,)
    found = [revised[k] for k in keys if k in revised]
    return min(found) if found else None


def seconds_until_next_run(now: datetime | None = None) -> float:
    now = (now or datetime.now(KST)).astimezone(KST)
    at = datetime.combine(now.date(), RUN_AT, tzinfo=KST)
    if at <= now:
        at += timedelta(days=1)
    return (at - now).total_seconds()


def missed_today(now: datetime | None = None, state_path: Path = STATE_PATH) -> bool:
    today = (now or datetime.now(KST)).astimezone(KST).date()
    try:
        last = date.fromisoformat(json.loads(state_path.read_text(encoding="utf-8"))["date"])
    except (OSError, ValueError, KeyError):
        return True
    return last < today


def loaded_tables(pg: psycopg.Connection, only: list[str] | None = None) -> list[tuple]:
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
    rows = pg.execute(
        "select prd_se, max(period) from raw.stat_value where tbl_key = %s group by prd_se",
        [tbl_key],
    ).fetchall()
    return {code: last for code, last in rows}


def enqueue_updates(pg: psycopg.Connection, tables: list[tuple[str, str]]) -> None:
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


async def _kosis_periods(client, org_id: str, tbl_id: str) -> list[dict]:
    from scripts.collect_kosis import call

    return await call(
        client,
        "/statisticsData.do",
        {"method": "getMeta", "orgId": org_id, "tblId": tbl_id, "type": "PRD"},
        pause=0,
    )


async def kosis_sent(client, org_id: str, tbl_id: str) -> list[dict]:
    from scripts.collect_kosis import call

    return await call(
        client,
        "/statisticsData.do",
        {"method": "getMeta", "orgId": org_id, "tblId": tbl_id, "type": "NCD"},
        pause=0,
    )


def loaded_at(pg: psycopg.Connection, tbl_key: int) -> datetime | None:
    row = pg.execute("select loaded_at from raw.stat_load where tbl_key = %s", [tbl_key])
    found = row.fetchone()
    return found[0] if found else None


def last_update_done(pg: psycopg.Connection, tbl_id: str) -> datetime | None:
    return pg.execute(
        "select max(finished_at) from app.load_jobs"
        " where tbl_id = %s and reason = 'update' and status = 'done'",
        [tbl_id],
    ).fetchone()[0]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def already_tried(tried: dict | None, kosis: dict[str, int], done_at: datetime | None) -> bool:
    if not tried or tried.get("kosis") != kosis or done_at is None:
        return False
    return done_at >= datetime.fromisoformat(tried["queued_at"])


async def _check_revised(client, limiter, org_id, tbl_id, tbl_key, lasts) -> dict[str, int]:
    from app.services.kosis_client import mask_key

    when = await asyncio.to_thread(_with_pg, loaded_at, tbl_key)
    if when is None or not lasts:
        return {}
    await limiter.wait()
    try:
        sent = await kosis_sent(client, org_id, tbl_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "%s: 값 수정 확인 실패 %s", tbl_id, mask_key(f"{type(e).__name__}: {e}")[:200]
        )
        return {}
    return revised_periods(sent, lasts, when)


async def check_all(
    only: list[str] | None = None,
    dry_run: bool = False,
    state_path: Path = STATE_PATH,
    tried_path: Path = TRIED_PATH,
) -> dict:
    from app.services.kosis_client import KosisClient, mask_key
    from scripts.collect_all import RateLimiter

    tables = await asyncio.to_thread(_with_pg, loaded_tables, only)
    client = KosisClient(timeout=30.0)
    limiter = RateLimiter(PER_MINUTE)
    started = datetime.now(KST)
    updated, failed, waiting = [], [], []
    tried = _read_json(tried_path)
    logger.info("갱신 확인 시작: 적재된 표 %d개", len(tables))
    for i, (tbl_id, org_id, tbl_key) in enumerate(tables, start=1):
        await limiter.wait()
        try:
            periods = await _kosis_periods(client, org_id, tbl_id)
            lasts = await asyncio.to_thread(_with_pg, db_lasts, tbl_key)
        except Exception as e:  # noqa: BLE001 - 표 하나가 실패해도 나머지는 확인한다
            failed.append({"tbl_id": tbl_id, "error": mask_key(f"{type(e).__name__}: {e}")[:200]})
            continue
        newer = needs_update(periods, lasts)
        if not newer:
            tried.pop(tbl_id, None)
            revised = await _check_revised(client, limiter, org_id, tbl_id, tbl_key, lasts)
            if revised:
                changes = {
                    code: {"db": lasts.get(code), "revised_from": p} for code, p in revised.items()
                }
                updated.append({"tbl_id": tbl_id, "org_id": org_id, "periods": changes})
                text = ", ".join(f"{code} {p}부터" for code, p in revised.items())
                logger.info("%s: KOSIS가 값을 다시 보낸 시점 %s", tbl_id, text)
        elif already_tried(
            tried.get(tbl_id),
            {code: k for code, (_, k) in newer.items()},
            await asyncio.to_thread(_with_pg, last_update_done, tbl_id),
        ):
            waiting.append(tbl_id)
            logger.info(
                "%s: KOSIS 수록 기간에는 새 시점이 있지만 받은 자료에는 없어 기다립니다", tbl_id
            )
        else:
            changes = {code: {"db": db, "kosis": k} for code, (db, k) in newer.items()}
            updated.append({"tbl_id": tbl_id, "org_id": org_id, "periods": changes})
            text = ", ".join(f"{code} {db or '없음'}→{k}" for code, (db, k) in newer.items())
            logger.info("%s: 가져올 자료 %s", tbl_id, text)
        if i % 100 == 0:
            logger.info("갱신 확인 %d/%d", i, len(tables))

    if updated and not dry_run:
        pairs = [(u["tbl_id"], u["org_id"]) for u in updated]
        await asyncio.to_thread(_with_pg, enqueue_updates, pairs)
        for u in updated:
            kosis = {code: v["kosis"] for code, v in u["periods"].items() if "kosis" in v}
            if kosis:
                tried[u["tbl_id"]] = {"kosis": kosis, "queued_at": datetime.now(KST).isoformat()}
    if not dry_run:
        tried_path.parent.mkdir(parents=True, exist_ok=True)
        tried_path.write_text(json.dumps(tried, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "date": started.date().isoformat(),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(KST).isoformat(timespec="seconds"),
        "checked": len(tables) - len(failed),
        "failed": failed,
        "updated": updated,
        "waiting": waiting,
        "dry_run": dry_run,
    }
    if not dry_run and not only:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "갱신 확인 끝: 확인 %d개, 갱신 대기열 %d개%s, 자료 기다림 %d개, 실패 %d개",
        summary["checked"],
        len(updated),
        " (넣지 않음, --dry-run)" if dry_run else "",
        len(waiting),
        len(failed),
    )
    return summary


async def run_nightly() -> None:
    if missed_today():
        logger.info("오늘 갱신 확인을 아직 안 해서 지금 확인합니다")
        await _safe_check()
    while True:
        wait = seconds_until_next_run()
        logger.info("다음 갱신 확인: %.1f시간 뒤 (매일 %s)", wait / 3600, RUN_AT.strftime("%H:%M"))
        while not missed_today():
            await asyncio.sleep(min(TICK, max(1.0, seconds_until_next_run())))
        await _safe_check()
        if missed_today():
            await asyncio.sleep(RETRY_AFTER)


async def _safe_check() -> None:
    try:
        await check_all()
    except Exception:  # noqa: BLE001 - 확인이 실패해도 적재 프로세스는 계속 돈다
        logger.exception("갱신 확인 실패")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="적재된 표의 KOSIS 갱신 확인")
    parser.add_argument("--dry-run", action="store_true", help="확인만 하고 대기열에 넣지 않음")
    parser.add_argument("--tables", nargs="*", help="이 표만 확인")
    args = parser.parse_args()
    summary = asyncio.run(check_all(args.tables or None, args.dry_run))
    for u in summary["updated"]:
        text = ", ".join(f"{c} {v['db'] or '없음'}→{v['kosis']}" for c, v in u["periods"].items())
        print(f"{u['tbl_id']}: {text}")
    print(
        f"확인 {summary['checked']}개, 갱신 {len(summary['updated'])}개, "
        f"실패 {len(summary['failed'])}개"
    )


if __name__ == "__main__":
    main()
