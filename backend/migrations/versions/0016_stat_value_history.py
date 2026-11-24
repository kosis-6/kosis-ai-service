from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stat_value_history",
        sa.Column("tbl_key", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.Integer(), nullable=False),
        sa.Column("c1", sa.Integer()),
        sa.Column("c2", sa.Integer()),
        sa.Column("c3", sa.Integer()),
        sa.Column("c4", sa.Integer()),
        sa.Column("c5", sa.Integer()),
        sa.Column("unit_key", sa.SmallInteger()),
        sa.Column("prd_se", sa.String(2), nullable=False),
        sa.Column("value", sa.Numeric()),
        sa.Column("flag", sa.String()),
        sa.Column(
            "archived_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="raw",
    )
    op.create_index(
        "stat_value_history_lookup",
        "stat_value_history",
        ["tbl_key", "item_key", "period", "c1"],
        schema="raw",
    )


def downgrade() -> None:
    op.drop_index("stat_value_history_lookup", table_name="stat_value_history", schema="raw")
    op.drop_table("stat_value_history", schema="raw")
