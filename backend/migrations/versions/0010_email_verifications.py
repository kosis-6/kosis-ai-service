"""app.email_verifications: 회원가입 이메일 인증번호

이메일마다 마지막으로 보낸 인증번호 하나(HMAC 값)와 만료 시각, 틀린 횟수, 확인한 시각을 둔다.
가입이 끝나면 그 이메일의 줄을 지운다.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_verifications",
        sa.Column("email", sa.String(), primary_key=True),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("email_verifications", schema="app")
