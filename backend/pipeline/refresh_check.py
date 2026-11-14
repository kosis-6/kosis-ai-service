"""적재된 표의 KOSIS 갱신 확인: 매일 0시(한국 시간)에 새 시점이 올라온 표를 다시 받게 한다

적재된 표(raw.stat_load)마다 KOSIS 수록 기간(getMeta type=PRD, 요청 1번)을 DB의 마지막 시점과
비교하고, KOSIS가 새로우면 적재 대기열에 갱신 작업(reason=update)을 넣는다.
받기·적재는 적재 프로세스(load_worker)가 한다: 갱신 작업은 표 전체가 아니라 가져올 부분만
KOSIS에서 받는다 (delta_periods: 빠진 주기는 전체, 새 시점이 생긴 주기는 DB 마지막 시점 다음부터).
받은 부분은 기존 자료와 합쳐 적재한다 (load_raw: 새 자료에 없는 기존 칸은 그대로 남는다).
중간에 실패하면 옛 표가 그대로 남는다.

비교 방법
  DB에 있는 주기(raw.stat_value.prd_se: A 연, H 반년, Q 분기, M 월 …)마다 마지막 시점을 KOSIS의
  같은 주기 마지막 시점과 비교한다. KOSIS 표기는 DB 형식으로 바꿔 비교한다
  (연 "2025" → 2025, 월 "2026.09" → 202609, 분기 "2026 3/4" → 202603).
  KOSIS에는 있는데 DB에 없는 주기도 갱신 대상이다 (빠진 자료를 가져온다)
  예) DT_1B04006을 연간 파일로만 적재했으면 KOSIS 월별(2008.01~)도 가져온다
      분기 요청 형식 버그로 분기가 비어 있던 표도 이렇게 채워진다
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
# 갱신 작업을 넣은 표와 그때 KOSIS가 알린 마지막 시점. 다시 받았는데도 그 시점이 없으면
# (KOSIS 수록 기간에는 있지만 자료는 아직 없음, 예: DT_1B85032 2018년) 수록 기간이 또 바뀔 때까지
# 다시 넣지 않는다. 안 그러면 매일 밤 같은 표를 통째로 다시 받는다
TRIED_PATH = ROOT / "data" / "refresh" / "tried.json"
RETRY_AFTER = 600.0  # 초. 확인이 통째로 실패하면 이만큼 뒤에 다시
TICK = 60.0  # 초. 0시가 지났는지 이 간격으로 실제 시각을 본다 (잠자기 등으로 시계가 어긋나도 맞게)
KST = ZoneInfo("Asia/Seoul")
RUN_AT = time(0, 0)  # 매일 이 시각 (한국 시간)
PER_MINUTE = 50
PRIORITY_UPDATE = 2  # app/services/load_queue.PRIORITY["update"]와 같다 (질문으로 들어온 표가 먼저)


# ---- 비교 (DB·KOSIS 없이 동작) ----------------------------------------------------------------


# KOSIS 수록주기 이름 → DB 주기 코드 (KOSIS 자료 응답의 PRD_SE와 같다)
PERIOD_CODE = {"년": "A", "반년": "H", "분기": "Q", "월": "M", "일": "D", "부정기": "IR"}
MULTI_YEAR = re.compile(r"^\d+년$")  # "2년", "5년" → 다년(F)
COMPACT_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})?")
KOSIS_DATE = re.compile(r"(\d{4})(?:\D+(\d{1,2}))?(?:\D+(\d{1,2}))?")


def period_code(label: str) -> str | None:
    label = str(label).strip()
    if label in PERIOD_CODE:
        return PERIOD_CODE[label]
    return "F" if MULTI_YEAR.match(label) else None


def db_period(code: str, end: str) -> int | None:
    """KOSIS 수록 시점 표기 → DB period 형식. 모르는 형식이면 None"""
    text = str(end).strip()
    if compact := COMPACT_DATE.fullmatch(text):  # "202609", "20261031" (구분자 없이 붙여 씀)
        text = ".".join(part for part in compact.groups() if part)
    m = KOSIS_DATE.match(text)
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


def needs_update(
    periods: list[dict], db_lasts: dict[str, int]
) -> dict[str, tuple[int | None, int]]:
    """가져와야 할 주기 {주기 코드: (DB 마지막 시점, KOSIS 마지막 시점)}. 비어 있으면 최신.
    KOSIS가 더 새로운 주기와, KOSIS에는 있는데 DB에 없는 주기(DB 칸이 None)"""
    if not db_lasts:  # 적재 기록만 있고 자료가 없는 표는 보지 않는다 (적재 쪽 문제)
        return {}
    out = {}
    for code, last in kosis_lasts(periods).items():
        db = _db_last(db_lasts, code)
        if db is None or last > db:
            out[code] = (db, last)
    return out


# 연 단위 주기끼리는 같은 칸으로 본다. 메타데이터에 "2년"·"5년"(다년, F)으로 나오는 표도 자료는
# 연(A)으로 와서 DB에 A로 들어 있다 (예: DT_182001_A002_2025). 따로 보면 다년 표 361개가 모두
# "주기 없음"으로 잘못 잡힌다
YEARLY = ("A", "F")


def _db_last(db_lasts: dict[str, int], code: str) -> int | None:
    if code in YEARLY:
        years = [db_lasts[c] for c in YEARLY if db_lasts.get(c) is not None]
        return max(years) if years else None
    return db_lasts.get(code)


def next_period(code: str, last: int) -> str:
    """DB 마지막 시점 다음 시점 (요청 형식). 월 202612 → 202701, 분기 202504 → 202601"""
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
    """KOSIS 수록 기간(메타데이터 PRD) 중 가져올 부분만 남긴다 (적재 프로세스가 갱신 작업에 씀)
    DB에 없는 주기      수록 기간 전체
    KOSIS가 고친 시점    그 시점부터 (revised_periods. 새 시점이 있으면 그것까지 함께)
    KOSIS가 새로운 주기  DB 마지막 시점 다음부터 (시작을 요청 형식으로 바꿔 둔다)
    DB와 같은 주기       뺀다"""
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


# ---- 값만 고친 시점 -------------------------------------------------------------------------
# KOSIS는 새 시점 없이 값만 고치기도 한다 (잠정치 → 확정치 등). 메타데이터 NCD가 시점마다
# 자료를 보낸 날(SEND_DE)을 주는데, 새로 낼 때 표 전체를 다시 보내 1970년 자료까지 같은 날로
# 찍히기도 한다 (DT_1B26001: 모든 달이 2026-10-15). 그래서 최근 REVISE_LAST개 시점만 보고,
# 그중 우리가 적재한 날 뒤에 보낸 시점부터 다시 받는다. 바뀐 값은 덧붙일 때 이력에 남는다
REVISE_LAST = 3


def revised_periods(
    sent: list[dict], db_lasts: dict[str, int], loaded_at: datetime, last_n: int = REVISE_LAST
) -> dict[str, int]:
    """DB에 있는 최근 시점 중 적재한 날 뒤에 KOSIS가 다시 보낸 시점 → {주기 코드: 다시 받을 첫 시점}
    sent: 메타데이터 NCD 행 (PRD_SE, PRD_DE, SEND_DE).
    DB에 아직 없는 새 시점은 needs_update가 본다"""
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
    """연 단위 주기(A·F)는 같은 칸으로 본다 (_db_last와 같은 규칙)"""
    keys = YEARLY if code in YEARLY else (code,)
    found = [revised[k] for k in keys if k in revised]
    return min(found) if found else None


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


async def kosis_sent(client, org_id: str, tbl_id: str) -> list[dict]:
    """시점마다 KOSIS가 자료를 보낸 날 (메타데이터 NCD: PRD_SE, PRD_DE, SEND_DE)"""
    from scripts.collect_kosis import call

    return await call(
        client,
        "/statisticsData.do",
        {"method": "getMeta", "orgId": org_id, "tblId": tbl_id, "type": "NCD"},
        pause=0,
    )


def loaded_at(pg: psycopg.Connection, tbl_key: int) -> datetime | None:
    """그 표를 마지막으로 적재한 시각 (덧붙이기도 고친다)"""
    row = pg.execute("select loaded_at from raw.stat_load where tbl_key = %s", [tbl_key])
    found = row.fetchone()
    return found[0] if found else None


def last_update_done(pg: psycopg.Connection, tbl_id: str) -> datetime | None:
    """그 표의 갱신 작업이 마지막으로 끝난 시각"""
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
    """같은 KOSIS 시점으로 갱신 작업을 넣었고, 그 뒤 다시 받기가 끝났는데도 DB가 그대로인가"""
    if not tried or tried.get("kosis") != kosis or done_at is None:
        return False
    return done_at >= datetime.fromisoformat(tried["queued_at"])


async def _check_revised(client, limiter, org_id, tbl_id, tbl_key, lasts) -> dict[str, int]:
    """새 시점이 없는 표: 최근 시점을 KOSIS가 적재 뒤에 다시 보냈는가. 확인하지 못하면 빈 dict
    (값 수정 확인은 덤이라 실패해도 그 표를 실패로 세지 않는다)"""
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
    """적재된 표를 모두 확인하고 새 시점이 있는 표를 갱신 대기열에 넣는다 → 결과 요약"""
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
            tried.pop(tbl_id, None)  # 다 받았다
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
            if kosis:  # 새 시점을 받으러 넣은 표만 (값을 고친 표는 적재 시각으로 다시 막힌다)
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
        "waiting": waiting,  # KOSIS 수록 기간에만 있고 자료는 아직 없는 표
        "dry_run": dry_run,
    }
    if not dry_run and not only:  # 일부 표만 본 수동 실행은 그날 확인으로 치지 않는다
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
    """적재 프로세스 안에서 계속 돈다: 오늘 확인을 놓쳤으면 바로, 그 뒤로는 매일 RUN_AT에 확인"""
    if missed_today():
        logger.info("오늘 갱신 확인을 아직 안 해서 지금 확인합니다")
        await _safe_check()
    while True:
        wait = seconds_until_next_run()
        logger.info("다음 갱신 확인: %.1f시간 뒤 (매일 %s)", wait / 3600, RUN_AT.strftime("%H:%M"))
        # 남은 시간을 한 번에 자지 않고 TICK마다 실제 시각을 본다 (컨테이너가 멈췄다 깨어나도 맞게)
        while not missed_today():
            await asyncio.sleep(min(TICK, max(1.0, seconds_until_next_run())))
        await _safe_check()
        if missed_today():  # 확인이 통째로 실패함 (DB 연결 등) → 바로 되풀이하지 않고 잠시 뒤
            await asyncio.sleep(RETRY_AFTER)


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
        text = ", ".join(f"{c} {v['db'] or '없음'}→{v['kosis']}" for c, v in u["periods"].items())
        print(f"{u['tbl_id']}: {text}")
    print(
        f"확인 {summary['checked']}개, 갱신 {len(summary['updated'])}개, "
        f"실패 {len(summary['failed'])}개"
    )


if __name__ == "__main__":
    main()
