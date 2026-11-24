from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.log_redaction import mask as mask
from app.services.log_redaction import redact

TZ = "Asia/Seoul"


def _masked(row: dict[str, Any]) -> dict[str, Any]:
    return redact(row)


def like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def conditions(
    column: dict[str, str],
    user_id: int | None = None,
    guest: bool | None = None,
    session_id: str | None = None,
    status: str | None = None,
    request_type: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    has_error: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}

    def add(sql: str, **values: Any) -> None:
        parts.append(sql)
        params.update(values)

    if user_id is not None:
        add(f"{column['user_id']} = :user_id", user_id=user_id)
    if guest is not None:
        add(f"{column['user_id']} is {'null' if guest else 'not null'}")
    if session_id:
        add(f"{column['session_id']} = :session_id", session_id=session_id)
    if status:
        add(f"{column['status']} = :status", status=status)
    if request_type and "request_type" in column:
        add(f"{column['request_type']} = :request_type", request_type=request_type)
    if q and q.strip():
        add(f"{column['question']} ilike :q escape '\\'", q=like(q.strip()))
    if since is not None:
        add(f"{column['created_at']} >= :since", since=since)
    if until is not None:
        add(f"{column['created_at']} < :until", until=until)
    if has_error is not None and "error" in column:
        add(f"{column['error']} is {'not null' if has_error else 'null'}")
    return (" where " + " and ".join(parts) if parts else ""), params


TURN_COLUMNS = {
    "user_id": "t.user_id",
    "session_id": "t.session_id",
    "status": "t.status",
    "request_type": "t.request_type",
    "question": "t.message",
    "created_at": "t.created_at",
}
SEARCH_COLUMNS = {
    "user_id": "s.user_id",
    "session_id": "s.session_id",
    "status": "s.status",
    "question": "s.question",
    "created_at": "s.created_at",
    "error": "s.error",
}
TURN_SELECT = (
    "select t.id, t.created_at, t.session_id, t.turn_no, t.user_id, u.email as user_email,"
    " u.nickname as user_nickname, t.message, t.request_type, t.status, t.tbl_id, t.recomputed,"
    " t.answer"
)
TURN_FROM = " from app.chat_turns t left join app.users u on u.id = t.user_id"
SEARCH_SELECT = (
    "select s.id, s.created_at, s.session_id, s.user_id, u.email as user_email,"
    " u.nickname as user_nickname, s.question, s.status, s.tbl_id, s.elapsed_ms, s.error"
)
SEARCH_FROM = " from app.search_logs s left join app.users u on u.id = s.user_id"


async def _rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await session.execute(text(sql), params)).mappings()]


async def _count(session: AsyncSession, sql: str, params: dict[str, Any]) -> int:
    return (await session.execute(text(sql), params)).scalar() or 0


