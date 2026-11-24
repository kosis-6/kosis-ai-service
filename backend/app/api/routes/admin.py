import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from app.api.deps import AdminUser, Session
from app.schemas.auth import UserAdminRead, UserList, UserUpdate
from app.schemas.logs import (
    AuditList,
    AuditRead,
    LogSummary,
    PersonList,
    SearchLogDetail,
    SearchLogList,
    SessionDetail,
    SessionList,
    TurnList,
)
from app.services import admin_audit, auth_sessions, load_queue, plan_cache, user_logs, users

logger = logging.getLogger(__name__)


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def audit(request: Request, admin: AdminUser, session: Session) -> None:
    try:
        await admin_audit.write(
            session, admin, request.method, request.url.path, dict(request.query_params)
        )
    except Exception:
        logger.exception("관리자 호출 기록 실패")
        try:
            await session.rollback()
        except Exception:
            pass


router = APIRouter(
    prefix="/admin", tags=["관리자"], dependencies=[Depends(no_store), Depends(audit)]
)


@router.get("/load-jobs", summary="통계표 적재 대기열")
async def load_jobs(
    user: AdminUser, limit: int = Query(default=20, ge=1, le=200)
) -> dict[str, Any]:
    return await load_queue.overview(limit)


@router.get("/users", summary="사용자 목록")
async def list_users(
    user: AdminUser,
    session: Session,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10_000_000),
) -> UserList:
    rows, total = await users.list_users(session, limit, offset)
    return UserList(total=total, users=[UserAdminRead.model_validate(u) for u in rows])


@router.patch("/users/{user_id}", summary="사용자 정지·복구, 관리자 지정")
async def update_user(
    user_id: int, body: UserUpdate, admin: AdminUser, session: Session
) -> UserAdminRead:
    target = await users.get(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="없는 사용자입니다.")
    if target.id == admin.id and (body.is_active is False or body.is_admin is False):
        raise HTTPException(
            status_code=400, detail="자기 계정은 정지하거나 관리자 권한을 뺄 수 없습니다."
        )
    target = await users.update(session, target, body.is_active, body.is_admin)
    if body.is_active is False:
        await auth_sessions.revoke_all(session, target.id, auth_sessions.now())
    return UserAdminRead.model_validate(target)


NO_NUL = r"^[^\x00]*$"
MAX_OFFSET = 10_000_000
MAX_ID = 2**63 - 1

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]
UserId = Annotated[int | None, Query(ge=1, le=MAX_ID, description="이 사용자의 기록만")]
Guest = Annotated[
    bool | None, Query(description="true: 손님(로그인 안 함)만, false: 로그인한 사용자만")
]
Word = Annotated[
    str | None, Query(max_length=100, pattern=NO_NUL, description="질문에 들어 있는 말")
]
SessionId = Annotated[
    str | None, Query(max_length=64, pattern=NO_NUL, description="이 대화의 기록만")
]
Code = Annotated[str | None, Query(max_length=40, pattern=NO_NUL)]
Since = Annotated[datetime | None, Query(description="이 시각부터 (ISO 8601)")]
Until = Annotated[datetime | None, Query(description="이 시각 전까지 (ISO 8601)")]


def _aware(at: datetime | None) -> datetime | None:
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


@router.get("/logs/summary", summary="최근 사용량 요약")
async def log_summary(
    user: AdminUser, session: Session, days: int = Query(default=7, ge=1, le=30)
) -> LogSummary:
    since = datetime.now(UTC) - timedelta(days=days)
    found = await user_logs.summary(session, since)
    return LogSummary(days=days, since=since, **found)


@router.get("/logs/people", summary="사람별 대화 묶음")
async def log_people(
    user: AdminUser,
    session: Session,
    limit: Limit = 50,
    offset: Offset = 0,
    guest: Guest = None,
    q: Word = None,
    since: Since = None,
    until: Until = None,
) -> PersonList:
    rows, total = await user_logs.people(
        session, limit, offset, guest, q, _aware(since), _aware(until)
    )
    return PersonList(total=total, people=rows)


