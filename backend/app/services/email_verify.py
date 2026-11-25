import hashlib
import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

from sqlalchemy import delete as sql_delete
from sqlalchemy import update
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
    if row is None:
        return 0
    left = (row.sent_at + timedelta(seconds=RESEND_SECONDS) - at).total_seconds()
    return max(0, math.ceil(left))


def issue(row: EmailVerification | None, email: str, at: datetime) -> tuple[EmailVerification, str]:
    code = new_code()
    row = row or EmailVerification(email=email)
    row.code_hash = code_hash(email, code)
    row.attempts = 0
    row.sent_at = at
    row.expires_at = at + timedelta(minutes=CODE_MINUTES)
    row.verified_at = None
    return row, code


def check(row: EmailVerification | None, code: str, at: datetime) -> str:
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
    return (
        row is not None
        and row.verified_at is not None
        and at < row.verified_at + timedelta(minutes=VERIFIED_MINUTES)
    )


def code_mail(code: str, service: str) -> tuple[str, str, str]:
    name = escape(service)
    subject = f"[{service}] 회원가입 인증번호 {code}"
    text = (
        f"{service} 회원가입 인증번호: {code}\n\n"
        f"{CODE_MINUTES}분 안에 가입 화면에 입력해 주세요.\n"
        "직접 요청하지 않았다면 이 메일은 무시하셔도 됩니다."
    )
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f7ef;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7ef;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="max-width:480px;background:#ffffff;border-radius:16px;
 font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#173b2e;">
<tr><td style="padding:32px 32px 8px;font-size:14px;font-weight:600;">{name}</td></tr>
<tr><td style="padding:0 32px;font-size:20px;font-weight:600;line-height:30px;">
회원가입 인증번호</td></tr>
<tr><td style="padding:24px 32px;">
<div style="padding:20px 0;border-radius:12px;background:#e7f1e5;text-align:center;
 font-family:Menlo,Consolas,monospace;font-size:32px;font-weight:700;letter-spacing:8px;">{code}</div>
</td></tr>
<tr><td style="padding:0 32px 32px;font-size:14px;line-height:22px;color:#5f7265;">
{CODE_MINUTES}분 안에 가입 화면에 입력해 주세요.<br>
직접 요청하지 않았다면 이 메일은 무시하셔도 됩니다.
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    return subject, text, html


async def get(session: AsyncSession, email: str) -> EmailVerification | None:
    return await session.get(EmailVerification, email.lower())


async def save(session: AsyncSession, row: EmailVerification) -> None:
    session.add(row)
    await session.commit()


async def use_attempt(session: AsyncSession, email: str) -> int | None:
    used = await session.scalar(
        update(EmailVerification)
        .where(
            EmailVerification.email == email.lower(),
            EmailVerification.attempts < MAX_ATTEMPTS,
        )
        .values(attempts=EmailVerification.attempts + 1)
        .returning(EmailVerification.attempts)
    )
    await session.commit()
    return used


async def delete(session: AsyncSession, email: str) -> None:
    await session.execute(
        sql_delete(EmailVerification).where(EmailVerification.email == email.lower())
    )
    await session.commit()
