"""회원가입(이메일 인증)·로그인·내 정보·비밀번호 변경

회원가입 순서: POST /auth/email-code → 메일로 받은 6자리를 POST /auth/email-code/verify
→ 30분 안에 POST /auth/register. 아이디는 이메일이다.

로그아웃 API는 없다. JWT는 서버에 저장하지 않으므로 화면이 토큰을 지우면 로그아웃이다.
비밀번호를 바꾸거나 관리자가 계정을 정지하면 사용자의 토큰 번호(token_version)가 올라가
그 전에 발급한 토큰이 모두 끊긴다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, Session
from app.core import login_limit
from app.core.config import get_settings
from app.core.security import create_access_token, verify_password
from app.models.app import User
from app.schemas.auth import (
    EmailCodeRequest,
    EmailCodeSent,
    EmailCodeVerify,
    EmailVerified,
    PasswordChange,
    ProfileUpdate,
    Token,
    UserCreate,
    UserRead,
)
from app.services import email_verify, mailer, users

router = APIRouter(prefix="/auth", tags=["로그인·회원가입"])

# 인증번호 메일: 같은 IP에서 1시간에 10통까지 (남의 메일함에 메일을 쏟아붓는 것을 막는다)
code_sends = login_limit.LoginLimiter(max_failures=10, window=60 * 60)

CODE_ERRORS = {
    "missing": "인증번호를 먼저 받아 주세요.",
    "expired": "인증번호 유효 시간이 지났습니다. 다시 받아 주세요.",
    "too_many": "인증번호를 여러 번 틀렸습니다. 다시 받아 주세요.",
}


def _too_many(detail: str, wait: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(wait)},
    )


def _token(user: User) -> Token:
    minutes = get_settings().access_token_minutes
    return Token(
        access_token=create_access_token(user.id, minutes, user.token_version),
        expires_in=minutes * 60,
    )


@router.post("/email-code", summary="① 인증번호 받기", status_code=status.HTTP_202_ACCEPTED)
async def send_email_code(
    body: EmailCodeRequest, request: Request, session: Session
) -> EmailCodeSent:
    """가입할 이메일로 6자리 인증번호를 보낸다. 10분 동안 쓸 수 있고, 60초 뒤에 다시 받을 수 있다.
    다시 받으면 전에 받은 번호는 쓸 수 없다"""
    if await users.get_by_email(session, body.email) is not None:
        raise HTTPException(status_code=409, detail="이미 가입한 이메일입니다.")
    at = email_verify.now()
    row = await email_verify.get(session, body.email)
    wait = email_verify.resend_wait(row, at)
    if wait:
        raise _too_many(f"인증번호는 {wait}초 뒤에 다시 받을 수 있습니다.", wait)
    ip = request.client.host if request.client else "-"
    wait = code_sends.retry_after(ip)
    if wait:
        raise _too_many("인증번호를 너무 많이 요청했습니다. 잠시 뒤에 다시 시도해 주세요.", wait)

    row, code = email_verify.issue(row, body.email, at)
    await email_verify.save(session, row)
    code_sends.fail(ip)  # 보낸 횟수로 센다
    try:
        await mailer.send(
            body.email,
            "[KOSIS 통계 서비스] 회원가입 인증번호",
            f"인증번호: {code}\n\n{email_verify.CODE_MINUTES}분 안에 입력해 주세요.\n"
            "직접 요청하지 않았다면 이 메일은 무시하셔도 됩니다.",
        )
    except mailer.MailNotConfigured:
        await email_verify.delete(session, body.email)
        raise
    except Exception as e:  # SMTP 연결·인증 실패
        await email_verify.delete(session, body.email)  # 바로 다시 요청할 수 있게
        raise HTTPException(
            status_code=502, detail="인증 메일을 보내지 못했습니다. 잠시 뒤에 다시 시도해 주세요."
        ) from e
    return EmailCodeSent(
        expires_in=email_verify.CODE_MINUTES * 60, resend_after=email_verify.RESEND_SECONDS
    )


@router.post("/email-code/verify", summary="② 인증번호 확인")
async def verify_email_code(body: EmailCodeVerify, session: Session) -> EmailVerified:
    """받은 인증번호를 확인한다. 맞으면 30분 안에 가입해야 한다. 5번 틀리면 다시 받아야 한다"""
    row = await email_verify.get(session, body.email)
    at = email_verify.now()
    result = email_verify.check(row, body.code, at)
    if result == "wrong":
        row.attempts += 1
        await email_verify.save(session, row)
        left = email_verify.MAX_ATTEMPTS - row.attempts
        detail = (
            f"인증번호가 맞지 않습니다. ({left}번 더 시도할 수 있습니다)"
            if left > 0
            else CODE_ERRORS["too_many"]
        )
        raise HTTPException(status_code=400, detail=detail)
    if result != "ok":
        raise HTTPException(status_code=400, detail=CODE_ERRORS[result])
    row.verified_at = at
    await email_verify.save(session, row)
    return EmailVerified(expires_in=email_verify.VERIFIED_MINUTES * 60)


@router.post("/register", summary="③ 회원가입", status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, session: Session) -> UserRead:
    """이메일 인증을 마친 뒤 30분 안에 가입한다.
    비밀번호: 6~20자, 영문(대소문자 상관없음)·숫자·특수기호를 모두 포함
    닉네임: 앞뒤 공백을 뺀 2~20자 (같은 닉네임도 된다)"""
    row = await email_verify.get(session, body.email)
    if not email_verify.can_register(row, email_verify.now()):
        raise HTTPException(status_code=400, detail="이메일 인증을 먼저 해 주세요.")
    try:
        user = await users.create(session, body.email, body.password, nickname=body.nickname)
    except users.EmailTaken as e:
        raise HTTPException(status_code=409, detail="이미 가입한 이메일입니다.") from e
    await email_verify.delete(session, body.email)
    return UserRead.model_validate(user)


@router.post("/token", summary="로그인")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], request: Request, session: Session
) -> Token:
    """OAuth2 표준 폼(username, password). username 칸에 이메일을 넣는다.
    같은 IP에서 같은 이메일로 15분 안에 5번 틀리면 잠시 막는다(429)"""
    key = login_limit.key(request.client.host if request.client else None, form.username)
    wait = login_limit.limiter.retry_after(key)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"로그인을 여러 번 실패했습니다. {wait // 60 + 1}분 뒤에 다시 시도해 주세요.",
            headers={"Retry-After": str(wait)},
        )
    user = await users.authenticate(session, form.username, form.password)
    if user is None:
        login_limit.limiter.fail(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 맞지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    login_limit.limiter.reset(key)
    return _token(user)


@router.get("/me", summary="내 정보")
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", summary="닉네임 바꾸기")
async def update_me(body: ProfileUpdate, user: CurrentUser, session: Session) -> UserRead:
    """내 닉네임을 바꾼다"""
    user = await users.set_nickname(session, user, body.nickname)
    return UserRead.model_validate(user)


@router.post("/password", summary="비밀번호 바꾸기")
async def change_password(body: PasswordChange, user: CurrentUser, session: Session) -> Token:
    """지금 비밀번호를 확인하고 새 비밀번호로 바꾼다.
    다른 기기에 남은 예전 토큰은 끊기고, 이 요청에는 새 토큰을 돌려준다"""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="지금 비밀번호가 맞지 않습니다.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="새 비밀번호가 지금 비밀번호와 같습니다.")
    user = await users.change_password(session, user, body.new_password)
    return _token(user)
