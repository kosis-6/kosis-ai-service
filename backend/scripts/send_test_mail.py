"""메일 설정 확인: .env의 SMTP 설정으로 인증번호 메일과 같은 모양의 시험 메일을 한 통 보낸다

실행: docker compose exec api python -m scripts.send_test_mail 받을주소@example.com
      (.env를 고쳤으면 먼저 docker compose up -d api 로 다시 띄워야 새 값이 들어간다)

비밀번호는 화면에 찍지 않는다. 실패하면 원인별로 확인할 것을 알려 준다.
"""

import argparse
import asyncio
import smtplib
import socket
import sys

from app.core.config import get_settings
from app.services import email_verify, mailer

HINTS = {
    smtplib.SMTPAuthenticationError: (
        "아이디·비밀번호가 맞지 않습니다. Gmail은 계정 비밀번호가 아니라 앱 비밀번호(16자리)를 "
        "공백 없이 넣어야 하고, 앱 비밀번호는 2단계 인증을 켠 계정에서만 만들 수 있습니다."
    ),
    smtplib.SMTPSenderRefused: (
        "보내는 주소를 거절했습니다. Gmail은 MAIL_FROM을 비우거나(SMTP_USER로 보냄) "
        "Gmail 설정에 등록한 '다른 주소에서 메일 보내기' 주소만 쓸 수 있습니다."
    ),
    smtplib.SMTPRecipientsRefused: "받는 주소를 거절했습니다. 주소에 오타가 없는지 확인하세요.",
    smtplib.SMTPNotSupportedError: "SMTP_PORT를 확인하세요. 587은 STARTTLS, 465는 SSL입니다.",
}
NO_CONNECTION = (
    TimeoutError,
    ConnectionError,
    socket.gaierror,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
)
CONNECT = (
    "메일 서버에 연결하지 못했습니다. SMTP_HOST·SMTP_PORT(Gmail: smtp.gmail.com, 587)와 "
    "서버의 바깥 연결(방화벽, 보안 그룹)을 확인하세요."
)


def describe() -> None:
    s = get_settings()
    print(f"MAIL_BACKEND  {s.mail_backend}")
    print(f"SMTP_HOST     {s.smtp_host or '(없음)'}:{s.smtp_port}")
    print(f"SMTP_USER     {s.smtp_user or '(없음)'}")
    password = s.smtp_password
    state = f"{len(password)}자" if password else "없음"
    if " " in password:
        state += ", 공백 있음"
    print(f"SMTP_PASSWORD ({state})")
    print(f"보내는 사람    {mailer.sender() or '(없음)'}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("to", help="시험 메일을 받을 주소")
    args = parser.parse_args()

    describe()
    s = get_settings()
    if s.mail_backend != "smtp":
        print(
            "\nMAIL_BACKEND가 smtp가 아니라 보내지 않습니다. .env에 MAIL_BACKEND=smtp를 넣으세요."
        )
        return 1
    subject, text, html = email_verify.code_mail("123456", s.mail_from_name or "LIKE SNAKE")
    try:
        await mailer.send(args.to, f"(시험) {subject}", text, html)
    except mailer.MailNotConfigured as e:
        print(f"\n보내지 못했습니다: {e}")
        return 1
    except (OSError, smtplib.SMTPException) as e:
        hint = next((h for kind, h in HINTS.items() if isinstance(e, kind)), None)
        if hint is None and isinstance(e, NO_CONNECTION):
            hint = CONNECT
        print(f"\n보내지 못했습니다: {type(e).__name__}: {e}")
        if hint:
            print(hint)
        return 1
    print(f"\n보냈습니다 → {args.to} (받은 편지함에 없으면 스팸함도 확인하세요)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