async def turns(
    session: AsyncSession, limit: int, offset: int, **filters: Any
) -> tuple[list[dict[str, Any]], int]:
    where, params = conditions(TURN_COLUMNS, **filters)
    total = await _count(session, f"select count(*) from app.chat_turns t{where}", params)
    rows = await _rows(
        session,
        f"{TURN_SELECT}{TURN_FROM}{where} order by t.created_at desc, t.id desc"
        " limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return redact(rows), total


SESSIONS_INNER = (
    "select t.session_id, max(t.user_id) as user_id, min(t.created_at) as started_at,"
    " max(t.created_at) as last_at, count(*) as turns,"
    " (array_agg(t.message order by t.created_at, t.id))[1] as first_message,"
    " (array_agg(t.status order by t.created_at desc, t.id desc))[1] as last_status"
    " from app.chat_turns t group by t.session_id"
)


def _session_filters(
    user_id: int | None,
    guest: bool | None,
    q: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    if user_id is not None:
        parts.append("c.user_id = :user_id")
        params["user_id"] = user_id
    if guest is not None:
        parts.append(f"c.user_id is {'null' if guest else 'not null'}")
    if q and q.strip():
        parts.append(
            "exists (select 1 from app.chat_turns m where m.session_id = c.session_id"
            " and m.message ilike :q escape '\\')"
        )
        params["q"] = like(q.strip())
    if since is not None:
        parts.append("c.last_at >= :since")
        params["since"] = since
    if until is not None:
        parts.append("c.last_at < :until")
        params["until"] = until
    return (" where " + " and ".join(parts) if parts else ""), params


async def people(
    session: AsyncSession,
    limit: int,
    offset: int,
    guest: bool | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    where, params = _session_filters(None, guest, q, since, until)
    grouped = (
        "select c.user_id, count(*) as sessions, sum(c.turns) as turns,"
        " min(c.started_at) as first_at, max(c.last_at) as last_at"
        f" from ({SESSIONS_INNER}) c{where} group by c.user_id"
    )
    total = await _count(session, f"select count(*) from ({grouped}) g", params)
    rows = await _rows(
        session,
        "select g.*, u.email as user_email, u.nickname as user_nickname"
        f" from ({grouped}) g left join app.users u on u.id = g.user_id"
        " order by g.last_at desc, g.user_id nulls last limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return redact(rows), total


async def sessions(
    session: AsyncSession,
    limit: int,
    offset: int,
    user_id: int | None = None,
    guest: bool | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    where, params = _session_filters(user_id, guest, q, since, until)
    total = await _count(session, f"select count(*) from ({SESSIONS_INNER}) c{where}", params)
    rows = await _rows(
        session,
        "select c.*, u.email as user_email, u.nickname as user_nickname"
        f" from ({SESSIONS_INNER}) c left join app.users u on u.id = c.user_id{where}"
        " order by c.last_at desc, c.session_id limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return redact(rows), total


async def session_turns(session: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    return redact(
        await _rows(
            session,
            f"{TURN_SELECT}, t.conditions_before, t.conditions_after, t.sources, t.flow"
            f"{TURN_FROM}"
            " where t.session_id = :session_id order by t.created_at, t.id",
            {"session_id": session_id},
        )
    )


async def _has_search_logs(session: AsyncSession) -> bool:
    found = await session.execute(text("select to_regclass('app.search_logs')"))
    return found.scalar() is not None


async def searches(
    session: AsyncSession, limit: int, offset: int, **filters: Any
) -> tuple[list[dict[str, Any]], int]:
    if not await _has_search_logs(session):
        return [], 0
    where, params = conditions(SEARCH_COLUMNS, **filters)
    total = await _count(session, f"select count(*) from app.search_logs s{where}", params)
    rows = await _rows(
        session,
        f"{SEARCH_SELECT}{SEARCH_FROM}{where} order by s.created_at desc, s.id desc"
        " limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return [_masked(r) for r in rows], total


async def search_detail(session: AsyncSession, log_id: int) -> dict[str, Any] | None:
    if not await _has_search_logs(session):
        return None
    rows = await _rows(
        session,
        f"{SEARCH_SELECT}, s.slots, s.candidates, s.response, s.hcx_raw, s.timings{SEARCH_FROM}"
        " where s.id = :id",
        {"id": log_id},
    )
    return _masked(rows[0]) if rows else None


async def summary(session: AsyncSession, since: datetime) -> dict[str, Any]:
    params = {"since": since, "tz": TZ}
    totals = (
        await _rows(
            session,
            "select count(*) as turns, count(distinct session_id) as sessions,"
            " count(distinct user_id) as users, count(*) filter (where user_id is null)"
            " as guest_turns from app.chat_turns where created_at >= :since",
            {"since": since},
        )
    )[0]
    by_day = await _rows(
        session,
        "select (created_at at time zone :tz)::date as day, count(*) as turns,"
        " count(distinct session_id) as sessions from app.chat_turns"
        " where created_at >= :since group by 1 order by 1",
        params,
    )
    by_status = await _rows(
        session,
        "select coalesce(status, '') as name, count(*) as count from app.chat_turns"
        " where created_at >= :since group by 1 order by 2 desc, 1",
        {"since": since},
    )
    by_request_type = await _rows(
        session,
        "select coalesce(request_type, '') as name, count(*) as count from app.chat_turns"
        " where created_at >= :since group by 1 order by 2 desc, 1",
        {"since": since},
    )
    search_count, search_errors = 0, 0
    if await _has_search_logs(session):
        found = (
            await _rows(
                session,
                "select count(*) as n, count(error) as errors from app.search_logs"
                " where created_at >= :since",
                {"since": since},
            )
        )[0]
        search_count, search_errors = found["n"], found["errors"]
    return {
        **totals,
        "searches": search_count,
        "search_errors": search_errors,
        "by_day": by_day,
        "by_status": by_status,
        "by_request_type": by_request_type,
    }
