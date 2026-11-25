from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dynamic_plans",
        sa.Column("words", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("dynamic_plans", "words", schema="app")
