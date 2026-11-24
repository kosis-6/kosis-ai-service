from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("turn_no", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("request_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("conditions_before", JSONB(), nullable=True),
        sa.Column("conditions_after", JSONB(), nullable=True),
        sa.Column("sources", JSONB(), nullable=True),
        sa.Column("tbl_id", sa.String(), nullable=True),
        sa.Column("recomputed", sa.Boolean(), nullable=False),
        sa.Column("answer", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="app",
    )
    op.create_index("chat_turns_session", "chat_turns", ["session_id", "turn_no"], schema="app")
    op.create_index("chat_turns_created", "chat_turns", ["created_at"], schema="app")


def downgrade() -> None:
    op.drop_index("chat_turns_created", table_name="chat_turns", schema="app")
    op.drop_index("chat_turns_session", table_name="chat_turns", schema="app")
    op.drop_table("chat_turns", schema="app")
