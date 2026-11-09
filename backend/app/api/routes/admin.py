"""관리자 전용 API"""

from typing import Any

from fastapi import APIRouter, Query

from app.api.deps import AdminUser
from app.services import load_queue

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/load-jobs")
async def load_jobs(
    user: AdminUser, limit: int = Query(default=20, ge=1, le=200)
) -> dict[str, Any]:
    """통계표 적재 대기열: 상태별 개수, 대기·진행 중인 표(처리 순서), 최근 실패"""
    return await load_queue.overview(limit)
