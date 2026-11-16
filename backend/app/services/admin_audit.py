"""관리자 API 호출 기록 (app.admin_audit): 누가 언제 무엇을 봤는지·바꿨는지

관리자는 사용자의 질문과 이메일을 볼 수 있다. 그 열람과 계정 변경을 남겨 두어, 나중에 누가 무엇을
봤는지 확인할 수 있게 한다. 라우터(app/api/routes/admin.py)가 관리자 API 마다 write 를 부른다.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app import AdminAudit, User
from app.services.log_redaction import redact

# 거르는 값이 길어도 기록은 이만큼만 남긴다
MAX_VALUE = 200


def trimmed(query: dict[str, str]) -> dict[str, str] | None:
    """주소의 거르는 값 (user_id, q, since 등). 없으면 None"""
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
    """최근 것부터. (줄, 전체 수)"""
    total = await session.scalar(select(func.count()).select_from(AdminAudit)) or 0
    rows = await session.scalars(
        select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit).offset(offset)
    )
    return list(rows), total
