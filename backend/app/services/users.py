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
    except IntegrityError as e:
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
    total = await session.scalar(select(func.count()).select_from(User)) or 0
    rows = await session.scalars(select(User).order_by(User.id).limit(limit).offset(offset))
    return list(rows), total


async def update(
    session: AsyncSession, user: User, is_active: bool | None = None, is_admin: bool | None = None
) -> User:
    if is_active is not None:
        if user.is_active and not is_active:
            user.token_version += 1
        user.is_active = is_active
    if is_admin is not None:
        user.is_admin = is_admin
    await session.commit()
    await session.refresh(user)
    return user
