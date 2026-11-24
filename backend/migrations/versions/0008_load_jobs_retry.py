from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "reason in ('question', 'indicator', 'update')"
NEW = "reason in ('question', 'indicator', 'update', 'manual')"


def upgrade() -> None:
    op.add_column(
        "load_jobs", sa.Column("run_after", sa.DateTime(timezone=True), nullable=True), schema="app"
    )
    op.drop_constraint("load_jobs_reason_check", "load_jobs", schema="app")
    op.create_check_constraint("load_jobs_reason_check", "load_jobs", NEW, schema="app")


def downgrade() -> None:
    op.execute("delete from app.load_jobs where reason = 'manual'")
    op.drop_constraint("load_jobs_reason_check", "load_jobs", schema="app")
    op.create_check_constraint("load_jobs_reason_check", "load_jobs", OLD, schema="app")
    op.drop_column("load_jobs", "run_after", schema="app")
