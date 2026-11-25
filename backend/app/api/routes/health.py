from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.services import search, search_llm

router = APIRouter(tags=["서버 상태"])


@router.get("/health", summary="서버 상태")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db", summary="DB 연결 상태")
async def health_db(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str | list[str]]:
    result = await session.execute(
        text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
    )
    return {"status": "ok", "extensions": sorted(result.scalars().all())}


@router.get("/health/search", summary="통계표 검색 준비 상태")
async def health_search(
    response: Response, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str | int | bool]:
    try:
        catalog_ready = await session.scalar(
            text(
                "select exists (select 1 from catalog.table_meta"
                " join catalog.table_families using (tbl_id))"
            )
        )
        tables = await search.load_tables() if catalog_ready else []
    except SQLAlchemyError:
        tables = []
    if not tables:
        response.status_code = 503
        return {"status": "unavailable", "tables": 0}
    gemma = await search_llm.connection()
    report = search.status()
    status = "ok"
    if gemma == "unavailable":
        status = "degraded" if get_settings().search_llm == "auto" else "unavailable"
        if status == "unavailable":
            response.status_code = 503
    elif report["search_llm_fallback"]:
        status = "degraded"
    return {"status": status, "gemma_connection": gemma, **report}
