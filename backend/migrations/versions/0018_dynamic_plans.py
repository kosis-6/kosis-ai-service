from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dynamic_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("tbl_id", sa.String(), nullable=False),
        sa.Column("forced", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("formula", sa.String(), nullable=True),
        sa.Column("cells", sa.String(), nullable=True),
        sa.Column("uses", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("key", "tbl_id", "forced", name="dynamic_plans_unique"),
        schema="app",
    )
    op.create_index("dynamic_plans_key", "dynamic_plans", ["key"], schema="app")


def downgrade() -> None:
    op.drop_index("dynamic_plans_key", table_name="dynamic_plans", schema="app")
    op.drop_table("dynamic_plans", schema="app")
