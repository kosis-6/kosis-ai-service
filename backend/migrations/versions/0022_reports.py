"""통합 보고서: reports · report_items

담을 때 그 답의 화면 내용(figure · sources · chart · summary · 질문)을 report_items.snapshot에
복사한다. chat_turns는 30일 뒤 지워지므로 참조만 하면 보고서가 깨진다 (그래서 FK를 걸지 않는다).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="draft", nullable=False),
        sa.Column("content", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status in ('draft', 'ready', 'failed')", name="reports_status"),
        schema="app",
    )
    op.create_index("reports_user", "reports", ["user_id", "updated_at"], schema="app")
    op.create_table(
        "report_items",
        sa.Column(
            "report_id",
            sa.BigInteger(),
            sa.ForeignKey("app.reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("turn_id", sa.BigInteger(), primary_key=True),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("report_items", schema="app")
    op.drop_index("reports_user", table_name="reports", schema="app")
    op.drop_table("reports", schema="app")
