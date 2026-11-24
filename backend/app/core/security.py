from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

ALGORITHM = "HS256"
_hasher = PasswordHash.recommended()


class AuthNotConfigured(RuntimeError):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _hasher.hash(password)
        return False
    return _hasher.verify(password, password_hash)


def secret_key() -> str:
    key = get_settings().jwt_secret_key
    if len(key) < 32:
        raise AuthNotConfigured(
            "로그인 설정이 없습니다. .env에 JWT_SECRET_KEY(32자 이상)를 넣으세요."
        )
    return key


class AccessClaims(NamedTuple):
    user_id: int
    version: int
    session_id: int
    jti: str


def create_access_token(
    user_id: int, session_id: int, jti: str, version: int = 0, minutes: int | None = None
) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=minutes or get_settings().access_token_minutes)
    payload = {
        "sub": str(user_id),
        "sid": session_id,
        "jti": jti,
        "ver": version,
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, secret_key(), ALGORITHM)


def decode_access_token(token: str) -> AccessClaims | None:
    try:
        payload = jwt.decode(
            token,
            secret_key(),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "sid", "jti"]},
        )
        return AccessClaims(
            int(payload["sub"]),
            int(payload.get("ver", 0)),
            int(payload["sid"]),
            str(payload["jti"]),
        )
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None
