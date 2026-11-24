from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_LOGS = """
CREATE TABLE IF NOT EXISTS app.search_logs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    session_id   text,
    user_id      bigint,
    question     text NOT NULL,
    status       text,
    tbl_id       text,
    slots        jsonb,
    candidates   jsonb,
    response     jsonb,
    hcx_raw      jsonb,
    timings      jsonb,
    elapsed_ms   int,
    error        text
)
"""


def upgrade() -> None:
    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "admin_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("admin_email", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("query", JSONB(), nullable=True),
        schema="app",
    )
    op.create_index("admin_audit_created", "admin_audit", ["created_at"], schema="app")

    op.execute(SEARCH_LOGS)
    op.execute("ALTER TABLE app.search_logs ADD COLUMN IF NOT EXISTS user_id bigint")
    op.execute(
        "UPDATE app.search_logs s SET user_id = NULL WHERE s.user_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM app.users u WHERE u.id = s.user_id)"
    )
    op.create_foreign_key(
        "search_logs_user_fk",
        "search_logs",
        "users",
        ["user_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index("search_logs_created", "search_logs", ["created_at"], schema="app")
    op.create_index("search_logs_user", "search_logs", ["user_id"], schema="app")


def downgrade() -> None:
    op.drop_index("search_logs_user", table_name="search_logs", schema="app")
    op.drop_index("search_logs_created", table_name="search_logs", schema="app")
    op.drop_constraint("search_logs_user_fk", "search_logs", schema="app", type_="foreignkey")
    op.drop_index("admin_audit_created", table_name="admin_audit", schema="app")
    op.drop_table("admin_audit", schema="app")
