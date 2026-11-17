"""app.conversations, app.chat_turns.response: 로그인 유저의 대화 기록 보관

로그인 유저가 나눈 대화를 계정에 남겨, 나중에 목록에서 다시 열어 이어 가거나 새 창으로 시작한다.
1. app.conversations: 로그인 유저의 대화 (주인, 제목, 마지막 대화 시각). 손님 대화는 남기지 않는다.
   사용자를 지우면 대화도 지운다. 마지막 대화 뒤 30일이 지나면 지운다 (log_retention.KEEP_DAYS).
2. app.chat_turns.response: 턴마다 화면에 보여 준 답 전체 (답 글·차트·표 카드·알림·적용 조건).
   예전 대화를 열면 다시 계산하지 않고 그대로 보여 준다. 마스킹은 기존 턴 기록과 같다.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="app",
    )
    op.create_index("conversations_user", "conversations", ["user_id", "updated_at"], schema="app")
    op.add_column("chat_turns", sa.Column("response", JSONB(), nullable=True), schema="app")


def downgrade() -> None:
    op.drop_column("chat_turns", "response", schema="app")
    op.drop_index("conversations_user", table_name="conversations", schema="app")
    op.drop_table("conversations", schema="app")
