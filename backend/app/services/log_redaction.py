"""기록에 들어가는 키·토큰·접속 비밀번호를 중첩 데이터에서도 가린다."""

import re
from typing import Any

SECRET_NAME = (
    r"(?:x[-_]ncp[-_]clovastudio[-_]api[-_]key|api[-_]?key|service[-_]?key|"
    r"(?:access|refresh|id)[-_]?token|token|(?:client[-_])?secret|password|passwd|"
    r"authorization|cookie|set-cookie)"
)
SECRET_KEY = re.compile(rf"{SECRET_NAME}", re.I)
SECRET_VALUE = re.compile(
    rf"(\b{SECRET_NAME}[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^&\s,;}}\]<>]+)",
    re.I,
)
AUTH_TOKEN = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
URL_PASSWORD = re.compile(r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.I)
COOKIE_HEADER = re.compile(r"(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+", re.I)


def mask(value: str | None) -> str | None:
    if not value:
        return value

    def replace(match: re.Match) -> str:
        secret = match[2]
        quote = secret[0] if secret[0] in "\"'" else ""
        return f"{match[1]}{quote}***{quote}"

    value = URL_PASSWORD.sub(r"\1***\2", value)
    value = COOKIE_HEADER.sub(r"\1***", value)
    value = AUTH_TOKEN.sub(r"\1 ***", value)
    return SECRET_VALUE.sub(
        lambda m: m[0] if m[2].lower() in ("bearer", "basic") else replace(m), value
    )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if SECRET_KEY.fullmatch(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return mask(value) if isinstance(value, str) else value