@router.get("/logs/sessions", summary="대화 목록")
async def log_sessions(
    user: AdminUser,
    session: Session,
    limit: Limit = 50,
    offset: Offset = 0,
    user_id: UserId = None,
    guest: Guest = None,
    q: Word = None,
    since: Since = None,
    until: Until = None,
) -> SessionList:
    rows, total = await user_logs.sessions(
        session, limit, offset, user_id, guest, q, _aware(since), _aware(until)
    )
    return SessionList(total=total, sessions=rows)


@router.get("/logs/sessions/{session_id}", summary="대화 하나의 턴")
async def log_session(
    session_id: Annotated[str, Path(max_length=64, pattern=NO_NUL)],
    user: AdminUser,
    session: Session,
) -> SessionDetail:
    rows = await user_logs.session_turns(session, session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="없는 대화입니다.")
    who = next((r for r in rows if r.get("user_id") is not None), rows[0])
    return SessionDetail(
        session_id=session_id,
        user_id=who.get("user_id"),
        user_email=who.get("user_email"),
        user_nickname=who.get("user_nickname"),
        turns=rows,
    )


@router.get("/logs/turns", summary="대화 턴 목록")
async def log_turns(
    user: AdminUser,
    session: Session,
    limit: Limit = 50,
    offset: Offset = 0,
    user_id: UserId = None,
    guest: Guest = None,
    session_id: SessionId = None,
    status: Annotated[Code, Query(description="/ask 응답의 status (예: ok, tables)")] = None,
    request_type: Annotated[Code, Query(description="예: new, change, summary")] = None,
    q: Word = None,
    since: Since = None,
    until: Until = None,
) -> TurnList:
    rows, total = await user_logs.turns(
        session,
        limit,
        offset,
        user_id=user_id,
        guest=guest,
        session_id=session_id,
        status=status,
        request_type=request_type,
        q=q,
        since=_aware(since),
        until=_aware(until),
    )
    return TurnList(total=total, turns=rows)


@router.get("/logs/searches", summary="통계표 찾기 기록 목록")
async def log_searches(
    user: AdminUser,
    session: Session,
    limit: Limit = 50,
    offset: Offset = 0,
    user_id: UserId = None,
    guest: Guest = None,
    session_id: SessionId = None,
    status: Annotated[Code, Query(description="예: ok, partial_match, ambiguous")] = None,
    has_error: bool | None = Query(default=None, description="true: 오류가 난 기록만"),
    q: Word = None,
    since: Since = None,
    until: Until = None,
) -> SearchLogList:
    rows, total = await user_logs.searches(
        session,
        limit,
        offset,
        user_id=user_id,
        guest=guest,
        session_id=session_id,
        status=status,
        has_error=has_error,
        q=q,
        since=_aware(since),
        until=_aware(until),
    )
    return SearchLogList(total=total, searches=rows)


@router.get("/logs/searches/{log_id}", summary="통계표 찾기 기록 하나")
async def log_search(
    log_id: Annotated[int, Path(ge=1, le=MAX_ID)], user: AdminUser, session: Session
) -> SearchLogDetail:
    row = await user_logs.search_detail(session, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="없는 기록입니다.")
    return SearchLogDetail(**row)


@router.get("/audit", summary="관리자 호출 기록")
async def audit_records(
    user: AdminUser, session: Session, limit: Limit = 50, offset: Offset = 0
) -> AuditList:
    rows, total = await admin_audit.recent(session, limit, offset)
    return AuditList(total=total, records=[AuditRead.model_validate(r) for r in rows])


@router.get("/dynamic-plans", summary="동적 지표 계산 방법 (캐시)")
async def dynamic_plans(user: AdminUser, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    return await plan_cache.listing(limit, offset)


@router.delete("/dynamic-plans/{plan_id}", summary="동적 지표 계산 방법 지우기", status_code=204)
async def drop_dynamic_plan(
    plan_id: Annotated[int, Path(ge=1, le=MAX_ID)], user: AdminUser
) -> Response:
    if not await plan_cache.drop(plan_id):
        raise HTTPException(status_code=404, detail="계산 방법이 없습니다.")
    return Response(status_code=204)
