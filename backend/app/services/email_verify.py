"""회원가입 이메일 인증번호: 만들기, 확인하기, 가입할 수 있는지 보기

흐름
  1. POST /auth/email-code          인증번호(6자리)를 메일로 보낸다
  2. POST /auth/email-code/verify   받은 번호를 확인한다 → verified_at
  3. POST /auth/register            확인한 이메일만 가입할 수 있다 → 줄을 지운다

규칙
  - 번호는 10분 동안 쓸 수 있다
  - 5번 틀리면 그 번호는 더 쓸 수 없고 새로 받아야 한다 (6자리를 다 넣어 보는 것을 막는다)
  - 같은 이메일로 다시 받으려면 60초를 기다린다
  - 확인한 뒤 30분 안에 가입해야 한다
  - DB에는 번호 대신 HMAC 값만 둔다 (DB가 새어도 번호를 알 수 없게. 키는 JWT_SECRET_KEY)

판단(check, resend_wait, can_register)은 DB를 모르는 함수로 두고,
저장(get, save, delete)만 DB를 쓴다.
테스트는 저장 함수만 바꿔 끼운다.
"""

import hashlib
import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import secret_key
from app.models.app import EmailVerification

CODE_MINUTES = 10
VERIFIED_MINUTES = 30
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60


def now() -> datetime:
    return datetime.now(UTC)


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def code_hash(email: str, code: str) -> str:
    msg = f"{email.lower()}:{code}".encode()
    return hmac.new(secret_key().encode(), msg, hashlib.sha256).hexdigest()


def resend_wait(row: EmailVerification | None, at: datetime) -> int:
    """다시 보내려면 남은 초 (0이면 지금 보낼 수 있다)"""
    if row is None:
        return 0
    left = (row.sent_at + timedelta(seconds=RESEND_SECONDS) - at).total_seconds()
    return max(0, math.ceil(left))


def issue(row: EmailVerification | None, email: str, at: datetime) -> tuple[EmailVerification, str]:
    """새 번호를 만들어 줄에 넣는다 (전에 받은 번호·확인 기록은 없어진다)"""
    code = new_code()
    row = row or EmailVerification(email=email)
    row.code_hash = code_hash(email, code)
    row.attempts = 0
    row.sent_at = at
    row.expires_at = at + timedelta(minutes=CODE_MINUTES)
    row.verified_at = None
    return row, code


def check(row: EmailVerification | None, code: str, at: datetime) -> str:
    """ok / missing(받은 적 없음) / expired / too_many(5번 틀림) / wrong"""
    if row is None:
        return "missing"
    if row.attempts >= MAX_ATTEMPTS:
        return "too_many"
    if at >= row.expires_at:
        return "expired"
    if not hmac.compare_digest(row.code_hash, code_hash(row.email, code.strip())):
        return "wrong"
    return "ok"


def can_register(row: EmailVerification | None, at: datetime) -> bool:
    """번호를 확인했고, 확인한 지 30분이 안 지났으면 가입할 수 있다"""
    return (
        row is not None
        and row.verified_at is not None
        and at < row.verified_at + timedelta(minutes=VERIFIED_MINUTES)
    )


async def get(session: AsyncSession, email: str) -> EmailVerification | None:
    return await session.get(EmailVerification, email.lower())


async def save(session: AsyncSession, row: EmailVerification) -> None:
    session.add(row)
    await session.commit()


async def delete(session: AsyncSession, email: str) -> None:
    await session.execute(
        sql_delete(EmailVerification).where(EmailVerification.email == email.lower())
    )
    await session.commit()
