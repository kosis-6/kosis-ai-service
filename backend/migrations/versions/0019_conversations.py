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
