"""관리자 전용 API: 통계표 적재 대기열, 사용자 관리, 사용자 기록 조회, 동적 지표 계산 방법

첫 관리자는 API로 만들 수 없고 스크립트로 만든다:
    python -m scripts.create_user admin@example.com --admin
"""

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
    """관리자 응답(사용자 이메일·질문)을 브라우저나 중간 서버가 저장해 두지 않게 한다"""
    response.headers["Cache-Control"] = "no-store"


async def audit(request: Request, admin: AdminUser, session: Session) -> None:
    """관리자 API 호출을 남긴다 (누가, 무엇을, 어떤 조건으로). 관리자가 아니면 여기 오기 전에 403.
    기록에 실패해도 요청은 막지 않고 서버 로그에 남긴다"""
    try:
        await admin_audit.write(
            session, admin, request.method, request.url.path, dict(request.query_params)
        )
    except Exception:
        logger.exception("관리자 호출 기록 실패")
        try:
            await session.rollback()
        except Exception:  # 세션까지 못 쓰는 상태면 요청이 그 오류를 알린다
            pass


router = APIRouter(
    prefix="/admin", tags=["관리자"], dependencies=[Depends(no_store), Depends(audit)]
)


@router.get("/load-jobs", summary="통계표 적재 대기열")
async def load_jobs(
    user: AdminUser, limit: int = Query(default=20, ge=1, le=200)
) -> dict[str, Any]:
    """통계표 적재 대기열: 상태별 개수, 대기·진행 중인 표(처리 순서), 최근 실패"""
    return await load_queue.overview(limit)


@router.get("/users", summary="사용자 목록")
async def list_users(
    user: AdminUser,
    session: Session,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10_000_000),
) -> UserList:
    """가입 순서대로 사용자 목록 (정지 여부 포함)"""
    rows, total = await users.list_users(session, limit, offset)
    return UserList(total=total, users=[UserAdminRead.model_validate(u) for u in rows])


@router.patch("/users/{user_id}", summary="사용자 정지·복구, 관리자 지정")
async def update_user(
    user_id: int, body: UserUpdate, admin: AdminUser, session: Session
) -> UserAdminRead:
    """계정 정지·복구(is_active), 관리자 권한 주기·빼기(is_admin).
    정지하면 그 사용자의 토큰이 끊긴다.
    자기 자신은 정지하거나 관리자 권한을 뺄 수 없다 (관리자가 아무도 없게 되는 것을 막는다)"""
    target = await users.get(session, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="없는 사용자입니다.")
    if target.id == admin.id and (body.is_active is False or body.is_admin is False):
        raise HTTPException(
            status_code=400, detail="자기 계정은 정지하거나 관리자 권한을 뺄 수 없습니다."
        )
    target = await users.update(session, target, body.is_active, body.is_admin)
    if body.is_active is False:  # 정지한 사용자의 로그인을 모두 끊는다
        await auth_sessions.revoke_all(session, target.id, auth_sessions.now())
    return UserAdminRead.model_validate(target)


# ---- 사용자 기록 조회 (app/services/user_logs.py) -------------------------------------------

# PostgreSQL 이 받지 못하는 값(NUL 글자, bigint 를 넘는 수)은 500 이 아니라 422 로 돌려보낸다
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
    """시간대 없이 온 시각은 UTC 로 본다 (DB 칸이 timestamptz 라 시간대가 있어야 한다)"""
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


@router.get("/logs/summary", summary="최근 사용량 요약")
async def log_summary(
    user: AdminUser, session: Session, days: int = Query(default=7, ge=1, le=30)
) -> LogSummary:
    """최근 days 일의 /ask 턴 수·대화 수·사용자 수, 날짜별(한국 시간)·상태별·요청 종류별 개수.
    턴 기록은 30일만 보관한다"""
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
    """대화를 사람별로 묶어 마지막 대화가 최근인 사람부터.
    사람마다 대화 창 수, 질문 수, 처음·마지막 시각.
    로그인 사용자는 한 사람이 한 줄이고(창을 여러 개 열어도 모인다), 비로그인 사용자는
    user_id 가 null 인 한 줄로 모인다.
    그 사람의 대화는 /logs/sessions?user_id= (비로그인은 guest=true)"""
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
    """/ask 대화를 마지막 턴이 최근인 것부터. 대화마다 사용자, 시작·마지막 시각, 턴 수, 첫 질문.
    q 는 그 대화의 어느 턴에든 들어 있으면 맞는다"""
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
    """대화 하나를 말한 순서대로: 사용자가 한 말, 답의 첫 문단, 조건이 어떻게 바뀌었는지"""
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
    """/ask 턴을 최근 것부터. 대화로 묶지 않고 질문 하나하나를 거를 때 쓴다"""
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
    """통계표 찾기 기록을 최근 것부터. /ask 가 수치로 답하지 못해 통계표를 찾은 것도 들어 있다
    (대화 id 는 /ask 의 것과 다르다)"""
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
    """슬롯, 후보와 점수, 응답, HCX 원문, 단계별 시간까지"""
    row = await user_logs.search_detail(session, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="없는 기록입니다.")
    return SearchLogDetail(**row)


@router.get("/audit", summary="관리자 호출 기록")
async def audit_records(
    user: AdminUser, session: Session, limit: Limit = 50, offset: Offset = 0
) -> AuditList:
    """관리자 API 를 누가 언제 어떤 조건으로 불렀는지, 최근 것부터 (app.admin_audit).
    사용자 기록 열람과 계정 정지·권한 변경이 모두 남는다"""
    rows, total = await admin_audit.recent(session, limit, offset)
    return AuditList(total=total, records=[AuditRead.model_validate(r) for r in rows])


@router.get("/dynamic-plans", summary="동적 지표 계산 방법 (캐시)")
async def dynamic_plans(user: AdminUser, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
    """사전에 없는 지표를 통계표에서 바로 계산한 방법들 (많이 쓴 것부터) + 서버 메모리 캐시 상태.
    자주 쓰인 것은 정의를 확인해 지표 사전(config/catalog_notes.toml)에 올린다"""
    return await plan_cache.listing(limit, offset)


@router.delete("/dynamic-plans/{plan_id}", summary="동적 지표 계산 방법 지우기", status_code=204)
async def drop_dynamic_plan(
    plan_id: Annotated[int, Path(ge=1, le=MAX_ID)], user: AdminUser
) -> Response:
    """틀린 표·칸으로 계산하던 방법을 지운다. 다음 질문부터 통계표를 다시 찾는다"""
    if not await plan_cache.drop(plan_id):
        raise HTTPException(status_code=404, detail="계산 방법이 없습니다.")
    return Response(status_code=204)
