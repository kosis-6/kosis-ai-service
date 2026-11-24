from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_turns", sa.Column("flow", JSONB(), nullable=True), schema="app")


def downgrade() -> None:
    op.drop_column("chat_turns", "flow", schema="app")
