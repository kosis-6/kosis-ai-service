"""로그인 기록(app.user_sessions): Refresh Token 보관, 새 토큰 받기, 예전 Access Token 막기

흐름
  로그인            줄을 만들고 Access Token(15분) + Refresh Token(HttpOnly 쿠키, 14일)을 준다
  요청마다          Access Token의 jti가 그 줄의 access_jti와 같아야 한다
  POST /auth/refresh 쿠키의 Refresh Token을 확인하고 둘 다 새로 준다.
                    access_jti가 바뀌므로 예전 Access Token은 바로 못 쓴다 (블랙리스트 대신)
  로그아웃          그 줄을 끊는다(revoked_at)

Refresh Token 모양: "{줄 id}.{세대}.{서명}". 서명 = HMAC(JWT_SECRET_KEY, 줄 id·세대·줄의 난수).
새 토큰을 받을 때마다 세대가 1 오른다. 서버는 예전 토큰을 쌓아 두지 않아도 서명을 다시 계산해
"예전에 진짜 발급한 값"인지 알 수 있다. 진짜 예전 값(몇 세대 전이든)이 다시 오면 누가 훔쳐 쓴 것으로
보고 그 줄을 끊는다 (진짜 사용자와 훔친 사람 모두 다시 로그인해야 한다).
바로 전 세대만 보면, 훔친 사람이 두 번 새로 받은 뒤 돌아온 진짜 사용자의 값은 "틀린 값"이 되어
훔친 사람의 로그인이 계속 살아 있었다.
단 새 값을 준 지 REUSE_GRACE_SECONDS 안에 바로 전 세대가 오면 끊지 않는다:
탭 두 개가 동시에 새 토큰을 받으면 뒤 요청이 바로 전 값을 들고 온다.
이때는 Refresh Token은 그대로 두고 지금 Access Token만 준다.
동시에 온 요청이 같은 줄을 함께 고치지 않도록 새 토큰을 받을 때 줄을 잠근다(get(lock=True)).
서명이 맞지 않으면 401만 주고 줄은 그대로 둔다: 줄 id는 짐작할 수 있어서, 틀린 값만으로
끊으면 누구든 남의 로그인을 끊을 수 있다.
DB에는 줄의 난수만 두고 토큰은 두지 않는다.
DB가 새어도 JWT_SECRET_KEY 없이는 토큰을 만들 수 없다.
쓸 때마다 만료를 refresh_token_days 뒤로 밀지만, 로그인한 지 session_max_days가 지나면
다시 로그인한다 (훔친 토큰을 계속 새로 받아도 그 기간을 넘겨 쓸 수 없다).

판단(start, rotate, check_refresh, access_ok)은 DB를 모르는 함수로 두고,
저장(get, create, save, revoke_all)만 DB를 쓴다. 테스트는 저장 함수만 바꿔 끼운다.
"""

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
COOKIE_PATH = "/auth"  # /auth/refresh, /auth/logout 에만 쿠키가 실린다
# 새 Refresh Token을 준 뒤 이 시간 안에 바로 전 값이 오면 탈취가 아니라 동시 요청으로 본다
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
    """이 줄의 지금 Refresh Token (줄 id가 생긴 뒤, 즉 저장한 뒤에 부른다)"""
    return f"{row.id}.{row.refresh_gen}.{_mac(row, row.refresh_gen)}"


def _number(s: str, digits: int) -> bool:
    return s.isascii() and s.isdigit() and len(s) <= digits


def parse_refresh(token: str | None) -> RefreshToken | None:
    """ "{줄 id}.{세대}.{서명}" → RefreshToken. 모양이 틀리면 None
    (이 모양 전에 준 "{줄 id}.{비밀 값}" 쿠키도 None → 다시 로그인)"""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    sid, gen, mac = parts
    # 숫자 자리 수를 막아 DB의 bigint 범위를 넘는 값으로 500이 나지 않게 한다
    if not (_number(sid, 18) and _number(gen, 9) and mac):
        return None
    return RefreshToken(int(sid), int(gen), mac)


def start(row: UserSession, at: datetime) -> str:
    """새 로그인 줄을 채운다 → Access 번호. 줄마다 난수를 새로 만든다"""
    row.refresh_salt = secrets.token_urlsafe(32)
    row.created_at = at
    row.refresh_gen = -1
    return rotate(row, at)


def rotate(row: UserSession, at: datetime) -> str:
    """세대를 올리고 새 Access Token 번호를 만든다 → Access 번호.
    예전 Refresh Token과 예전 Access Token은 이때부터 못 쓴다.
    쓸 때마다 만료를 refresh_token_days 뒤로 밀되, 로그인한 지 session_max_days를 넘기지 않는다"""
    jti = secrets.token_urlsafe(16)
    row.refresh_gen += 1
    row.access_jti = jti
    row.last_used_at = at
    row.expires_at = min(at + lifetime(), row.created_at + max_lifetime())
    return jti


def check_refresh(row: UserSession | None, token: RefreshToken, at: datetime) -> str:
    """ok / missing(없는 줄) / revoked(끊김) / expired / wrong(서명이 틀림. 줄은 끊지 않는다) /
    grace(바로 전 세대지만 새 값을 준 지 REUSE_GRACE_SECONDS 안. 동시 요청) /
    reused(진짜 예전 값이 다시 옴, 탈취)"""
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


async def create(
    session: AsyncSession, user_id: int, at: datetime, persistent: bool = True
) -> tuple[UserSession, str]:
    """새 로그인 줄을 만든다 → (줄, Access 번호). persistent=False면 "로그인 상태 유지" 안 함.
    이 사용자의 만료됐거나 끊긴 지 하루 지난 줄은 이때 지운다"""
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
    """이 사용자의 모든 로그인을 끊는다 (비밀번호 변경·계정 정지)"""
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    await session.commit()
