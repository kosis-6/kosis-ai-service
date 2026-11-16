"""기록에 들어가는 키·토큰·접속 비밀번호를 중첩 데이터에서도 가린다."""

import re
from typing import Any

SECRET_NAME = (
    r"(?:x[-_]ncp[-_]clovastudio[-_]api[-_]key|api[-_]?key|service[-_]?key|"
    r"(?:access|refresh|id)[-_]?token|token|(?:client[-_])?secret|password|passwd|"
    r"authorization|cookie|set-cookie)"
)
# 환경 변수 꼴(HCX_API_KEY, JWT_SECRET_KEY, POSTGRES_PASSWORD)도 잡는다: 앞에 붙은 말과 뒤의 _KEY.
# 앞이 글자·숫자로 이어지면 이름이 아니다 (promptTokens, max_tokens 는 가리지 않는다)
FULL_NAME = rf"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[-_])*{SECRET_NAME}(?:[-_]key)?"
SECRET_KEY = re.compile(FULL_NAME, re.I)
SECRET_VALUE = re.compile(
    rf"({FULL_NAME}[\"']?\s*(?:[:=]|%3D)\s*)"
    rf"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^&\s,;}}\]<>]+)",
    re.I,
)
# 이름 없이 값만 찍힌 것: 로그인 토큰(JWT), CLOVA Studio(nv-) · OpenAI 꼴(sk-) 키
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
PROVIDER_KEY = re.compile(r"\b(?:nv|sk)-[A-Za-z0-9_-]{16,}")
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
    value = PROVIDER_KEY.sub("***", JWT.sub("***", value))
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
