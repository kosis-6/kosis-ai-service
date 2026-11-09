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


def _secret() -> str:
    key = get_settings().jwt_secret_key
    if len(key) < 32:
        raise AuthNotConfigured(
            "로그인 설정이 없습니다. .env에 JWT_SECRET_KEY(32자 이상)를 넣으세요."
        )
    return key


def create_access_token(user_id: int, minutes: int | None = None) -> str:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=minutes or get_settings().access_token_minutes)
    return jwt.encode({"sub": str(user_id), "iat": now, "exp": exp}, _secret(), ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """서명이 맞고 만료되지 않았으면 사용자 id, 아니면 None"""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["exp"]})
        return int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None
