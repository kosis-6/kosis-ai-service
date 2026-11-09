"""통계표 적재 대기열 (app.load_jobs): 질문 처리 쪽에서 쓰는 부분

전체 2,364개 표를 미리 적재하지 않는다. 적재 안 된 표가 질문이나 지표 사전에서 나오면 여기서
대기열에 넣고, 적재 프로세스가 표 전체를 받아 검증한 뒤 적재한다 (collect_table → load_table).

  - 적재 여부는 raw.stat_load로만 판단한다. stat_value에 일부 칸만 있는 상태를 만들지 않는다
  - 같은 표의 대기·진행 중 작업은 하나뿐이다. 다시 요청되면 requested_count만 올린다
  - 대기열에 넣기만 하고 꺼내 처리하지 않는다 (꺼내 적재하는 쪽: pipeline/load_worker.py)
"""

from typing import Any, Literal

from sqlalchemy import text

from app.core.db import SessionLocal

Reason = Literal["question", "indicator", "update", "manual"]
PRIORITY: dict[str, int] = {"question": 0, "indicator": 1, "manual": 1, "update": 2}
KOSIS_URL = "https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"


class UnknownTable(LookupError):
    """메타데이터(catalog.table_meta)에 없는 표. 수집 대상 2,364개 밖이라 적재할 수 없다"""


async def loaded(tbl_ids: list[str]) -> set[str]:
    """이 중 표 전체 적재가 끝난 표 (raw.stat_load에 기록된 표)"""
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
    """표 이름·기관·KOSIS 주소 (메타데이터 2,364개에서). 없으면 UnknownTable"""
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
    """적재 작업을 넣는다. 이미 대기·진행 중이면 요청 수만 올리고 우선순위는 높은 쪽으로 둔다

    반환: id, status, requested_count, created(이번에 새로 만들었는지)
    """
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
    """적재 안 된 표만 넣는다. 넣은(또는 이미 대기 중인) 표 목록"""
    done = await loaded(tbl_ids)
    missing = sorted(set(tbl_ids) - done)
    for tbl_id in missing:
        await enqueue(tbl_id, reason)
    return missing


async def overview(limit: int = 20) -> dict[str, Any]:
    """관리자 화면용: 상태별 개수, 대기 중인 표(먼저 처리할 순서), 최근 실패"""
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
