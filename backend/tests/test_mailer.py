"""메일 만들기: 보낸 사람 이름, 스팸으로 보지 않게 하는 머리글, 글자·HTML 본문 (보내지는 않는다)"""

import pytest

from app.core.config import get_settings
from app.services import email_verify, mailer


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "smtp_user", "team@gmail.com")
    monkeypatch.setattr(s, "mail_from", "")
    monkeypatch.setattr(s, "mail_from_name", "LIKE SNAKE")


def test_sender_has_name_and_login_address(gmail) -> None:
    assert mailer.sender() == "LIKE SNAKE <team@gmail.com>"


def test_mail_from_with_name_is_kept(gmail, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mail_from", "통계 서비스 <team@gmail.com>")
    assert mailer.sender() == "통계 서비스 <team@gmail.com>"


def test_message_headers_and_bodies(gmail) -> None:
    subject, text, html = email_verify.code_mail("012345", "LIKE SNAKE")
    msg = mailer.message("user@naver.com", subject, text, html)
    assert msg["To"] == "user@naver.com"
    assert msg["Subject"] == "[LIKE SNAKE] 회원가입 인증번호 012345"
    assert msg["Date"]
    assert msg["Message-ID"].endswith("@gmail.com>")
    assert msg.get_content_type() == "multipart/alternative"
    plain, rich = msg.iter_parts()
    assert plain.get_content_type() == "text/plain" and "012345" in plain.get_content()
    assert rich.get_content_type() == "text/html" and "012345" in rich.get_content()


def test_text_only_message(gmail) -> None:
    msg = mailer.message("user@naver.com", "제목", "본문")
    assert msg.get_content_type() == "text/plain"


def test_code_mail_escapes_service_name() -> None:
    _, text, html = email_verify.code_mail("123456", "<b>팀</b>")
    assert "<b>팀</b>" not in html and "&lt;b&gt;" in html
    assert f"{email_verify.CODE_MINUTES}분" in text
