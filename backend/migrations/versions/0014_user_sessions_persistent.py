"""app.user_sessions.persistent: "로그인 상태 유지"를 골랐는지

False면 Refresh Token 쿠키에 기간을 주지 않아 브라우저를 닫으면 로그인이 끝난다.
새 토큰을 받을 때(POST /auth/refresh)도 같은 방식으로 쿠키를 다시 주려고 줄에 적어 둔다.
이미 있는 로그인은 지금까지처럼 유지(true)로 둔다.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_sessions",
        sa.Column("persistent", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("user_sessions", "persistent", schema="app")
