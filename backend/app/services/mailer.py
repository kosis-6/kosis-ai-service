"""메일 보내기 (회원가입 인증번호)

MAIL_BACKEND=smtp 이면 SMTP로 보내고, console 이면 보내지 않고 서버 로그에 내용을 찍는다(개발용).
SMTP 계정 비밀번호는 .env에만 두고 레포에 올리지 않는다.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MailNotConfigured(RuntimeError):
    """MAIL_BACKEND=smtp 인데 SMTP_HOST가 없다. main.py가 503으로 바꾼다"""


def _send_smtp(msg: EmailMessage) -> None:
    s = get_settings()
    context = ssl.create_default_context()
    if s.smtp_port == 465:
        server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=10)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10)
        server.starttls(context=context)
    with server:
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)


async def send(to: str, subject: str, body: str) -> None:
    s = get_settings()
    if s.mail_backend == "console":
        logger.warning(
            "[개발용 메일, 실제로 보내지 않음] 받는 사람=%s 제목=%s\n%s", to, subject, body
        )
        return
    if not s.smtp_host:
        raise MailNotConfigured(
            "메일 설정이 없습니다. .env에 SMTP_HOST·SMTP_USER·SMTP_PASSWORD를 넣으세요."
        )
    msg = EmailMessage()
    msg["From"] = s.mail_from or s.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    # smtplib은 동기 함수라 다른 요청을 막지 않게 스레드에서 보낸다
    await asyncio.to_thread(_send_smtp, msg)
