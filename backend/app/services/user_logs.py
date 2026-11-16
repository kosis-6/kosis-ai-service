"""사용자 기록 조회 (관리자용). 쓰기는 하지 않는다

기록은 두 곳에 쌓인다:
- app.chat_turns   /ask 대화의 턴 (ask_session.log_turn). 사용자가 한 말, 요청 종류, 답의 첫 문단,
                   조건이 어떻게 바뀌었는지. 30일 보관
- app.search_logs  통계표 찾기 한 번 (search_log.write). 슬롯·후보·단계별 시간·오류.
                   /ask 가 수치로 답하지 못해 통계표를 찾을 때도 여기에 남는다

두 기록의 대화 id 는 서로 다르다(/ask 는 "A_…", 통계표 찾기는 "S_…"). 그래서 하나로 합치지 않고
대화(턴)를 기본 화면으로, 통계표 찾기는 따로 본다.

라우터는 DB 를 직접 다루지 않고 이 함수들만 부른다 (테스트에서 이 함수들만 바꿔 끼운다).
"""

import re
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 요약의 날짜 경계 (하루를 한국 시간으로 나눈다)
TZ = "Asia/Seoul"


# 오류 글에는 호출 주소가 통째로 들어갈 수 있다 (KOSIS 주소의 apiKey 등).
# 화면으로 내보내기 전에 가린다
SECRET_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|service[_-]?key|access[_-]?token|token|secret|password)=[^&\s\"'<>]+"
)
SECRET_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def mask(value: str | None) -> str | None:
    """글 속의 키·토큰 값을 *** 로 바꾼다"""
    if not value:
        return value
    return SECRET_BEARER.sub("Bearer ***", SECRET_PARAM.sub(r"\1=***", value))


def _masked(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "error": mask(row.get("error"))}


def like(word: str) -> str:
    """부분 일치 검색어. 사용자가 넣은 %, _ 는 글자 그대로 찾는다"""
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
    """거르는 조건 → (where 절, 값). 값은 모두 바인딩하고, 칸 이름은 column 에 적힌 것만 쓴다.
    guest: True 면 손님(로그인 안 함)만, False 면 로그인한 사용자만
    since ≤ created_at < until"""
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
    """/ask 대화의 턴을 최근 것부터. (줄, 전체 수)"""
    where, params = conditions(TURN_COLUMNS, **filters)
    total = await _count(session, f"select count(*) from app.chat_turns t{where}", params)
    rows = await _rows(
        session,
        f"{TURN_SELECT}{TURN_FROM}{where} order by t.created_at desc, t.id desc"
        " limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return rows, total


# 대화 하나를 한 줄로: 누가, 언제부터 언제까지, 몇 턴, 첫 질문
SESSIONS_INNER = (
    "select t.session_id, max(t.user_id) as user_id, min(t.created_at) as started_at,"
    " max(t.created_at) as last_at, count(*) as turns,"
    " (array_agg(t.message order by t.created_at, t.id))[1] as first_message,"
    " (array_agg(t.status order by t.created_at desc, t.id desc))[1] as last_status"
    " from app.chat_turns t group by t.session_id"
)


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
    """대화 목록을 마지막 턴이 최근인 것부터. (줄, 전체 수)
    q 는 그 대화의 어느 턴에든 들어 있으면 맞고, since·until 은 마지막 턴 시각으로 거른다"""
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
    where = " where " + " and ".join(parts) if parts else ""
    total = await _count(session, f"select count(*) from ({SESSIONS_INNER}) c{where}", params)
    rows = await _rows(
        session,
        "select c.*, u.email as user_email, u.nickname as user_nickname"
        f" from ({SESSIONS_INNER}) c left join app.users u on u.id = c.user_id{where}"
        " order by c.last_at desc, c.session_id limit :limit offset :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return rows, total


async def session_turns(session: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    """대화 하나의 턴을 말한 순서대로. 조건이 어떻게 바뀌었는지와 출처까지. 없으면 빈 목록"""
    return await _rows(
        session,
        f"{TURN_SELECT}, t.conditions_before, t.conditions_after, t.sources{TURN_FROM}"
        " where t.session_id = :session_id order by t.created_at, t.id",
        {"session_id": session_id},
    )


async def _has_search_logs(session: AsyncSession) -> bool:
    """app.search_logs 는 처음 기록할 때 만들어진다 (search_log.write). 아직 없을 수 있다"""
    found = await session.execute(text("select to_regclass('app.search_logs')"))
    return found.scalar() is not None


async def searches(
    session: AsyncSession, limit: int, offset: int, **filters: Any
) -> tuple[list[dict[str, Any]], int]:
    """통계표 찾기 기록을 최근 것부터. (줄, 전체 수)"""
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
    """통계표 찾기 기록 하나: 슬롯, 후보와 점수, 응답, HCX 원문, 단계별 시간까지"""
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
    """since 부터 지금까지의 사용량: 전체 수, 날짜별(한국 시간), 상태별, 요청 종류별"""
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
