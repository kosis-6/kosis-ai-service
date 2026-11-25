from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "stat_value",
        "prd_se",
        schema="raw",
        type_=sa.String(2),
        existing_type=sa.CHAR(1),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "stat_value",
        "prd_se",
        schema="raw",
        type_=sa.CHAR(1),
        existing_type=sa.String(2),
        existing_nullable=False,
    )
