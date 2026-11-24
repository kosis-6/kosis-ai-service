import asyncio
import smtplib
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
    monkeypatch.setattr(login_limit, "limiter", login_limit.LoginLimiter())
    monkeypatch.setattr(
        auth_routes, "code_sends", login_limit.LoginLimiter(max_failures=10, window=3600)
    )
    monkeypatch.setattr(get_settings(), "mail_backend", "console")


class Clock:
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
SENT: list[dict[str, str]] = []


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    SENT.clear()

    async def send(to, subject, body, html=None):
        SENT.append({"to": to, "subject": subject, "body": body, "html": html})

    monkeypatch.setattr(mailer, "send", send)
    return SENT


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> dict:
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
    rows: dict[int, SimpleNamespace] = {}

    async def create(session, user_id, at, persistent=True):
        row = SimpleNamespace(
            id=len(rows) + 1, user_id=user_id, revoked_at=None, persistent=persistent
        )
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
    client.cookies.clear()
    return rows


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, codes, outbox, clock, logins) -> dict[int, SimpleNamespace]:
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
    return SENT[-1]["body"].split("인증번호: ")[1][:6]


def _verify(email: str) -> None:
    assert client.post("/auth/email-code", json={"email": email}).status_code == 202
    code = _sent_code()
    r = client.post("/auth/email-code/verify", json={"email": email, "code": code})
    assert r.status_code == 200


def _register(email: str, password: str = PW):
    _verify(email)
    body = {"email": email, "password": password, "nickname": NICK}
    return client.post("/auth/register", json=body)


def _signup(email: str, password: str = PW) -> str:
    assert _register(email, password).status_code == 201
    return _login(email, password).json()["access_token"]


def test_password_hash_roundtrip() -> None:
    h = security.hash_password("correct horse")
    assert h.startswith("$argon2")
    assert security.verify_password("correct horse", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("anything", None)


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
        {"email": "a@b.co", "password": "a1!", "nickname": NICK},
        {"email": "a@b.co", "password": "abcdef!", "nickname": NICK},
        {"email": "a@b.co", "password": "abc123", "nickname": NICK},
        {"email": "a@b.co", "password": "1234!!", "nickname": NICK},
        {"email": "a@b.co", "password": PW},
        {"email": "a@b.co", "password": PW, "nickname": " 가 "},
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


def _from(host: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_require_admin() -> None:
    admin = SimpleNamespace(id=1, is_admin=True)
    assert asyncio.run(require_admin(admin, _from("203.0.113.5"))) is admin
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_admin(SimpleNamespace(id=2, is_admin=False), _from("203.0.113.5")))
    assert e.value.status_code == 403


def test_admin_only_from_allowed_ips(monkeypatch, caplog) -> None:
    monkeypatch.setattr(get_settings(), "admin_allow_ips", "203.0.113.5, 198.51.100.0/24")
    admin = SimpleNamespace(id=1, is_admin=True)
    for host in ("203.0.113.5", "198.51.100.77", "::ffff:198.51.100.8"):
        assert asyncio.run(require_admin(admin, _from(host))) is admin
    for host in ("203.0.113.6", "testclient", ""):
        with caplog.at_level("WARNING"), pytest.raises(HTTPException) as e:
            asyncio.run(require_admin(admin, _from(host)))
        assert e.value.status_code == 403 and "이 곳" in e.value.detail
    assert "203.0.113.6" in caplog.text


def test_admin_menu_hidden_outside_allowed_ips(db, monkeypatch) -> None:
    token = _signup("admin@b.co")
    db[1].is_admin = True
    monkeypatch.setattr(get_settings(), "admin_allow_ips", "198.51.100.0/24")
    inside = TestClient(app, client=("198.51.100.7", 50000))

    assert inside.get("/auth/me", headers=_auth(token)).json()["is_admin"] is True
    assert inside.get("/admin/users", headers=_auth(token)).status_code == 200
    assert client.get("/auth/me", headers=_auth(token)).json()["is_admin"] is False
    assert client.get("/admin/users", headers=_auth(token)).status_code == 403


def test_wrong_admin_ip_setting_stops_server() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="1.2.3"):
        Settings(admin_allow_ips="203.0.113.5,1.2.3")
    assert [str(n) for n in Settings(admin_allow_ips=" 203.0.113.5 ,").admin_networks] == [
        "203.0.113.5/32"
    ]


