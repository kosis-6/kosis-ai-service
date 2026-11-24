import logging
from ipaddress import ip_address
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.security import decode_access_token
from app.models.app import User
from app.services import auth_sessions, users

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")
optional_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

Session = Annotated[AsyncSession, Depends(get_session)]

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="로그인이 필요합니다.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def user_from_token(token: str, session: AsyncSession) -> User | None:
    claims = decode_access_token(token)
    if claims is None:
        return None
    user = await users.get(session, claims.user_id)
    if user is None or not user.is_active or user.token_version != claims.version:
        return None
    row = await auth_sessions.get(session, claims.session_id)
    if not auth_sessions.access_ok(row, claims, auth_sessions.now()):
        return None
    return user


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], session: Session) -> User:
    user = await user_from_token(token, session)
    if user is None:
        raise UNAUTHORIZED
    return user


async def get_optional_user(
    token: Annotated[str | None, Depends(optional_scheme)], session: Session
) -> User | None:
    if not token:
        return None
    user = await user_from_token(token, session)
    if user is None:
        raise UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def admin_ip_allowed(request: Request) -> bool:
    networks = get_settings().admin_networks
    if not networks:
        return True
    try:
        ip = ip_address(client_ip(request))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks)


async def require_admin(user: CurrentUser, request: Request) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="관리자만 쓸 수 있습니다.")
    if not admin_ip_allowed(request):
        logger.warning(
            "허용하지 않은 곳에서 관리자 기능 요청: user_id=%s ip=%s", user.id, client_ip(request)
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="이 곳에서는 관리자 기능을 쓸 수 없습니다."
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
