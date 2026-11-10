"""비밀번호 해시와 로그인 토큰(JWT). DB를 모르는 순수 함수만 둔다"""

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

ALGORITHM = "HS256"
_hasher = PasswordHash.recommended()  # argon2


class AuthNotConfigured(RuntimeError):
    """JWT_SECRET_KEY가 없거나 너무 짧다. main.py가 503으로 바꾼다"""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """없는 사용자(password_hash=None)도 해시를 한 번 계산한다.
    응답 시간 차이로 가입한 이메일인지 드러나지 않게 하려는 것"""
    if password_hash is None:
        _hasher.hash(password)
        return False
    return _hasher.verify(password, password_hash)


def secret_key() -> str:
    """로그인 토큰 서명 키. 이메일 인증번호 HMAC에도 같은 키를 쓴다"""
    key = get_settings().jwt_secret_key
    if len(key) < 32:
        raise AuthNotConfigured(
            "로그인 설정이 없습니다. .env에 JWT_SECRET_KEY(32자 이상)를 넣으세요."
        )
    return key


class AccessClaims(NamedTuple):
    user_id: int
    version: int  # 사용자의 token_version
    session_id: int  # app.user_sessions.id (어느 로그인에서 받은 토큰인지)
    jti: str  # 토큰 번호. 그 로그인의 access_jti와 같아야 쓸 수 있다


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
    """서명이 맞고 만료되지 않았으면 토큰 내용, 아니면 None.
    로그인 기록(sid, jti)이 없는 토큰(Refresh Token을 넣기 전에 발급)은 받지 않는다"""
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
