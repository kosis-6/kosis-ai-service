"""로그인 기본틀: 해시·토큰 함수와 /auth API (DB 대신 services.users를 바꿔 끼운다)"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import require_admin
from app.core import security
from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.services import users

SECRET = "test-secret-key-that-is-long-enough-0123456789"


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret_key", SECRET)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> dict[int, SimpleNamespace]:
    """이메일 → 사용자 대신 id → 사용자 사전. users 모듈 함수만 바꾸고 라우터는 그대로 쓴다"""
    rows: dict[int, SimpleNamespace] = {}

    async def create(session, email, password, is_admin=False):
        email = email.lower()
        if any(u.email == email for u in rows.values()):
            raise users.EmailTaken(email)
        user = SimpleNamespace(
            id=len(rows) + 1,
            email=email,
            password_hash=security.hash_password(password),
            is_active=True,
            is_admin=is_admin,
            created_at=datetime.now(UTC),
        )
        rows[user.id] = user
        return user

    async def get_by_email(session, email):
        return next((u for u in rows.values() if u.email == email.lower()), None)

    monkeypatch.setattr(users, "create", create)
    monkeypatch.setattr(users, "get_by_email", get_by_email)
    monkeypatch.setattr(users, "get", lambda session, i: _async(rows.get(i)))
    app.dependency_overrides[get_session] = lambda: None
    yield rows
    app.dependency_overrides.clear()


async def _async(v):
    return v


client = TestClient(app)


def _login(email: str, password: str):
    return client.post("/auth/token", data={"username": email, "password": password})


def test_password_hash_roundtrip() -> None:
    h = security.hash_password("correct horse")
    assert h.startswith("$argon2")
    assert security.verify_password("correct horse", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("anything", None)  # 없는 사용자


def test_token_roundtrip_and_rejects_bad_tokens() -> None:
    token = security.create_access_token(7)
    assert security.decode_access_token(token) == 7
    assert security.decode_access_token(token + "x") is None
    assert security.decode_access_token(security.create_access_token(7, minutes=-1)) is None


def test_short_secret_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jwt_secret_key", "short")
    with pytest.raises(security.AuthNotConfigured):
        security.create_access_token(1)


def test_register_login_me(db) -> None:
    r = client.post("/auth/register", json={"email": "User@Example.com", "password": "pw123456"})
    assert r.status_code == 201
    assert r.json()["email"] == "user@example.com"
    assert r.json()["is_admin"] is False
    assert "password_hash" not in r.json()

    again = client.post("/auth/register", json={"email": "user@example.com", "password": "x" * 8})
    assert again.status_code == 409

    assert _login("user@example.com", "wrong-pass").status_code == 401
    token = _login("USER@example.com", "pw123456").json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"


def test_register_validates_input(db) -> None:
    bad = [{"email": "no-at", "password": "pw123456"}, {"email": "a@b.co", "password": "short"}]
    for body in bad:
        assert client.post("/auth/register", json=body).status_code == 422


def test_me_requires_valid_active_user(db) -> None:
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401

    client.post("/auth/register", json={"email": "a@b.co", "password": "pw123456"})
    token = _login("a@b.co", "pw123456").json()["access_token"]
    db[1].is_active = False
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _login("a@b.co", "pw123456").status_code == 401


def test_login_without_secret_is_503(db, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/auth/register", json={"email": "a@b.co", "password": "pw123456"})
    monkeypatch.setattr(get_settings(), "jwt_secret_key", "")
    r = _login("a@b.co", "pw123456")
    assert r.status_code == 503
    assert "JWT_SECRET_KEY" in r.json()["detail"]


def test_require_admin() -> None:
    admin = SimpleNamespace(is_admin=True)
    assert asyncio.run(require_admin(admin)) is admin
    with pytest.raises(HTTPException) as e:
        asyncio.run(require_admin(SimpleNamespace(is_admin=False)))
    assert e.value.status_code == 403
