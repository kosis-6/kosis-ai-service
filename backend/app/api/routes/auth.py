"""회원가입(이메일 인증)·로그인·내 정보·비밀번호 변경

회원가입 순서: POST /auth/email-code → 메일로 받은 6자리를 POST /auth/email-code/verify
→ 30분 안에 POST /auth/register. 아이디는 이메일이다.

토큰: 로그인하면 Access Token(15분)은 응답 본문으로, Refresh Token(14일)은 HttpOnly 쿠키로 준다.
Access Token이 만료되면(401) POST /auth/refresh로 둘 다 새로 받는다. 서버는 로그인마다
app.user_sessions에 Refresh Token 해시와 지금 쓸 수 있는 Access Token 번호를 두어,
새 토큰을 받거나 로그아웃하면 예전 Access Token을 바로 막는다 (services/auth_sessions.py).
비밀번호를 바꾸거나 관리자가 계정을 정지하면 그 사용자의 로그인이 모두 끊긴다.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, Session, optional_scheme
from app.core import login_limit
from app.core.config import get_settings
from app.core.security import create_access_token, decode_access_token, verify_password
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
from app.services import auth_sessions, email_verify, mailer, users

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


def _refresh_failed() -> JSONResponse:
    """401과 함께 쓸 수 없는 Refresh Token 쿠키를 지운다
    (HTTPException으로 내면 지우라는 쿠키가 응답에 실리지 않는다)"""
    r = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "다시 로그인해 주세요."}
    )
    _forget_cookie(r)
    return r


def _issue(response: Response, user: User, row, secret: str, jti: str) -> Token:
    """Access Token은 본문으로, Refresh Token은 HttpOnly 쿠키로 준다"""
    s = get_settings()
    response.set_cookie(
        auth_sessions.COOKIE,
        auth_sessions.refresh_token(row, secret),
        max_age=s.refresh_token_days * 24 * 60 * 60,
        path=auth_sessions.COOKIE_PATH,
        httponly=True,  # 화면 JavaScript가 읽을 수 없다 (XSS로 훔쳐 가지 못하게)
        secure=s.cookie_secure,
        samesite="strict",  # 다른 사이트에서 보낸 요청에는 실리지 않는다
    )
    return Token(
        access_token=create_access_token(user.id, row.id, jti, user.token_version),
        expires_in=s.access_token_minutes * 60,
    )


async def _new_login(response: Response, session, user: User) -> Token:
    row, secret, jti = await auth_sessions.create(session, user.id, auth_sessions.now())
    return _issue(response, user, row, secret, jti)


def _forget_cookie(response: Response) -> None:
    response.delete_cookie(auth_sessions.COOKIE, path=auth_sessions.COOKIE_PATH)


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
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    response: Response,
    session: Session,
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
    return await _new_login(response, session, user)


@router.post(
    "/refresh",
    summary="토큰 새로 받기",
    response_model=Token,
    responses={401: {"description": "다시 로그인해야 함"}},
)
async def refresh(
    response: Response,
    session: Session,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> Token | JSONResponse:
    """Access Token이 만료됐을 때(401) 부른다. 로그인 때 받은 Refresh Token 쿠키를 브라우저가
    자동으로 보낸다. Access Token·Refresh Token을 둘 다 새로 주고, 예전 Access Token은 바로 끊긴다.
    401이면 다시 로그인해야 한다 (쿠키 없음·만료·로그아웃함·이미 쓴 Refresh Token)"""
    parsed = auth_sessions.parse_refresh(refresh_token)
    if parsed is None:
        return _refresh_failed()
    sid, secret = parsed
    at = auth_sessions.now()
    row = await auth_sessions.get(session, sid)
    result = auth_sessions.check_refresh(row, secret, at)
    if result == "reused":  # 바로 전 Refresh Token: 훔쳐 간 것으로 보고 이 로그인을 끊는다
        row.revoked_at = at
        await auth_sessions.save(session, row)
    user = await users.get(session, row.user_id) if result == "ok" else None
    if user is None or not user.is_active:
        return _refresh_failed()
    new_secret, jti = auth_sessions.rotate(row, at)
    await auth_sessions.save(session, row)
    return _issue(response, user, row, new_secret, jti)


@router.post("/logout", summary="로그아웃", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session: Session,
    token: Annotated[str | None, Depends(optional_scheme)] = None,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> None:
    """이 기기의 로그인을 끊는다. 이 로그인의 Access Token·Refresh Token을 바로 못 쓴다.
    Access Token(Authorization)이나 Refresh Token 쿠키 중 하나만 있어도 된다.
    이미 끊겼거나 토큰이 없어도 204 (화면은 그냥 로그아웃 처리하면 된다)"""
    at = auth_sessions.now()
    claims = decode_access_token(token) if token else None
    parsed = auth_sessions.parse_refresh(refresh_token)
    if claims:
        row = await auth_sessions.get(session, claims.session_id)
        ok = auth_sessions.access_ok(row, claims, at)
    elif parsed:
        row = await auth_sessions.get(session, parsed[0])
        ok = auth_sessions.check_refresh(row, parsed[1], at) == "ok"
    else:
        row, ok = None, False
    if ok:
        row.revoked_at = at
        await auth_sessions.save(session, row)
    _forget_cookie(response)


@router.get("/me", summary="내 정보")
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", summary="닉네임 바꾸기")
async def update_me(body: ProfileUpdate, user: CurrentUser, session: Session) -> UserRead:
    """내 닉네임을 바꾼다"""
    user = await users.set_nickname(session, user, body.nickname)
    return UserRead.model_validate(user)


@router.post("/password", summary="비밀번호 바꾸기")
async def change_password(
    body: PasswordChange, user: CurrentUser, response: Response, session: Session
) -> Token:
    """지금 비밀번호를 확인하고 새 비밀번호로 바꾼다.
    이 기기를 포함한 모든 로그인이 끊기고, 이 요청에는 새 로그인 토큰을 돌려준다"""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="지금 비밀번호가 맞지 않습니다.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="새 비밀번호가 지금 비밀번호와 같습니다.")
    user = await users.change_password(session, user, body.new_password)
    await auth_sessions.revoke_all(session, user.id, auth_sessions.now())
    return await _new_login(response, session, user)
