import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import AccessClaims, secret_key
from app.models.app import UserSession

COOKIE = "refresh_token"
COOKIE_PATH = "/auth"
REUSE_GRACE_SECONDS = 30


def now() -> datetime:
    return datetime.now(UTC)


def lifetime() -> timedelta:
    return timedelta(days=get_settings().refresh_token_days)


def max_lifetime() -> timedelta:
    return timedelta(days=get_settings().session_max_days)


class RefreshToken(NamedTuple):
    session_id: int
    gen: int
    mac: str


def _mac(row: UserSession, gen: int) -> str:
    msg = f"refresh:{row.id}:{gen}:{row.refresh_salt}".encode()
    digest = hmac.new(secret_key().encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def refresh_token(row: UserSession) -> str:
    return f"{row.id}.{row.refresh_gen}.{_mac(row, row.refresh_gen)}"


def _number(s: str, digits: int) -> bool:
    return s.isascii() and s.isdigit() and len(s) <= digits


def parse_refresh(token: str | None) -> RefreshToken | None:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    sid, gen, mac = parts
    if not (_number(sid, 18) and _number(gen, 9) and mac):
        return None
    return RefreshToken(int(sid), int(gen), mac)


def start(row: UserSession, at: datetime) -> str:
    row.refresh_salt = secrets.token_urlsafe(32)
    row.created_at = at
    row.refresh_gen = -1
    return rotate(row, at)


def rotate(row: UserSession, at: datetime) -> str:
    jti = secrets.token_urlsafe(16)
    row.refresh_gen += 1
    row.access_jti = jti
    row.last_used_at = at
    row.expires_at = min(at + lifetime(), row.created_at + max_lifetime())
    return jti


def check_refresh(row: UserSession | None, token: RefreshToken, at: datetime) -> str:
    if row is None:
        return "missing"
    if row.revoked_at is not None:
        return "revoked"
    if at >= row.expires_at:
        return "expired"
    if token.gen > row.refresh_gen or not hmac.compare_digest(token.mac, _mac(row, token.gen)):
        return "wrong"
    if token.gen == row.refresh_gen:
        return "ok"
    if token.gen == row.refresh_gen - 1 and at - row.last_used_at < timedelta(
        seconds=REUSE_GRACE_SECONDS
    ):
        return "grace"
    return "reused"


def access_ok(row: UserSession | None, claims: AccessClaims, at: datetime) -> bool:
    return (
        row is not None
        and row.user_id == claims.user_id
        and row.revoked_at is None
        and at < row.expires_at
        and hmac.compare_digest(row.access_jti, claims.jti)
    )


async def get(session: AsyncSession, session_id: int, lock: bool = False) -> UserSession | None:
    if lock:
        return await session.get(
            UserSession, session_id, with_for_update=True, populate_existing=True
        )
    return await session.get(UserSession, session_id)


async def create(
    session: AsyncSession, user_id: int, at: datetime, persistent: bool = True
) -> tuple[UserSession, str]:
    await session.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id,
            or_(UserSession.expires_at < at, UserSession.revoked_at < at - timedelta(days=1)),
        )
    )
    row = UserSession(user_id=user_id, persistent=persistent)
    jti = start(row, at)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, jti


async def save(session: AsyncSession, row: UserSession) -> None:
    session.add(row)
    await session.commit()


async def revoke_all(session: AsyncSession, user_id: int, at: datetime) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    await session.commit()
