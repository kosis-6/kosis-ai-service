"""사용자 계정 만들기. 관리자는 API로 만들 수 없어 이 스크립트로만 만든다
(서버에서 직접 만드는 계정이라 이메일 인증은 거치지 않는다. 비밀번호 규칙은 회원가입과 같다)

실행: python -m scripts.create_user admin@example.com --admin --nickname 관리자
      (비밀번호는 화면에 보이지 않게 입력받는다)

관리자 이메일은 본인이 실제로 쓰는 주소로 만든다. 남이 쓰는 주소(admin@gmail.com 등)로 만들면
메일로 비밀번호를 찾는 기능이 생겼을 때 그 주소의 주인이 관리자 계정을 가져갈 수 있다
"""

import argparse
import asyncio
import getpass
import re

from app.core.db import SessionLocal
from app.schemas.auth import PASSWORD_RULE, nickname_problem, password_problem
from app.services import users

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--admin", action="store_true", help="관리자 권한 부여")
    parser.add_argument("--nickname", help="화면에 보일 이름(2~20자). 빼면 비워 둔다")
    args = parser.parse_args()
    email = args.email.strip().lower()
    if not EMAIL.fullmatch(email):
        raise SystemExit(f"이메일 형식이 아닙니다: {args.email} (예: admin@example.com)")
    nickname = args.nickname.strip() if args.nickname else None
    if nickname and (problem := nickname_problem(nickname)):
        raise SystemExit(problem)

    password = getpass.getpass(f"비밀번호({PASSWORD_RULE}): ")
    problem = password_problem(password)
    if problem:
        raise SystemExit(problem)
    if password != getpass.getpass("한 번 더: "):
        raise SystemExit("두 번 입력한 비밀번호가 다릅니다.")
    async with SessionLocal() as session:
        try:
            user = await users.create(
                session, email, password, nickname=nickname, is_admin=args.admin
            )
        except users.EmailTaken:
            raise SystemExit(f"이미 있는 이메일입니다: {email}") from None
    print(f"만들었습니다: id={user.id} {user.email} 닉네임={user.nickname} 관리자={user.is_admin}")


if __name__ == "__main__":
    asyncio.run(main())
