"""사용자 조회·생성. 라우터는 DB를 직접 다루지 않고 이 함수만 부른다
(테스트에서 이 함수들만 바꿔 끼우면 DB 없이 API를 확인할 수 있다)
"""

from sqlalchemy import func, select
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


async def create(
    session: AsyncSession,
    email: str,
    password: str,
    nickname: str | None = None,
    is_admin: bool = False,
) -> User:
    user = User(
        email=email.lower(),
        password_hash=hash_password(password),
        nickname=nickname,
        is_admin=is_admin,
    )
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


async def change_password(session: AsyncSession, user: User, new_password: str) -> User:
    """새 비밀번호로 바꾸고 토큰 번호를 올린다 (다른 기기에 남은 예전 토큰이 끊긴다)"""
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    await session.commit()
    await session.refresh(user)
    return user


async def set_nickname(session: AsyncSession, user: User, nickname: str) -> User:
    user.nickname = nickname
    await session.commit()
    await session.refresh(user)
    return user


async def list_users(session: AsyncSession, limit: int, offset: int) -> tuple[list[User], int]:
    """가입 순서대로 사용자 목록과 전체 수"""
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    rows = await session.scalars(select(User).order_by(User.id).limit(limit).offset(offset))
    return list(rows), total


async def update(
    session: AsyncSession, user: User, is_active: bool | None = None, is_admin: bool | None = None
) -> User:
    """관리자가 계정을 정지·복구하거나 관리자 권한을 주고 뺀다.
    정지할 때는 토큰 번호도 올린다: 나중에 다시 풀어도 정지 전 토큰은 살아나지 않는다"""
    if is_active is not None:
        if user.is_active and not is_active:
            user.token_version += 1
        user.is_active = is_active
    if is_admin is not None:
        user.is_admin = is_admin
    await session.commit()
    await session.refresh(user)
    return user
