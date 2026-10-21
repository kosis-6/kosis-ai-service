from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session

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
