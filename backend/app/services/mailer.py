"""메일 보내기 (회원가입 인증번호)

MAIL_BACKEND=smtp 이면 SMTP로 보내고, console 이면 보내지 않고 서버 로그에 내용을 찍는다(개발용).
SMTP 계정 비밀번호는 .env에만 두고 레포에 올리지 않는다. 설정 방법은 app/core/config.py,
설정 확인은 python -m scripts.send_test_mail 받을주소
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MailNotConfigured(RuntimeError):
    """MAIL_BACKEND=smtp 인데 SMTP_HOST가 없다. main.py가 503으로 바꾼다"""


def sender() -> str:
    """보낸 사람: '이름 <주소>'. MAIL_FROM에 이름까지 적었으면 그대로 쓴다"""
    s = get_settings()
    address = s.mail_from or s.smtp_user
    if not address or "<" in address:
        return address
    return formataddr((s.mail_from_name, address)) if s.mail_from_name else address


def message(to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    """보낼 메일. 글자만 보는 메일 앱을 위해 글자 본문을 먼저 넣고,
    HTML은 대신 보여 줄 본문으로 붙인다.
    Date·Message-ID가 없으면 스팸으로 보는 메일 서비스가 있어 직접 넣는다"""
    msg = EmailMessage()
    msg["From"] = sender()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(msg["From"])[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


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


async def send(to: str, subject: str, text: str, html: str | None = None) -> None:
    s = get_settings()
    if s.mail_backend == "console":
        logger.warning(
            "[개발용 메일, 실제로 보내지 않음] 받는 사람=%s 제목=%s\n%s", to, subject, text
        )
        return
    if not s.smtp_host:
        raise MailNotConfigured(
            "메일 설정이 없습니다. .env에 SMTP_HOST·SMTP_USER·SMTP_PASSWORD를 넣으세요."
        )
    # smtplib은 동기 함수라 다른 요청을 막지 않게 스레드에서 보낸다
    await asyncio.to_thread(_send_smtp, message(to, subject, text, html))
