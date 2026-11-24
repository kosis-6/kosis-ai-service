import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import (
    CurrentUser,
    Session,
    admin_ip_allowed,
    oauth2_scheme,
    optional_scheme,
)
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["로그인·회원가입"])

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


def _refresh_failed(request: Request) -> JSONResponse:
    r = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "다시 로그인해 주세요."}
    )
    _forget_cookie(r, request)
    return r


def _secure(request: Request) -> bool:
    forced = get_settings().cookie_secure
    return forced if forced is not None else request.url.scheme == "https"


def _issue(request: Request, response: Response, user: User, row, jti: str) -> Token:
    s = get_settings()
    left = (row.expires_at - auth_sessions.now()).total_seconds()
    response.set_cookie(
        auth_sessions.COOKIE,
        auth_sessions.refresh_token(row),
        max_age=max(0, int(left)) if row.persistent else None,
        path=auth_sessions.COOKIE_PATH,
        httponly=True,
        secure=_secure(request),
        samesite="strict",
    )
    return Token(
        access_token=create_access_token(user.id, row.id, jti, user.token_version),
        expires_in=s.access_token_minutes * 60,
    )


async def _new_login(
    request: Request, response: Response, session, user: User, persistent: bool = True
) -> Token:
    row, jti = await auth_sessions.create(session, user.id, auth_sessions.now(), persistent)
    return _issue(request, response, user, row, jti)


def _forget_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        auth_sessions.COOKIE,
        path=auth_sessions.COOKIE_PATH,
        httponly=True,
        secure=_secure(request),
        samesite="strict",
    )


@router.post("/email-code", summary="① 인증번호 받기", status_code=status.HTTP_202_ACCEPTED)
async def send_email_code(
    body: EmailCodeRequest, request: Request, session: Session
) -> EmailCodeSent:
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
    code_sends.fail(ip)
    subject, text, html = email_verify.code_mail(
        code, get_settings().mail_from_name or "LIKE SNAKE"
    )
    try:
        await mailer.send(body.email, subject, text, html)
    except mailer.MailNotConfigured:
        await email_verify.delete(session, body.email)
        raise
    except Exception as e:
        logger.warning("인증 메일을 보내지 못함: %s: %s", type(e).__name__, e)
        await email_verify.delete(session, body.email)
        raise HTTPException(
            status_code=502, detail="인증 메일을 보내지 못했습니다. 잠시 뒤에 다시 시도해 주세요."
        ) from e
    return EmailCodeSent(
        expires_in=email_verify.CODE_MINUTES * 60, resend_after=email_verify.RESEND_SECONDS
    )


@router.post("/email-code/verify", summary="② 인증번호 확인")
async def verify_email_code(body: EmailCodeVerify, session: Session) -> EmailVerified:
    row = await email_verify.get(session, body.email)
    at = email_verify.now()
    result = email_verify.check(row, body.code, at)
    if result in ("ok", "wrong"):
        used = await email_verify.use_attempt(session, body.email)
        if used is None:
            raise HTTPException(status_code=400, detail=CODE_ERRORS["too_many"])
    if result == "wrong":
        left = email_verify.MAX_ATTEMPTS - used
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
    keep: Annotated[
        str, Form(description="로그인 상태 유지. false면 브라우저를 닫을 때 끝난다")
    ] = "true",
) -> Token:
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
    persistent = keep.strip().lower() not in ("false", "0", "off", "no")
    return await _new_login(request, response, session, user, persistent=persistent)


@router.post(
    "/refresh",
    summary="토큰 새로 받기",
    response_model=Token,
    responses={401: {"description": "다시 로그인해야 함"}},
)
async def refresh(
    request: Request,
    response: Response,
    session: Session,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> Token | JSONResponse:
    parsed = auth_sessions.parse_refresh(refresh_token)
    if parsed is None:
        return _refresh_failed(request)
    at = auth_sessions.now()
    row = await auth_sessions.get(session, parsed.session_id, lock=True)
    result = auth_sessions.check_refresh(row, parsed, at)
    if result == "reused":
        row.revoked_at = at
        await auth_sessions.save(session, row)
    user = await users.get(session, row.user_id) if result in ("ok", "grace") else None
    if user is None or not user.is_active:
        return _refresh_failed(request)
    if result == "grace":
        return Token(
            access_token=create_access_token(user.id, row.id, row.access_jti, user.token_version),
            expires_in=get_settings().access_token_minutes * 60,
        )
    jti = auth_sessions.rotate(row, at)
    await auth_sessions.save(session, row)
    return _issue(request, response, user, row, jti)


@router.post("/logout", summary="로그아웃", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: Session,
    token: Annotated[str | None, Depends(optional_scheme)] = None,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> None:
    at = auth_sessions.now()
    claims = decode_access_token(token) if token else None
    parsed = auth_sessions.parse_refresh(refresh_token)
    row, ok = None, False
    if claims:
        row = await auth_sessions.get(session, claims.session_id)
        ok = auth_sessions.access_ok(row, claims, at)
    if not ok and parsed:
        row = await auth_sessions.get(session, parsed.session_id)
        ok = auth_sessions.check_refresh(row, parsed, at) in ("ok", "grace")
    if ok:
        row.revoked_at = at
        await auth_sessions.save(session, row)
    _forget_cookie(response, request)


def _me(user: User, request: Request) -> UserRead:
    out = UserRead.model_validate(user)
    if out.is_admin and not admin_ip_allowed(request):
        out.is_admin = False
    return out


@router.get("/me", summary="내 정보")
async def me(user: CurrentUser, request: Request) -> UserRead:
    return _me(user, request)


@router.patch("/me", summary="닉네임 바꾸기")
async def update_me(
    body: ProfileUpdate, user: CurrentUser, session: Session, request: Request
) -> UserRead:
    user = await users.set_nickname(session, user, body.nickname)
    return _me(user, request)


@router.post("/password", summary="비밀번호 바꾸기")
async def change_password(
    body: PasswordChange,
    user: CurrentUser,
    request: Request,
    response: Response,
    session: Session,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> Token:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="지금 비밀번호가 맞지 않습니다.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="새 비밀번호가 지금 비밀번호와 같습니다.")
    claims = decode_access_token(token)
    current = await auth_sessions.get(session, claims.session_id) if claims else None
    user = await users.change_password(session, user, body.new_password)
    await auth_sessions.revoke_all(session, user.id, auth_sessions.now())
    return await _new_login(
        request, response, session, user, persistent=current.persistent if current else True
    )
