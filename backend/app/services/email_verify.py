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


def code_mail(code: str, service: str) -> tuple[str, str, str]:
    """인증번호 메일 → (제목, 글자 본문, HTML 본문).
    메일 앱은 HTML을 보여 주고, 못 하면 글자 본문을 보여 준다.
    HTML은 메일 앱마다 지원하는 CSS가 달라 표와 인라인 스타일만 쓴다"""
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
    """확인 시도 한 번을 DB에서 한 번에 센다 → 센 뒤의 횟수. 이미 MAX_ATTEMPTS번이면 None.
    읽고 더해 저장하면 동시에 보낸 요청이 모두 0을 읽어 5번 넘게 넣어 볼 수 있다"""
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