def test_token_without_login_record_is_rejected() -> None:
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
    assert client.post("/auth/password", json=body).status_code == 401

    r = client.post("/auth/password", json=body, headers=_auth(old))
    assert r.status_code == 200
    new = r.json()["access_token"]
    assert client.get("/auth/me", headers=_auth(old)).status_code == 401
    assert client.get("/auth/me", headers=_auth(other_device)).status_code == 401
    assert client.get("/auth/me", headers=_auth(new)).status_code == 200
    assert client.post("/auth/refresh").status_code == 200
    assert _login("a@b.co", PW).status_code == 401
    assert _login("a@b.co", "new-pass-1").status_code == 200


def test_login_is_blocked_after_repeated_failures(db) -> None:
    _register("a@b.co")
    for _ in range(login_limit.MAX_FAILURES):
        assert _login("a@b.co", "wrong-pass").status_code == 401
    blocked = _login("a@b.co", PW)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
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

    stop = client.patch("/admin/users/2", json={"is_active": False}, headers=_auth(admin_token))
    assert stop.status_code == 200 and stop.json()["is_active"] is False
    assert client.get("/auth/me", headers=_auth(user_token)).status_code == 401
    client.patch("/admin/users/2", json={"is_active": True}, headers=_auth(admin_token))
    assert client.get("/auth/me", headers=_auth(user_token)).status_code == 401
    assert _login("user@b.co", PW).status_code == 200
    assert logins[2].revoked_at is not None

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
    assert password_problem("ABC12!") is None
    assert password_problem("Ab1~ 가나") is None
    assert "6자 이상" in password_problem("a1!")
    assert password_problem("a1!" + "x" * 17) is None
    assert "20자 이하" in password_problem("a1!" + "x" * 18)
    assert "숫자·특수기호" in password_problem("abcdefg")
    assert password_problem("가나다12!").startswith("비밀번호에 영문을")
    assert "특수기호" in password_problem("abc 123")


def test_register_requires_email_verification(db, codes) -> None:
    body = {"email": "a@b.co", "password": PW, "nickname": NICK}
    assert client.post("/auth/register", json=body).status_code == 400

    sent = client.post("/auth/email-code", json={"email": "A@b.co"})
    assert sent.status_code == 202
    assert sent.json() == {"expires_in": 600, "resend_after": 60}
    assert SENT[-1]["to"] == "a@b.co"
    code = _sent_code()
    assert codes["a@b.co"].code_hash != code
    assert client.post("/auth/register", json=body).status_code == 400

    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": wrong})
    assert r.status_code == 400 and "4번 더" in r.json()["detail"]
    ok = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": code})
    assert ok.status_code == 200 and ok.json() == {"expires_in": 1800}

    assert client.post("/auth/register", json=body).status_code == 201
    assert "a@b.co" not in codes
    assert _login("a@b.co", PW).status_code == 200


