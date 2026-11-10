"""로그인 기록(app.user_sessions): Refresh Token 보관, 새 토큰 받기, 예전 Access Token 막기

흐름
  로그인            줄을 만들고 Access Token(15분) + Refresh Token(HttpOnly 쿠키, 14일)을 준다
  요청마다          Access Token의 jti가 그 줄의 access_jti와 같아야 한다
  POST /auth/refresh 쿠키의 Refresh Token을 확인하고 둘 다 새로 준다.
                    access_jti가 바뀌므로 예전 Access Token은 바로 못 쓴다 (블랙리스트 대신)
  로그아웃          그 줄을 끊는다(revoked_at)

Refresh Token 모양: "{줄 id}.{비밀 값}". DB에는 비밀 값의 SHA-256만 둔다.
새 토큰을 받을 때마다 비밀 값도 바뀐다. 바로 전 비밀 값이 다시 오면 누가 훔쳐 쓴 것으로 보고
그 줄을 끊는다 (진짜 사용자와 훔친 사람 모두 다시 로그인해야 한다).
단 새 값을 준 지 REUSE_GRACE_SECONDS 안이면 끊지 않는다: 탭 두 개가 동시에 새 토큰을 받으면
뒤 요청이 바로 전 값을 들고 온다. 이때는 Refresh Token은 그대로 두고 지금 Access Token만 준다.
동시에 온 요청이 같은 줄을 함께 고치지 않도록 새 토큰을 받을 때 줄을 잠근다(get(lock=True)).
아무 값이나 맞지 않으면 401만 주고 줄은 그대로 둔다: 줄 id는 짐작할 수 있어서, 틀린 값만으로
끊으면 누구든 남의 로그인을 끊을 수 있다.

판단(check_refresh, access_ok, rotate)은 DB를 모르는 함수로 두고,
저장(get, create, save, revoke_all)만 DB를 쓴다. 테스트는 저장 함수만 바꿔 끼운다.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import AccessClaims
from app.models.app import UserSession

COOKIE = "refresh_token"
COOKIE_PATH = "/auth"  # /auth/refresh, /auth/logout 에만 쿠키가 실린다
# 새 Refresh Token을 준 뒤 이 시간 안에 바로 전 값이 오면 탈취가 아니라 동시 요청으로 본다
REUSE_GRACE_SECONDS = 30


def now() -> datetime:
    return datetime.now(UTC)


def lifetime() -> timedelta:
    return timedelta(days=get_settings().refresh_token_days)


def _hash(secret: str) -> str:
    # 비밀 값은 32바이트 난수라 느린 해시(argon2)가 필요 없다
    return hashlib.sha256(secret.encode()).hexdigest()


def refresh_token(row: UserSession, secret: str) -> str:
    return f"{row.id}.{secret}"


def parse_refresh(token: str | None) -> tuple[int, str] | None:
    """ "{줄 id}.{비밀 값}" → (줄 id, 비밀 값). 모양이 틀리면 None"""
    if not token or "." not in token:
        return None
    sid, secret = token.split(".", 1)
    return (int(sid), secret) if sid.isdigit() and secret else None


def rotate(row: UserSession, at: datetime) -> tuple[str, str]:
    """새 Refresh Token 비밀 값과 새 Access Token 번호를 만들어 줄에 넣는다.
    예전 비밀 값과 예전 Access Token은 이때부터 못 쓴다. 쓸 때마다 만료를 14일 뒤로 민다"""
    secret, jti = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    row.prev_refresh_hash = getattr(row, "refresh_hash", None)
    row.refresh_hash = _hash(secret)
    row.access_jti = jti
    row.last_used_at = at
    row.expires_at = at + lifetime()
    return secret, jti


def check_refresh(row: UserSession | None, secret: str, at: datetime) -> str:
    """ok / missing(없는 줄) / revoked(끊김) / expired / reused(바로 전 비밀 값이 다시 옴, 탈취) /
    grace(바로 전 비밀 값이지만 새 값을 준 지 REUSE_GRACE_SECONDS 안. 동시 요청) /
    wrong(맞는 값이 아님. 줄은 끊지 않는다)"""
    if row is None:
        return "missing"
    if row.revoked_at is not None:
        return "revoked"
    if at >= row.expires_at:
        return "expired"
    given = _hash(secret)
    if hmac.compare_digest(row.refresh_hash, given):
        return "ok"
    if row.prev_refresh_hash and hmac.compare_digest(row.prev_refresh_hash, given):
        if at - row.last_used_at < timedelta(seconds=REUSE_GRACE_SECONDS):
            return "grace"
        return "reused"
    return "wrong"


def access_ok(row: UserSession | None, claims: AccessClaims, at: datetime) -> bool:
    """이 Access Token이 그 로그인의 지금 토큰인지 (새 토큰을 받았거나 끊겼으면 False)"""
    return (
        row is not None
        and row.user_id == claims.user_id
        and row.revoked_at is None
        and at < row.expires_at
        and hmac.compare_digest(row.access_jti, claims.jti)
    )


async def get(session: AsyncSession, session_id: int, lock: bool = False) -> UserSession | None:
    """lock=True면 이 요청이 커밋할 때까지 다른 요청이 같은 줄을 읽어 고치지 못하게 잠근다"""
    if lock:
        return await session.get(
            UserSession, session_id, with_for_update=True, populate_existing=True
        )
    return await session.get(UserSession, session_id)


async def create(session: AsyncSession, user_id: int, at: datetime) -> tuple[UserSession, str, str]:
    """새 로그인 줄을 만든다 → (줄, Refresh 비밀 값, Access 번호).
    이 사용자의 만료됐거나 끊긴 지 하루 지난 줄은 이때 지운다"""
    await session.execute(
        delete(UserSession).where(
            UserSession.user_id == user_id,
            or_(UserSession.expires_at < at, UserSession.revoked_at < at - timedelta(days=1)),
        )
    )
    row = UserSession(user_id=user_id)
    secret, jti = rotate(row, at)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, secret, jti


async def save(session: AsyncSession, row: UserSession) -> None:
    session.add(row)
    await session.commit()


async def revoke_all(session: AsyncSession, user_id: int, at: datetime) -> None:
    """이 사용자의 모든 로그인을 끊는다 (비밀번호 변경·계정 정지)"""
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    await session.commit()
