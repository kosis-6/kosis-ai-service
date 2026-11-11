"""로그인: 해시·토큰 함수, 회원가입(이메일 인증·비밀번호 규칙), /auth API, 선택 로그인,
시도 횟수 제한, 관리자 사용자 관리, Refresh Token·로그아웃
(DB 대신 services.users와 email_verify·auth_sessions의 저장 함수를 바꿔 끼우고,
메일은 보내지 않고 모은다)
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_optional_user, require_admin
from app.api.routes import auth as auth_routes
from app.core import login_limit, security
from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.schemas.auth import nickname_problem, password_problem
from app.services import auth_sessions, email_verify, mailer, users

SECRET = "test-secret-key-that-is-long-enough-0123456789"


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret_key", SECRET)
    # 시도 횟수 기록이 테스트끼리 섞이지 않게 매번 새로 만든다
    monkeypatch.setattr(login_limit, "limiter", login_limit.LoginLimiter())
    monkeypatch.setattr(
        auth_routes, "code_sends", login_limit.LoginLimiter(max_failures=10, window=3600)
    )
    monkeypatch.setattr(get_settings(), "mail_backend", "console")


class Clock:
    """email_verify.now 대신 쓰는 시계. clock.at += timedelta(...)로 시간을 보낸다"""

    def __init__(self) -> None:
        self.at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(email_verify, "now", c)
    monkeypatch.setattr(auth_sessions, "now", c)
    return c


REAL_SEND = mailer.send
# 보낸 메일 대신 쌓아 두는 곳 (테스트마다 비운다)
SENT: list[dict[str, str]] = []


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    SENT.clear()

    async def send(to, subject, body):
        SENT.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(mailer, "send", send)
    return SENT


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> dict:
    """이메일 → 인증번호 줄. email_verify의 저장 함수만 바꾸고 판단 함수는 그대로 쓴다"""
    rows: dict = {}

    async def save(session, row):
        rows[row.email.lower()] = row

    async def delete(session, email):
        rows.pop(email.lower(), None)

    async def use_attempt(session, email):
        row = rows.get(email.lower())
        if row is None or row.attempts >= email_verify.MAX_ATTEMPTS:
            return None
        row.attempts += 1
        return row.attempts

    monkeypatch.setattr(email_verify, "get", lambda session, e: _async(rows.get(e.lower())))
    monkeypatch.setattr(email_verify, "save", save)
    monkeypatch.setattr(email_verify, "use_attempt", use_attempt)
    monkeypatch.setattr(email_verify, "delete", delete)
    return rows


@pytest.fixture
def logins(monkeypatch: pytest.MonkeyPatch) -> dict[int, SimpleNamespace]:
    """로그인 기록(app.user_sessions) 대신 id → 줄 사전. 판단 함수(rotate 등)는 그대로 쓴다"""
    rows: dict[int, SimpleNamespace] = {}

    async def create(session, user_id, at):
        row = SimpleNamespace(id=len(rows) + 1, user_id=user_id, revoked_at=None)
        jti = auth_sessions.start(row, at)
        rows[row.id] = row
        return row, jti

    async def save(session, row):
        rows[row.id] = row

    async def revoke_all(session, user_id, at):
        for row in rows.values():
            if row.user_id == user_id and row.revoked_at is None:
                row.revoked_at = at

    monkeypatch.setattr(auth_sessions, "get", lambda session, i, lock=False: _async(rows.get(i)))
    monkeypatch.setattr(auth_sessions, "create", create)
    monkeypatch.setattr(auth_sessions, "save", save)
    monkeypatch.setattr(auth_sessions, "revoke_all", revoke_all)
    client.cookies.clear()  # 앞 테스트의 Refresh Token 쿠키가 남지 않게
    return rows


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, codes, outbox, clock, logins) -> dict[int, SimpleNamespace]:
    """이메일 → 사용자 대신 id → 사용자 사전. users 모듈 함수만 바꾸고 라우터는 그대로 쓴다"""
    rows: dict[int, SimpleNamespace] = {}

    async def create(session, email, password, nickname=None, is_admin=False):
        email = email.lower()
        if any(u.email == email for u in rows.values()):
            raise users.EmailTaken(email)
        user = SimpleNamespace(
            id=len(rows) + 1,
            email=email,
            nickname=nickname,
            password_hash=security.hash_password(password),
            is_active=True,
            is_admin=is_admin,
            token_version=0,
            created_at=datetime.now(UTC),
        )
        rows[user.id] = user
        return user

    async def get_by_email(session, email):
        return next((u for u in rows.values() if u.email == email.lower()), None)

    async def change_password(session, user, new_password):
        user.password_hash = security.hash_password(new_password)
        user.token_version += 1
        return user

    async def set_nickname(session, user, nickname):
        user.nickname = nickname
        return user

    async def list_users(session, limit, offset):
        ordered = sorted(rows.values(), key=lambda u: u.id)
        return ordered[offset : offset + limit], len(ordered)

    async def update(session, user, is_active=None, is_admin=None):
        if is_active is not None:
            if user.is_active and not is_active:
                user.token_version += 1
            user.is_active = is_active
        if is_admin is not None:
            user.is_admin = is_admin
        return user

    monkeypatch.setattr(users, "create", create)
    monkeypatch.setattr(users, "get_by_email", get_by_email)
    monkeypatch.setattr(users, "change_password", change_password)
    monkeypatch.setattr(users, "set_nickname", set_nickname)
    monkeypatch.setattr(users, "list_users", list_users)
    monkeypatch.setattr(users, "update", update)
    monkeypatch.setattr(users, "get", lambda session, i: _async(rows.get(i)))
    app.dependency_overrides[get_session] = lambda: None
    yield rows
    app.dependency_overrides.clear()


async def _async(v):
    return v


client = TestClient(app)


def _login(email: str, password: str):
    return client.post("/auth/token", data={"username": email, "password": password})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


PW = "pw1234!"
NICK = "통계왕"


def _sent_code() -> str:
    """마지막으로 보낸 메일의 인증번호"""
    return SENT[-1]["body"].split("인증번호: ")[1][:6]


def _verify(email: str) -> None:
    """인증번호를 받고 확인까지 한다"""
    assert client.post("/auth/email-code", json={"email": email}).status_code == 202
    code = _sent_code()
    r = client.post("/auth/email-code/verify", json={"email": email, "code": code})
    assert r.status_code == 200


def _register(email: str, password: str = PW):
    _verify(email)
    body = {"email": email, "password": password, "nickname": NICK}
    return client.post("/auth/register", json=body)


def _signup(email: str, password: str = PW) -> str:
    """이메일 인증 → 가입 → 로그인해서 토큰을 돌려준다"""
    assert _register(email, password).status_code == 201
    return _login(email, password).json()["access_token"]


def test_password_hash_roundtrip() -> None:
    h = security.hash_password("correct horse")
    assert h.startswith("$argon2")
    assert security.verify_password("correct horse", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("anything", None)  # 없는 사용자


def test_token_roundtrip_and_rejects_bad_tokens() -> None:
    token = security.create_access_token(7, 2, "jti-1", version=3)
    assert security.decode_access_token(token) == security.AccessClaims(7, 3, 2, "jti-1")
    assert security.decode_access_token(token + "x") is None
    expired = security.create_access_token(7, 2, "jti-1", minutes=-1)
    assert security.decode_access_token(expired) is None


def test_short_secret_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret_key", "short")
    with pytest.raises(security.AuthNotConfigured):
        security.create_access_token(1, 1, "jti")


def test_register_login_me(db) -> None:
    r = _register("User@Example.com")
    assert r.status_code == 201
    assert r.json()["email"] == "user@example.com"
    assert r.json()["is_admin"] is False
    assert r.json()["nickname"] == NICK
    assert "password_hash" not in r.json()

    # 이미 가입한 이메일은 인증번호부터 받을 수 없다
    again = client.post("/auth/email-code", json={"email": "user@example.com"})
    assert again.status_code == 409

    assert _login("user@example.com", "wrong-pass").status_code == 401
    token = _login("USER@example.com", PW).json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


def test_register_validates_input(db) -> None:
    bad = [
        {"email": "no-at", "password": PW, "nickname": NICK},
        {"email": "a@b.co", "password": "a1!", "nickname": NICK},  # 6자 미만
        {"email": "a@b.co", "password": "abcdef!", "nickname": NICK},  # 숫자 없음
        {"email": "a@b.co", "password": "abc123", "nickname": NICK},  # 특수기호 없음
        {"email": "a@b.co", "password": "1234!!", "nickname": NICK},  # 영문 없음
        {"email": "a@b.co", "password": PW},  # 닉네임 없음
        {"email": "a@b.co", "password": PW, "nickname": " 가 "},  # 공백 빼면 1자
        {"email": "a@b.co", "password": PW, "nickname": "가" * 21},
        {"email": "a@b.co", "password": PW, "nickname": "통계\n왕"},
    ]
    for body in bad:
        assert client.post("/auth/register", json=body).status_code == 422
    r = client.post(
        "/auth/register", json={"email": "a@b.co", "password": "abc123", "nickname": NICK}
    )
    assert r.json()["detail"][0]["msg"].startswith("비밀번호에 특수기호를 넣어 주세요.")


def test_me_requires_valid_active_user(db) -> None:
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401

    _register("a@b.co")
    token = _login("a@b.co", PW).json()["access_token"]
    db[1].is_active = False
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _login("a@b.co", PW).status_code == 401


def test_login_without_secret_is_503(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _register("a@b.co")
    monkeypatch.setattr(get_settings(), "jwt_secret_key", "")
    r = _login("a@b.co", PW)
    assert r.status_code == 503
    assert "JWT_SECRET_KEY" in r.json()["detail"]


def test_require_admin() -> None:
    admin = SimpleNamespace(is_admin=True)
    assert asyncio.run(require_admin(admin)) is admin
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_admin(SimpleNamespace(is_admin=False)))
    assert e.value.status_code == 403


def test_token_without_login_record_is_rejected() -> None:
    """Refresh Token을 넣기 전에 발급한 토큰(sid, jti 없음)은 받지 않는다. 다시 로그인하면 된다"""
    import jwt

    exp = datetime.now(UTC).timestamp() + 60
    old = jwt.encode({"sub": "7", "ver": 0, "exp": exp}, SECRET, "HS256")
    assert security.decode_access_token(old) is None


def test_change_password_cuts_old_tokens(db) -> None:
    other_device = _signup("a@b.co")
    old = _login("a@b.co", PW).json()["access_token"]
    body = {"current_password": PW, "new_password": "new-pass-1"}

    wrong = client.post(
        "/auth/password", json={**body, "current_password": "nope1234"}, headers=_auth(old)
    )
    assert wrong.status_code == 400
    same = client.post("/auth/password", json={**body, "new_password": PW}, headers=_auth(old))
    assert same.status_code == 400
    assert client.post("/auth/password", json=body).status_code == 401  # 로그인 필요

    r = client.post("/auth/password", json=body, headers=_auth(old))
    assert r.status_code == 200
    new = r.json()["access_token"]
    assert client.get("/auth/me", headers=_auth(old)).status_code == 401  # 예전 토큰은 끊김
    assert client.get("/auth/me", headers=_auth(other_device)).status_code == 401  # 다른 기기도
    assert client.get("/auth/me", headers=_auth(new)).status_code == 200
    assert client.post("/auth/refresh").status_code == 200  # 새로 받은 쿠키로 계속 쓸 수 있다
    assert _login("a@b.co", PW).status_code == 401
    assert _login("a@b.co", "new-pass-1").status_code == 200


def test_login_is_blocked_after_repeated_failures(db) -> None:
    _register("a@b.co")
    for _ in range(login_limit.MAX_FAILURES):
        assert _login("a@b.co", "wrong-pass").status_code == 401
    blocked = _login("a@b.co", PW)  # 맞는 비밀번호도 잠시 막힌다
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    # 다른 이메일은 막히지 않는다 (남의 이메일로 일부러 틀려서 막는 것과 구분)
    assert _login("other@b.co", PW).status_code == 401


def test_login_success_resets_failures(db) -> None:
    _register("a@b.co")
    for _ in range(login_limit.MAX_FAILURES - 1):
        _login("a@b.co", "wrong-pass")
    assert _login("a@b.co", PW).status_code == 200
    for _ in range(login_limit.MAX_FAILURES - 1):
        _login("a@b.co", "wrong-pass")
    assert _login("a@b.co", PW).status_code == 200


def test_login_limiter_forgets_after_window() -> None:
    now = [0.0]
    lim = login_limit.LoginLimiter(max_failures=2, window=60, clock=lambda: now[0])
    key = login_limit.key("1.2.3.4", " A@B.co ")
    assert key == "1.2.3.4|a@b.co"
    lim.fail(key)
    lim.fail(key)
    assert lim.retry_after(key) == 61
    now[0] = 30
    assert lim.retry_after(key) == 31
    now[0] = 60.5
    assert lim.retry_after(key) == 0


def test_optional_user(db) -> None:
    """선택 로그인: 토큰 없으면 손님(None), 틀린 토큰은 401, 맞으면 사용자"""
    token = _signup("a@b.co")
    assert asyncio.run(get_optional_user(None, None)) is None
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_optional_user("nope", None))
    assert e.value.status_code == 401
    assert asyncio.run(get_optional_user(token, None)).email == "a@b.co"


def test_admin_lists_and_updates_users(db, logins) -> None:
    admin_token = _signup("admin@b.co")
    db[1].is_admin = True
    user_token = _signup("user@b.co")

    assert client.get("/admin/users", headers=_auth(user_token)).status_code == 403
    r = client.get("/admin/users", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert [u["email"] for u in r.json()["users"]] == ["admin@b.co", "user@b.co"]
    assert r.json()["users"][1]["is_active"] is True

    # 정지하면 그 사용자의 토큰이 끊기고, 다시 풀어도 정지 전 토큰은 살아나지 않는다
    stop = client.patch("/admin/users/2", json={"is_active": False}, headers=_auth(admin_token))
    assert stop.status_code == 200 and stop.json()["is_active"] is False
    assert client.get("/auth/me", headers=_auth(user_token)).status_code == 401
    client.patch("/admin/users/2", json={"is_active": True}, headers=_auth(admin_token))
    assert client.get("/auth/me", headers=_auth(user_token)).status_code == 401
    assert _login("user@b.co", PW).status_code == 200
    assert logins[2].revoked_at is not None  # 정지 전 로그인 줄은 끊김

    promote = client.patch("/admin/users/2", json={"is_admin": True}, headers=_auth(admin_token))
    assert promote.json()["is_admin"] is True
    assert (
        client.patch(
            "/admin/users/99", json={"is_active": False}, headers=_auth(admin_token)
        ).status_code
        == 404
    )


def test_admin_cannot_lock_out_self(db) -> None:
    token = _signup("admin@b.co")
    db[1].is_admin = True
    for body in ({"is_active": False}, {"is_admin": False}):
        assert client.patch("/admin/users/1", json=body, headers=_auth(token)).status_code == 400
    assert client.get("/auth/me", headers=_auth(token)).status_code == 200


def test_password_rule() -> None:
    assert password_problem("abc12!") is None
    assert password_problem("ABC12!") is None  # 대소문자 상관없음
    assert password_problem("Ab1~ 가나") is None
    assert "6자 이상" in password_problem("a1!")
    assert password_problem("a1!" + "x" * 17) is None  # 20자까지
    assert "20자 이하" in password_problem("a1!" + "x" * 18)
    assert "숫자·특수기호" in password_problem("abcdefg")
    assert password_problem("가나다12!").startswith("비밀번호에 영문을")  # 한글은 영문이 아니다
    assert "특수기호" in password_problem("abc 123")  # 공백은 특수기호가 아니다


def test_register_requires_email_verification(db, codes) -> None:
    body = {"email": "a@b.co", "password": PW, "nickname": NICK}
    assert client.post("/auth/register", json=body).status_code == 400  # 인증 전

    sent = client.post("/auth/email-code", json={"email": "A@b.co"})
    assert sent.status_code == 202
    assert sent.json() == {"expires_in": 600, "resend_after": 60}
    assert SENT[-1]["to"] == "a@b.co"
    code = _sent_code()
    assert codes["a@b.co"].code_hash != code  # 번호를 그대로 두지 않는다
    assert client.post("/auth/register", json=body).status_code == 400  # 받기만 하고 확인 전

    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": wrong})
    assert r.status_code == 400 and "4번 더" in r.json()["detail"]
    ok = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": code})
    assert ok.status_code == 200 and ok.json() == {"expires_in": 1800}

    assert client.post("/auth/register", json=body).status_code == 201
    assert "a@b.co" not in codes  # 가입하면 인증 기록을 지운다
    assert _login("a@b.co", PW).status_code == 200


def test_email_code_resend_wait_and_old_code(db, clock) -> None:
    client.post("/auth/email-code", json={"email": "a@b.co"})
    first = _sent_code()
    r = client.post("/auth/email-code", json={"email": "a@b.co"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"

    clock.at += timedelta(seconds=61)
    assert client.post("/auth/email-code", json={"email": "a@b.co"}).status_code == 202
    second = _sent_code()
    if first != second:  # 다시 받으면 전에 받은 번호는 못 쓴다
        r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": first})
        assert r.status_code == 400
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": second})
    assert r.status_code == 200


def test_email_code_expires_and_locks(db, clock) -> None:
    client.post("/auth/email-code", json={"email": "a@b.co"})
    code = _sent_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(email_verify.MAX_ATTEMPTS):
        client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": wrong})
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": code})
    assert r.status_code == 400 and "다시 받아" in r.json()["detail"]  # 맞는 번호도 막힌다

    clock.at += timedelta(seconds=61)
    client.post("/auth/email-code", json={"email": "a@b.co"})
    clock.at += timedelta(minutes=email_verify.CODE_MINUTES)
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": _sent_code()})
    assert r.status_code == 400 and "유효 시간" in r.json()["detail"]
    r = client.post("/auth/email-code/verify", json={"email": "x@b.co", "code": "123456"})
    assert r.status_code == 400 and "먼저 받아" in r.json()["detail"]


def test_verified_email_must_register_in_time(db, clock) -> None:
    _verify("a@b.co")
    clock.at += timedelta(minutes=email_verify.VERIFIED_MINUTES)
    r = client.post("/auth/register", json={"email": "a@b.co", "password": PW, "nickname": NICK})
    assert r.status_code == 400


def test_email_code_send_limit_per_ip(db, clock) -> None:
    for i in range(10):
        assert client.post("/auth/email-code", json={"email": f"u{i}@b.co"}).status_code == 202
    assert client.post("/auth/email-code", json={"email": "u10@b.co"}).status_code == 429


def test_email_code_without_mail_settings_is_503(db, codes, monkeypatch) -> None:
    monkeypatch.setattr(mailer, "send", REAL_SEND)  # 가짜 메일함 대신 진짜 send
    monkeypatch.setattr(get_settings(), "mail_backend", "smtp")
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    r = client.post("/auth/email-code", json={"email": "a@b.co"})
    assert r.status_code == 503 and "SMTP_HOST" in r.json()["detail"]
    assert "a@b.co" not in codes  # 못 보냈으면 바로 다시 요청할 수 있게 지운다


def test_nickname_rule_and_change(db) -> None:
    assert nickname_problem("가나") is None
    assert nickname_problem("a" * 20) is None
    assert "2~20자" in nickname_problem("가")
    assert "줄바꿈" in nickname_problem("통계\t왕")

    # 앞뒤 공백은 빼고 저장한다. 같은 닉네임도 가입할 수 있다
    _verify("a@b.co")
    body = {"email": "a@b.co", "password": PW, "nickname": "  통계왕 "}
    assert client.post("/auth/register", json=body).json()["nickname"] == "통계왕"
    assert _register("c@d.co").status_code == 201

    token = _login("a@b.co", PW).json()["access_token"]
    assert client.patch("/auth/me", json={"nickname": "새이름"}).status_code == 401
    bad = client.patch("/auth/me", json={"nickname": "가"}, headers=_auth(token))
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["msg"].startswith("닉네임은 2~20자")
    r = client.patch("/auth/me", json={"nickname": "새이름"}, headers=_auth(token))
    assert r.status_code == 200 and r.json()["nickname"] == "새이름"
    assert client.get("/auth/me", headers=_auth(token)).json()["nickname"] == "새이름"


def _refresh(cookie: str | None = None):
    """cookie를 주면 쿠키 저장소 대신 그 Refresh Token을 보낸다"""
    if cookie is None:
        return client.post("/auth/refresh")
    client.cookies.clear()
    return client.post("/auth/refresh", headers={"Cookie": f"refresh_token={cookie}"})


def test_login_sets_refresh_cookie(db) -> None:
    _register("a@b.co")
    r = _login("a@b.co", PW)
    assert r.status_code == 200
    assert r.json()["expires_in"] == 15 * 60
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("refresh_token=1.0.")
    assert "Secure" not in cookie  # http 요청이면 Secure를 붙이지 않는다 (로컬 개발)
    for part in ("HttpOnly", "Path=/auth", "SameSite=strict", "Max-Age=1209600"):
        assert part in cookie
    assert "refresh_token" not in r.json()  # 본문에는 Access Token만


def test_refresh_rotates_and_cuts_old_access_token(db, logins, clock) -> None:
    old_access = _signup("a@b.co")
    old_refresh = client.cookies["refresh_token"]

    r = _refresh()
    assert r.status_code == 200
    new_access = r.json()["access_token"]
    new_refresh = client.cookies["refresh_token"]
    assert new_refresh != old_refresh
    assert client.get("/auth/me", headers=_auth(old_access)).status_code == 401  # 예전 토큰 막힘
    assert client.get("/auth/me", headers=_auth(new_access)).status_code == 200
    assert old_refresh.split(".")[2] not in str(vars(logins[1]))  # 토큰은 두지 않는다
    assert new_refresh.split(".")[2] not in str(vars(logins[1]))

    # 이미 쓴 Refresh Token이 (동시 요청으로 볼 시간이 지나) 다시 오면
    # 훔쳐 간 것으로 보고 그 로그인을 통째로 끊는다
    clock.at += timedelta(seconds=auth_sessions.REUSE_GRACE_SECONDS)
    reused = _refresh(old_refresh)
    assert reused.status_code == 401
    assert 'refresh_token=""' in reused.headers["set-cookie"]  # 쿠키도 지운다
    assert logins[1].revoked_at is not None
    assert client.get("/auth/me", headers=_auth(new_access)).status_code == 401
    assert _refresh(new_refresh).status_code == 401


def test_refresh_fails_without_valid_cookie(db, clock) -> None:
    assert _refresh().status_code == 401  # 쿠키 없음
    assert _refresh("garbage").status_code == 401
    assert _refresh("99.0.nope").status_code == 401  # 없는 로그인
    assert _refresh("1.nope").status_code == 401  # 이 변경 전 모양
    assert _refresh(f"{'9' * 30}.0.x").status_code == 401  # bigint를 넘는 번호
    access = _signup("a@b.co")
    cookie = client.cookies["refresh_token"]

    clock.at += timedelta(days=15)  # 14일 동안 안 씀
    assert _refresh(cookie).status_code == 401
    assert client.get("/auth/me", headers=_auth(access)).status_code == 401


def test_refresh_extends_while_used_up_to_max(db, clock) -> None:
    _signup("a@b.co")
    for _ in range(2):  # 10일마다 새로 받으면 14일이 지나도 계속 쓸 수 있다
        clock.at += timedelta(days=10)
        assert _refresh().status_code == 200
    # 남은 기간이 14일보다 짧으면 쿠키도 그때까지만 (로그인한 지 30일)
    assert "Max-Age=864000" in client.post("/auth/refresh").headers["set-cookie"]
    clock.at += timedelta(days=10)  # 계속 써도 로그인한 지 30일이 지나면 다시 로그인
    assert _refresh().status_code == 401


def test_refresh_fails_for_stopped_user(db) -> None:
    _signup("a@b.co")
    db[1].is_active = False
    assert _refresh().status_code == 401


def test_logout_cuts_this_login_only(db) -> None:
    phone = _signup("a@b.co")
    phone_cookie = client.cookies["refresh_token"]
    laptop = _login("a@b.co", PW).json()["access_token"]
    laptop_cookie = client.cookies["refresh_token"]

    client.cookies.clear()
    r = client.post("/auth/logout", headers=_auth(phone))
    assert r.status_code == 204
    assert client.get("/auth/me", headers=_auth(phone)).status_code == 401
    assert _refresh(phone_cookie).status_code == 401
    assert client.get("/auth/me", headers=_auth(laptop)).status_code == 200  # 다른 기기는 그대로

    # Access Token이 만료돼도 쿠키만으로 로그아웃할 수 있다
    client.cookies.clear()
    client.cookies.set("refresh_token", laptop_cookie)
    r = client.post("/auth/logout")
    assert r.status_code == 204 and 'refresh_token=""' in r.headers["set-cookie"]
    assert client.get("/auth/me", headers=_auth(laptop)).status_code == 401

    assert client.post("/auth/logout").status_code == 204  # 이미 로그아웃해도 204


def test_wrong_refresh_secret_does_not_cut_login(db, logins) -> None:
    """남의 로그인 줄 번호에 아무 값이나 붙여 보내도 그 로그인은 끊기지 않는다 (번호는 짐작 가능).
    지난 세대·지금 세대·앞으로 올 세대 번호를 붙여도 서명이 틀리면 마찬가지"""
    _signup("a@b.co")
    r = _refresh()  # 1세대
    access, cookie = r.json()["access_token"], client.cookies["refresh_token"]
    for guess in ("1.0.guessed-value", "1.1.guessed-value", "1.5.guessed-value"):
        assert _refresh(guess).status_code == 401
    assert logins[1].revoked_at is None
    assert client.get("/auth/me", headers=_auth(access)).status_code == 200
    assert _refresh(cookie).status_code == 200


def test_email_code_attempts_are_counted_in_db(db, codes) -> None:
    """동시에 보낸 요청이 모두 '아직 0번 틀림'을 읽어도 확인은 DB에서 센 5번까지만 한다"""
    client.post("/auth/email-code", json={"email": "a@b.co"})
    code = _sent_code()
    stale = SimpleNamespace(**vars(codes["a@b.co"]))  # 동시 요청이 먼저 읽어 둔 줄 (0번)
    codes["a@b.co"].attempts = email_verify.MAX_ATTEMPTS  # 그사이 다른 요청들이 5번을 씀
    real = codes["a@b.co"]
    email_verify.get = lambda session, e: _async(stale)  # monkeypatch가 테스트 뒤 되돌린다
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": code})
    assert r.status_code == 400 and "다시 받아" in r.json()["detail"]  # 맞는 번호여도 막힌다
    assert real.verified_at is None


def test_refresh_twice_at_once_keeps_login(db, logins, clock) -> None:
    """탭 두 개가 같은 쿠키로 동시에 새 토큰을 받으면 뒤 요청도 끊기지 않는다"""
    _signup("a@b.co")
    old_refresh = client.cookies["refresh_token"]
    first = _refresh(old_refresh)
    assert first.status_code == 200
    new_refresh = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]

    second = _refresh(old_refresh)  # 앞 요청이 바꾼 직후 예전 쿠키로 온 요청
    assert second.status_code == 200
    assert "set-cookie" not in second.headers  # 앞 요청이 준 새 쿠키를 그대로 쓴다
    assert logins[1].revoked_at is None
    for r in (first, second):  # 두 탭의 Access Token 모두 쓸 수 있다
        assert client.get("/auth/me", headers=_auth(r.json()["access_token"])).status_code == 200
    assert _refresh(new_refresh).status_code == 200


def test_logout_with_old_access_token_still_cuts_login(db, logins) -> None:
    """다른 탭이 새 토큰을 받아 지난 번호가 된 Access Token으로 로그아웃해도 쿠키로 끊는다"""
    old_access = _signup("a@b.co")
    assert _refresh().status_code == 200
    cookie = client.cookies["refresh_token"]
    r = client.post("/auth/logout", headers=_auth(old_access))
    assert r.status_code == 204
    assert logins[1].revoked_at is not None
    assert _refresh(cookie).status_code == 401


def test_stolen_refresh_token_is_caught_after_many_rotations(db, logins, clock) -> None:
    """훔친 사람이 여러 번 새로 받은 뒤 진짜 사용자가 예전 쿠키로 돌아와도 그 로그인을 끊는다
    (바로 전 값만 보면 '틀린 값'이 되어 훔친 사람의 로그인이 계속 살아 있었다)"""
    _signup("a@b.co")
    victim_cookie = client.cookies["refresh_token"]
    attacker = _refresh(victim_cookie)  # 훔친 쿠키로 먼저 새로 받는다
    for _ in range(3):
        clock.at += timedelta(minutes=15)
        attacker = client.post("/auth/refresh")
        assert attacker.status_code == 200
    attacker_access = attacker.json()["access_token"]

    clock.at += timedelta(minutes=1)
    assert _refresh(victim_cookie).status_code == 401  # 진짜 사용자가 돌아옴
    assert logins[1].revoked_at is not None  # 탈취로 보고 끊는다
    assert client.get("/auth/me", headers=_auth(attacker_access)).status_code == 401
    client.cookies.clear()
    assert (
        _refresh(attacker.headers["set-cookie"].split(";")[0].split("=", 1)[1]).status_code == 401
    )


def test_refresh_token_signature_is_tied_to_login(db, logins) -> None:
    """다른 로그인의 서명을 옮겨 붙이거나 세대를 바꾸면 틀린 값 (끊지 않는다)"""
    _signup("a@b.co")
    first = client.cookies["refresh_token"]
    _login("a@b.co", PW)
    second = client.cookies["refresh_token"]
    sid1, _, mac1 = first.split(".")
    sid2, _, mac2 = second.split(".")
    assert mac1 != mac2
    assert _refresh(f"{sid2}.0.{mac1}").status_code == 401
    assert _refresh(f"{sid1}.1.{mac1}").status_code == 401
    assert logins[1].revoked_at is None and logins[2].revoked_at is None
    assert _refresh(first).status_code == 200


def test_refresh_cookie_secure_follows_https(db, monkeypatch) -> None:
    """COOKIE_SECURE를 비워 두면 https 요청일 때만 Secure. true/false로 강제할 수 있다"""
    _register("a@b.co")
    https = TestClient(app, base_url="https://testserver")
    r = https.post("/auth/token", data={"username": "a@b.co", "password": PW})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
    assert "Secure" in https.post("/auth/logout").headers["set-cookie"]  # 지울 때도 같게

    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    assert "Secure" in _login("a@b.co", PW).headers["set-cookie"]  # http여도 강제
    monkeypatch.setattr(get_settings(), "cookie_secure", False)
    assert (
        "Secure"
        not in https.post("/auth/token", data={"username": "a@b.co", "password": PW}).headers[
            "set-cookie"
        ]
    )
