"""app.users.nickname: 회원가입 때 적는 닉네임 (화면 표시용)

같은 닉네임을 여러 사람이 써도 된다 (로그인 아이디는 이메일).
이 열을 넣기 전에 가입한 계정은 비어 있다(NULL). PATCH /auth/me 로 적을 수 있다.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("nickname", sa.String(), nullable=True), schema="app")


def downgrade() -> None:
    op.drop_column("users", "nickname", schema="app")
