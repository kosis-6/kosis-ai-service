import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MailNotConfigured(RuntimeError):
    pass


def sender() -> str:
    s = get_settings()
    address = s.mail_from or s.smtp_user
    if not address or "<" in address:
        return address
    return formataddr((s.mail_from_name, address)) if s.mail_from_name else address


def message(to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
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
    await asyncio.to_thread(_send_smtp, message(to, subject, text, html))
