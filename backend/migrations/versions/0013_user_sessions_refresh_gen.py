"""app.user_sessions: Refresh Token을 해시 대신 세대·서명으로 확인

바로 전 Refresh Token 해시(prev_refresh_hash)만 두면, 훔친 사람이 두 번 새로 받은 뒤 돌아온
진짜 사용자의 토큰은 "틀린 값"이 되어 탈취를 알아채지 못했다.
이제 토큰은 "{줄 id}.{세대}.{서명}"이고
서명 = HMAC(JWT_SECRET_KEY, 줄 id·세대·refresh_salt). 서버는 예전 토큰을 쌓아 두지 않아도
몇 세대 전이든 진짜 발급한 값인지 확인해 끊을 수 있다 (app/services/auth_sessions.py).

예전 모양의 쿠키는 확인할 수 없어서 기존 로그인을 모두 지운다 (모두 다시 로그인).

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM app.user_sessions")
    op.drop_column("user_sessions", "refresh_hash", schema="app")
    op.drop_column("user_sessions", "prev_refresh_hash", schema="app")
    op.add_column(
        "user_sessions", sa.Column("refresh_salt", sa.String(), nullable=False), schema="app"
    )
    op.add_column(
        "user_sessions", sa.Column("refresh_gen", sa.Integer(), nullable=False), schema="app"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.user_sessions")
    op.drop_column("user_sessions", "refresh_gen", schema="app")
    op.drop_column("user_sessions", "refresh_salt", schema="app")
    op.add_column(
        "user_sessions", sa.Column("refresh_hash", sa.String(), nullable=False), schema="app"
    )
    op.add_column(
        "user_sessions", sa.Column("prev_refresh_hash", sa.String(), nullable=True), schema="app"
    )
