"""회원가입·로그인·내 정보

로그아웃 API는 없다. JWT는 서버에 저장하지 않으므로 화면이 토큰을 지우면 로그아웃이다.
토큰을 강제로 끊어야 하면(비밀번호 변경, 계정 정지) 그때 토큰 버전이나 차단 목록을 더한다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, Session
from app.core.config import get_settings
from app.core.security import create_access_token
from app.schemas.auth import Token, UserCreate, UserRead
from app.services import users

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, session: Session) -> UserRead:
    try:
        user = await users.create(session, body.email, body.password)
    except users.EmailTaken as e:
        raise HTTPException(status_code=409, detail="이미 가입한 이메일입니다.") from e
    return UserRead.model_validate(user)


@router.post("/token")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: Session) -> Token:
    """OAuth2 표준 폼(username, password). username 칸에 이메일을 넣는다"""
    user = await users.authenticate(session, form.username, form.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 맞지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    minutes = get_settings().access_token_minutes
    return Token(access_token=create_access_token(user.id, minutes), expires_in=minutes * 60)


@router.get("/me")
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
