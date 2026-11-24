from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app import AdminAudit, User
from app.services.log_redaction import redact

MAX_VALUE = 200


def trimmed(query: dict[str, str]) -> dict[str, str] | None:
    return {k[:50]: v[:MAX_VALUE] for k, v in redact(query).items()} or None


async def write(
    session: AsyncSession, admin: User, method: str, path: str, query: dict[str, str]
) -> None:
    session.add(
        AdminAudit(
            admin_id=admin.id,
            admin_email=admin.email,
            method=method,
            path=path[:300],
            query=trimmed(query),
        )
    )
    await session.commit()


async def recent(session: AsyncSession, limit: int, offset: int) -> tuple[list[AdminAudit], int]:
    total = await session.scalar(select(func.count()).select_from(AdminAudit)) or 0
    rows = await session.scalars(
        select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), total