def test_email_code_resend_wait_and_old_code(db, clock) -> None:
    client.post("/auth/email-code", json={"email": "a@b.co"})
    first = _sent_code()
    r = client.post("/auth/email-code", json={"email": "a@b.co"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"

    clock.at += timedelta(seconds=61)
    assert client.post("/auth/email-code", json={"email": "a@b.co"}).status_code == 202
    second = _sent_code()
    if first != second:
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
    assert r.status_code == 400 and "다시 받아" in r.json()["detail"]

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
    monkeypatch.setattr(mailer, "send", REAL_SEND)
    monkeypatch.setattr(get_settings(), "mail_backend", "smtp")
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    r = client.post("/auth/email-code", json={"email": "a@b.co"})
    assert r.status_code == 503 and "SMTP_HOST" in r.json()["detail"]
    assert "a@b.co" not in codes


def test_email_code_mail_has_code_in_text_and_html(db, outbox) -> None:
    assert client.post("/auth/email-code", json={"email": "a@b.co"}).status_code == 202
    sent = outbox[-1]
    code = _sent_code()
    assert sent["to"] == "a@b.co"
    assert code in sent["subject"] and code in sent["html"]
    assert f"{email_verify.CODE_MINUTES}분" in sent["body"]


def test_email_code_smtp_failure_is_502_and_logged(db, codes, monkeypatch, caplog) -> None:
    async def fail(to, subject, body, html=None):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(mailer, "send", fail)
    with caplog.at_level("WARNING"):
        r = client.post("/auth/email-code", json={"email": "a@b.co"})
    assert r.status_code == 502
    assert "a@b.co" not in codes
    assert "SMTPAuthenticationError" in caplog.text


def test_nickname_rule_and_change(db) -> None:
    assert nickname_problem("가나") is None
    assert nickname_problem("a" * 20) is None
    assert "2~20자" in nickname_problem("가")
    assert "줄바꿈" in nickname_problem("통계\t왕")

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
    assert "Secure" not in cookie
    for part in ("HttpOnly", "Path=/auth", "SameSite=strict", "Max-Age=1209600"):
        assert part in cookie
    assert "refresh_token" not in r.json()


def test_refresh_rotates_and_cuts_old_access_token(db, logins, clock) -> None:
    old_access = _signup("a@b.co")
    old_refresh = client.cookies["refresh_token"]

    r = _refresh()
    assert r.status_code == 200
    new_access = r.json()["access_token"]
    new_refresh = client.cookies["refresh_token"]
    assert new_refresh != old_refresh
    assert client.get("/auth/me", headers=_auth(old_access)).status_code == 401
    assert client.get("/auth/me", headers=_auth(new_access)).status_code == 200
    assert old_refresh.split(".")[2] not in str(vars(logins[1]))
    assert new_refresh.split(".")[2] not in str(vars(logins[1]))

    clock.at += timedelta(seconds=auth_sessions.REUSE_GRACE_SECONDS)
    reused = _refresh(old_refresh)
    assert reused.status_code == 401
    assert 'refresh_token=""' in reused.headers["set-cookie"]
    assert logins[1].revoked_at is not None
    assert client.get("/auth/me", headers=_auth(new_access)).status_code == 401
    assert _refresh(new_refresh).status_code == 401


def test_refresh_fails_without_valid_cookie(db, clock) -> None:
    assert _refresh().status_code == 401
    assert _refresh("garbage").status_code == 401
    assert _refresh("99.0.nope").status_code == 401
    assert _refresh("1.nope").status_code == 401
    assert _refresh(f"{'9' * 30}.0.x").status_code == 401
    access = _signup("a@b.co")
    cookie = client.cookies["refresh_token"]

    clock.at += timedelta(days=15)
    assert _refresh(cookie).status_code == 401
    assert client.get("/auth/me", headers=_auth(access)).status_code == 401


def test_refresh_extends_while_used_up_to_max(db, clock) -> None:
    _signup("a@b.co")
    for _ in range(2):
        clock.at += timedelta(days=10)
        assert _refresh().status_code == 200
    assert "Max-Age=864000" in client.post("/auth/refresh").headers["set-cookie"]
    clock.at += timedelta(days=10)
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
    assert client.get("/auth/me", headers=_auth(laptop)).status_code == 200

    client.cookies.clear()
    client.cookies.set("refresh_token", laptop_cookie)
    r = client.post("/auth/logout")
    assert r.status_code == 204 and 'refresh_token=""' in r.headers["set-cookie"]
    assert client.get("/auth/me", headers=_auth(laptop)).status_code == 401

    assert client.post("/auth/logout").status_code == 204


def test_wrong_refresh_secret_does_not_cut_login(db, logins) -> None:
    _signup("a@b.co")
    r = _refresh()
    access, cookie = r.json()["access_token"], client.cookies["refresh_token"]
    for guess in ("1.0.guessed-value", "1.1.guessed-value", "1.5.guessed-value"):
        assert _refresh(guess).status_code == 401
    assert logins[1].revoked_at is None
    assert client.get("/auth/me", headers=_auth(access)).status_code == 200
    assert _refresh(cookie).status_code == 200


def test_email_code_attempts_are_counted_in_db(db, codes) -> None:
    client.post("/auth/email-code", json={"email": "a@b.co"})
    code = _sent_code()
    stale = SimpleNamespace(**vars(codes["a@b.co"]))
    codes["a@b.co"].attempts = email_verify.MAX_ATTEMPTS
    real = codes["a@b.co"]
    email_verify.get = lambda session, e: _async(stale)
    r = client.post("/auth/email-code/verify", json={"email": "a@b.co", "code": code})
    assert r.status_code == 400 and "다시 받아" in r.json()["detail"]
    assert real.verified_at is None


def test_refresh_twice_at_once_keeps_login(db, logins, clock) -> None:
    _signup("a@b.co")
    old_refresh = client.cookies["refresh_token"]
    first = _refresh(old_refresh)
    assert first.status_code == 200
    new_refresh = first.headers["set-cookie"].split(";")[0].split("=", 1)[1]

    second = _refresh(old_refresh)
    assert second.status_code == 200
    assert "set-cookie" not in second.headers
    assert logins[1].revoked_at is None
    for r in (first, second):
        assert client.get("/auth/me", headers=_auth(r.json()["access_token"])).status_code == 200
    assert _refresh(new_refresh).status_code == 200


def test_logout_with_old_access_token_still_cuts_login(db, logins) -> None:
    old_access = _signup("a@b.co")
    assert _refresh().status_code == 200
    cookie = client.cookies["refresh_token"]
    r = client.post("/auth/logout", headers=_auth(old_access))
    assert r.status_code == 204
    assert logins[1].revoked_at is not None
    assert _refresh(cookie).status_code == 401


def test_stolen_refresh_token_is_caught_after_many_rotations(db, logins, clock) -> None:
    _signup("a@b.co")
    victim_cookie = client.cookies["refresh_token"]
    attacker = _refresh(victim_cookie)
    for _ in range(3):
        clock.at += timedelta(minutes=15)
        attacker = client.post("/auth/refresh")
        assert attacker.status_code == 200
    attacker_access = attacker.json()["access_token"]

    clock.at += timedelta(minutes=1)
    assert _refresh(victim_cookie).status_code == 401
    assert logins[1].revoked_at is not None
    assert client.get("/auth/me", headers=_auth(attacker_access)).status_code == 401
    client.cookies.clear()
    assert (
        _refresh(attacker.headers["set-cookie"].split(";")[0].split("=", 1)[1]).status_code == 401
    )


def test_refresh_token_signature_is_tied_to_login(db, logins) -> None:
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
    _register("a@b.co")
    https = TestClient(app, base_url="https://testserver")
    r = https.post("/auth/token", data={"username": "a@b.co", "password": PW})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
    assert "Secure" in https.post("/auth/logout").headers["set-cookie"]

    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    assert "Secure" in _login("a@b.co", PW).headers["set-cookie"]
    monkeypatch.setattr(get_settings(), "cookie_secure", False)
    assert (
        "Secure"
        not in https.post("/auth/token", data={"username": "a@b.co", "password": PW}).headers[
            "set-cookie"
        ]
    )


def test_login_without_keep_gives_session_cookie(db, logins) -> None:
    _register("a@b.co")
    r = client.post("/auth/token", data={"username": "a@b.co", "password": PW, "keep": "false"})
    assert r.status_code == 200 and "Max-Age" not in r.headers["set-cookie"]
    r = client.post("/auth/refresh")
    assert r.status_code == 200 and "Max-Age" not in r.headers["set-cookie"]
    r = client.post(
        "/auth/password",
        json={"current_password": PW, "new_password": "new1234!"},
        headers=_auth(r.json()["access_token"]),
    )
    assert r.status_code == 200 and "Max-Age" not in r.headers["set-cookie"]
    assert "Max-Age" in _login("a@b.co", "new1234!").headers["set-cookie"]
