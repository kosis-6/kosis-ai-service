"""라우터 공통 의존성: 로그인 사용자, 선택 로그인, 관리자

- 로그인이 필요한 API: 인자에 `user: CurrentUser`
- 로그인 없이도 쓰지만 로그인하면 사용자를 알고 싶은 API(/ask, /search): `user: OptionalUser`
- 관리자 전용: `user: AdminUser` (관리자 계정이고, ADMIN_ALLOW_IPS에 든 곳에서 온 요청)
"""

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

# Swagger(/docs)의 Authorize 버튼이 이 주소로 로그인한다
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")
# 토큰이 없어도 401을 내지 않는다 (선택 로그인)
optional_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

Session = Annotated[AsyncSession, Depends(get_session)]

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="로그인이 필요합니다.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def user_from_token(token: str, session: AsyncSession) -> User | None:
    """토큰이 맞고, 사용자가 있고, 정지되지 않았고, 토큰 번호가 지금 번호와 같고,
    그 로그인(app.user_sessions)이 끊기지 않았고 이 토큰이 그 로그인의 지금 토큰이면 사용자"""
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
    """토큰이 없으면 손님(None). 토큰을 보냈는데 틀렸거나 만료됐으면 401:
    조용히 손님으로 바꾸면 화면은 로그인한 줄 아는데 기록이 사용자와 연결되지 않는다"""
    if not token:
        return None
    user = await user_from_token(token, session)
    if user is None:
        raise UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def client_ip(request: Request) -> str:
    """요청한 곳의 IP. 운영에서는 Caddy가 붙인 X-Forwarded-For를 uvicorn이 읽어 넣은 값이다
    (--proxy-headers. Caddy는 바깥에서 보낸 X-Forwarded-For를 믿지 않고 실제 주소로 바꾼다)"""
    return request.client.host if request.client else ""


def admin_ip_allowed(request: Request) -> bool:
    """관리자 기능을 쓸 수 있는 곳에서 온 요청인가 (ADMIN_ALLOW_IPS). 비워 두면 어디서나"""
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
        # 관리자 비밀번호가 샜을 수 있다. 서버 로그로 남겨 알아챌 수 있게 한다
        logger.warning(
            "허용하지 않은 곳에서 관리자 기능 요청: user_id=%s ip=%s", user.id, client_ip(request)
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="이 곳에서는 관리자 기능을 쓸 수 없습니다."
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
