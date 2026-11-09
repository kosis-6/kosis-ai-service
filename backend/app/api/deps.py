"""라우터 공통 의존성: 로그인 사용자, 관리자

로그인이 필요한 API는 인자에 `user: CurrentUser`, 관리자 전용은 `user: AdminUser`를 넣는다.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import decode_access_token
from app.models.app import User
from app.services import users

# Swagger(/docs)의 Authorize 버튼이 이 주소로 로그인한다
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

Session = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], session: Session) -> User:
    user_id = decode_access_token(token)
    user = await users.get(session, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="관리자만 쓸 수 있습니다.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
