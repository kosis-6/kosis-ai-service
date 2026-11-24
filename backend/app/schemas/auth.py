import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError

PASSWORD_MIN = 6
PASSWORD_MAX = 20
OLD_PASSWORD_MAX = 128
PASSWORD_RULE = f"{PASSWORD_MIN}~{PASSWORD_MAX}자, 영문·숫자·특수기호를 모두 넣어 주세요."
NICKNAME_MIN = 2
NICKNAME_MAX = 20


def password_problem(v: str) -> str | None:
    if len(v) < PASSWORD_MIN:
        return f"비밀번호는 {PASSWORD_MIN}자 이상이어야 합니다."
    if len(v) > PASSWORD_MAX:
        return f"비밀번호는 {PASSWORD_MAX}자 이하여야 합니다."
    missing = [
        name
        for name, pattern in (
            ("영문", r"[A-Za-z]"),
            ("숫자", r"[0-9]"),
            ("특수기호", r"[^A-Za-z0-9\s]"),
        )
        if not re.search(pattern, v)
    ]
    if missing:
        particle = "을" if missing[-1] == "영문" else "를"
        return f"비밀번호에 {'·'.join(missing)}{particle} 넣어 주세요. ({PASSWORD_RULE})"
    return None


def _check_password(v: str) -> str:
    problem = password_problem(v)
    if problem:
        raise PydanticCustomError("password_rule", problem)
    return v


def nickname_problem(v: str) -> str | None:
    if not NICKNAME_MIN <= len(v) <= NICKNAME_MAX:
        return f"닉네임은 {NICKNAME_MIN}~{NICKNAME_MAX}자로 적어 주세요."
    if any(not c.isprintable() for c in v):
        return "닉네임에 줄바꿈이나 탭은 넣을 수 없습니다."
    return None


def _check_nickname(v: str) -> str:
    v = v.strip()
    problem = nickname_problem(v)
    if problem:
        raise PydanticCustomError("nickname_rule", problem)
    return v


def _lower(v: str) -> str:
    return v.strip().lower()


Email = Annotated[
    str,
    Field(
        max_length=320, pattern=r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$", examples=["user@example.com"]
    ),
    AfterValidator(_lower),
]
Password = Annotated[str, Field(description=PASSWORD_RULE), AfterValidator(_check_password)]
Nickname = Annotated[
    str,
    Field(description=f"화면에 보일 이름, {NICKNAME_MIN}~{NICKNAME_MAX}자", examples=["통계왕"]),
    AfterValidator(_check_nickname),
]


class EmailCodeRequest(BaseModel):
    email: Email


class EmailCodeVerify(BaseModel):
    email: Email
    code: str = Field(pattern=r"^\s*\d{6}\s*$", description="메일로 받은 6자리 숫자")


class EmailCodeSent(BaseModel):
    expires_in: int = Field(description="인증번호를 쓸 수 있는 시간(초)")
    resend_after: int = Field(description="다시 받을 수 있을 때까지(초)")


class EmailVerified(BaseModel):
    expires_in: int = Field(description="이 시간(초) 안에 가입을 마쳐야 한다")


class UserCreate(BaseModel):
    email: Email
    password: Password
    nickname: Nickname


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    nickname: str | None = Field(description="0011 전에 가입한 계정은 비어 있을 수 있다")
    is_admin: bool
    created_at: datetime


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=OLD_PASSWORD_MAX)
    new_password: Password


class ProfileUpdate(BaseModel):
    nickname: Nickname


class UserAdminRead(UserRead):
    is_active: bool


class UserList(BaseModel):
    total: int
    users: list[UserAdminRead]


class UserUpdate(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="토큰 유효 시간(초)")
