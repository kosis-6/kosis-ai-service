"""app.users.token_version: 비밀번호 변경·계정 정지 때 예전 토큰을 끊는 번호

토큰에 발급 당시 번호(ver)를 넣고, 요청마다 사용자 번호와 같은지 본다.
비밀번호를 바꾸거나 관리자가 계정을 정지하면 번호를 1 올려 그 전 토큰을 모두 무효로 만든다.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("users", "token_version", schema="app")
