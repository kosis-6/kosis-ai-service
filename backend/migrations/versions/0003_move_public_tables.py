from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = {
    "stat_tables": "catalog",
    "table_families": "catalog",
    "table_embeddings": "catalog",
    "chat_sessions": "app",
    "search_logs": "app",
}


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    for table, schema in TABLES.items():
        op.execute(f"ALTER TABLE IF EXISTS public.{table} SET SCHEMA {schema}")
    op.execute("ALTER VIEW IF EXISTS public.family_axes SET SCHEMA catalog")


def downgrade() -> None:
    op.execute("ALTER VIEW IF EXISTS catalog.family_axes SET SCHEMA public")
    for table, schema in TABLES.items():
        op.execute(f"ALTER TABLE IF EXISTS {schema}.{table} SET SCHEMA public")
    op.execute("DROP SCHEMA IF EXISTS app")
