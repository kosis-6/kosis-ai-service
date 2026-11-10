"""관리자 전용 API: 통계표 적재 대기열, 사용자 관리

첫 관리자는 API로 만들 수 없고 스크립트로 만든다:
    python -m scripts.create_user admin@example.com --admin
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AdminUser, Session
from app.schemas.auth import UserAdminRead, UserList, UserUpdate
from app.services import load_queue, users

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/load-jobs")
async def load_jobs(
    user: AdminUser, limit: int = Query(default=20, ge=1, le=200)
) -> dict[str, Any]:
    """통계표 적재 대기열: 상태별 개수, 대기·진행 중인 표(처리 순서), 최근 실패"""
    return await load_queue.overview(limit)


@router.get("/users")
async def list_users(
    user: AdminUser,
    session: Session,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UserList:
    """가입 순서대로 사용자 목록 (정지 여부 포함)"""
    rows, total = await users.list_users(session, limit, offset)
    return UserList(total=total, users=[UserAdminRead.model_validate(u) for u in rows])


@router.patch("/users/{user_id}")
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
    return UserAdminRead.model_validate(target)
