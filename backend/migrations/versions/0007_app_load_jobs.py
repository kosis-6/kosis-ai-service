"""app.load_jobs: 통계표 적재 대기열

적재 안 된 표를 질문·지표 사전·갱신에서 넣고, 적재 프로세스가 꺼내 처리한다.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "load_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tbl_id", sa.String(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("requested_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column(
            "last_requested_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'done', 'failed')", name="load_jobs_status_check"
        ),
        sa.CheckConstraint(
            "reason in ('question', 'indicator', 'update')", name="load_jobs_reason_check"
        ),
        schema="app",
    )
    op.create_index(
        "load_jobs_active_tbl",
        "load_jobs",
        ["tbl_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("status in ('queued', 'running')"),
    )
    op.create_index(
        "load_jobs_queue", "load_jobs", ["status", "priority", "requested_at"], schema="app"
    )


def downgrade() -> None:
    op.drop_table("load_jobs", schema="app")
