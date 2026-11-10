"""app.user_sessions: 로그인(기기)마다 Refresh Token 해시와 지금 쓸 수 있는 Access Token 번호

새 토큰을 받으면 access_jti가 바뀌어 예전 Access Token은 바로 못 쓴다 (블랙리스트 대신).
바로 전 Refresh Token 해시(prev_refresh_hash)가 다시 오면 탈취로 보고 끊는다.
로그아웃·비밀번호 변경·계정 정지 때 revoked_at을 채운다. 사용자를 지우면 같이 지워진다.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_hash", sa.String(), nullable=False),
        sa.Column("prev_refresh_hash", sa.String(), nullable=True),
        sa.Column("access_jti", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.create_index("user_sessions_user", "user_sessions", ["user_id"], schema="app")


def downgrade() -> None:
    op.drop_index("user_sessions_user", table_name="user_sessions", schema="app")
    op.drop_table("user_sessions", schema="app")
