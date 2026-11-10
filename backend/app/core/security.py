"""비밀번호 해시와 로그인 토큰(JWT). DB를 모르는 순수 함수만 둔다"""

from datetime import UTC, datetime, timedelta

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


def create_access_token(user_id: int, minutes: int | None = None, version: int = 0) -> str:
    """version은 사용자의 token_version. 사용자 번호가 올라가면 이 토큰은 더 쓸 수 없다"""
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=minutes or get_settings().access_token_minutes)
    payload = {"sub": str(user_id), "ver": version, "iat": now, "exp": exp}
    return jwt.encode(payload, secret_key(), ALGORITHM)


def decode_access_token(token: str) -> tuple[int, int] | None:
    """서명이 맞고 만료되지 않았으면 (사용자 id, 토큰 번호), 아니면 None.
    번호가 없는 토큰(번호를 넣기 전에 발급)은 0으로 본다"""
    try:
        payload = jwt.decode(
            token, secret_key(), algorithms=[ALGORITHM], options={"require": ["exp"]}
        )
        return int(payload["sub"]), int(payload.get("ver", 0))
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError):
        return None
