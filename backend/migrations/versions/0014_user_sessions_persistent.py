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
