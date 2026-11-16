"""질문 기록과 검색 상태의 보관 기간. 요청이 없어도 서버가 주기적으로 정리한다."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)
KEEP_DAYS = 30
INTERVAL_SECONDS = 3600


async def purge() -> None:
    from app.core.db import engine

    async with engine.begin() as conn:
        # 처음 요청 전에는 기록 테이블이 아직 없을 수 있다.
        for table, column in (
            ("chat_sessions", "updated_at"),
            ("chat_turns", "created_at"),
            ("search_logs", "created_at"),
        ):
            exists = await conn.execute(text("select to_regclass(:name)"), {"name": f"app.{table}"})
            if exists.scalar() is not None:
                await conn.execute(
                    text(
                        f"delete from app.{table} where {column} < now()"
                        " - make_interval(days => :days)"
                    ),
                    {"days": KEEP_DAYS},
                )


async def purge_files() -> None:
    from app.services import file_logs, qa

    cutoff = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
    for path in (qa.REQUEST_LOG, qa.MISSING_LOG):
        await asyncio.to_thread(file_logs.purge, path, cutoff)


async def run_forever() -> None:
    while True:
        for cleanup in (purge, purge_files):
            try:
                await cleanup()
            except Exception:
                logger.exception("사용 기록 보관 기간 정리 실패: %s", cleanup.__name__)
        await asyncio.sleep(INTERVAL_SECONDS)
