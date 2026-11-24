from typing import Any, Literal

from sqlalchemy import text

from app.core.db import SessionLocal

Reason = Literal["question", "indicator", "update", "manual"]
PRIORITY: dict[str, int] = {"question": 0, "indicator": 1, "manual": 1, "update": 2}
KOSIS_URL = "https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


class UnknownTable(LookupError):
    pass


async def loaded(tbl_ids: list[str]) -> set[str]:
    if not tbl_ids:
        return set()
    async with SessionLocal() as s:
        rows = await s.execute(
            text(
                "select t.tbl_id from catalog.stat_table t join raw.stat_load l using (tbl_key)"
                " where t.tbl_id = any(:ids)"
            ),
            {"ids": list(tbl_ids)},
        )
        return set(rows.scalars())


async def table_info(tbl_id: str) -> dict[str, Any]:
    async with SessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "select org_id, organization, table_name from catalog.table_meta"
                    " where tbl_id = :t"
                ),
                {"t": tbl_id},
            )
        ).first()
    if row is None:
        raise UnknownTable(tbl_id)
    return {
        "tbl_id": tbl_id,
        "org_id": row.org_id,
        "name": row.table_name,
        "source": f"{row.organization}, {row.table_name} ({tbl_id})",
        "url": KOSIS_URL.format(org_id=row.org_id, tbl_id=tbl_id),
    }


async def enqueue(tbl_id: str, reason: Reason) -> dict[str, Any]:
    info = await table_info(tbl_id)
    async with SessionLocal() as s:
        row = (
            await s.execute(
                text(
                    """
                    insert into app.load_jobs (tbl_id, org_id, reason, priority)
                    values (:tbl_id, :org_id, :reason, :priority)
                    on conflict (tbl_id) where status in ('queued', 'running') do update
                    set requested_count = app.load_jobs.requested_count + 1,
                        last_requested_at = now(),
                        priority = least(app.load_jobs.priority, excluded.priority)
                    returning id, status, requested_count, (xmax = 0) as created
                    """
                ),
                {
                    "tbl_id": tbl_id,
                    "org_id": info["org_id"],
                    "reason": reason,
                    "priority": PRIORITY[reason],
                },
            )
        ).one()
        await s.commit()
    return dict(row._mapping)


async def enqueue_missing(tbl_ids: list[str], reason: Reason) -> list[str]:
    done = await loaded(tbl_ids)
    missing = sorted(set(tbl_ids) - done)
    for tbl_id in missing:
        await enqueue(tbl_id, reason)
    return missing


async def overview(limit: int = 20) -> dict[str, Any]:
    async with SessionLocal() as s:
        counts = await s.execute(
            text("select status, count(*) as n from app.load_jobs group by status")
        )
        queued = await s.execute(
            text(
                "select id, tbl_id, reason, status, priority, requested_count, requested_at,"
                " run_after, attempts, error"
                " from app.load_jobs where status in ('queued', 'running')"
                " order by status desc, priority, requested_at limit :n"
            ),
            {"n": limit},
        )
        failed = await s.execute(
            text(
                "select id, tbl_id, reason, attempts, finished_at, error from app.load_jobs"
                " where status = 'failed' order by finished_at desc nulls last limit :n"
            ),
            {"n": limit},
        )
        return {
            "counts": {r.status: r.n for r in counts},
            "queued": [dict(r._mapping) for r in queued],
            "failed": [dict(r._mapping) for r in failed],
        }
