"""사용자 조회·생성. 라우터는 DB를 직접 다루지 않고 이 함수만 부른다
(테스트에서 이 함수들만 바꿔 끼우면 DB 없이 API를 확인할 수 있다)
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models.app import User


class EmailTaken(Exception):
    pass


async def get(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email.lower()))


async def create(session: AsyncSession, email: str, password: str, is_admin: bool = False) -> User:
    user = User(email=email.lower(), password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as e:  # 동시에 같은 이메일로 가입한 경우도 여기서 잡힌다
        await session.rollback()
        raise EmailTaken(email) from e
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    user = await get_by_email(session, email)
    if not verify_password(password, user.password_hash if user else None):
        return None
    return user if user and user.is_active else None
