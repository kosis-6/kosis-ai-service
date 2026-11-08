"""사용자 계정 만들기. 관리자는 API로 만들 수 없어 이 스크립트로만 만든다

실행: python -m scripts.create_user admin@example.com --admin
      (비밀번호는 화면에 보이지 않게 입력받는다)
"""

import argparse
import asyncio
import getpass

from app.core.db import SessionLocal
from app.services import users


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--admin", action="store_true", help="관리자 권한 부여")
    args = parser.parse_args()

    password = getpass.getpass("비밀번호(8자 이상): ")
    if len(password) < 8 or password != getpass.getpass("한 번 더: "):
        raise SystemExit("비밀번호가 8자 미만이거나 두 번 입력한 값이 다릅니다.")
    async with SessionLocal() as session:
        try:
            user = await users.create(session, args.email, password, is_admin=args.admin)
        except users.EmailTaken:
            raise SystemExit(f"이미 있는 이메일입니다: {args.email}") from None
    print(f"만들었습니다: id={user.id} {user.email} 관리자={user.is_admin}")


if __name__ == "__main__":
    asyncio.run(main())
