from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("stat_value_key", table_name="stat_value", schema="raw")
    op.create_index(
        "stat_value_lookup",
        "stat_value",
        ["tbl_key", "item_key", "period", "c1"],
        schema="raw",
    )


def downgrade() -> None:
    op.drop_index("stat_value_lookup", table_name="stat_value", schema="raw")
    op.create_index(
        "stat_value_key",
        "stat_value",
        ["tbl_key", "item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5"],
        unique=True,
        schema="raw",
        postgresql_nulls_not_distinct=True,
    )
