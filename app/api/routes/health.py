from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services import search

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | list[str]]:
    result = await session.execute(
        text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
    )
    return {"status": "ok", "extensions": sorted(result.scalars().all())}


@router.get("/health/search")
async def health_search() -> dict[str, str | int | bool]:
    """통계표 검색 준비 상태. retrieval이 keyword면 표 벡터가 없는 것 (pipeline.build_search)"""
    await search.load_tables()
    return {"status": "ok", **search.status()}
