"""public 테이블을 성격에 맞는 스키마로 옮긴다: 검색용 표 정보 → catalog, 대화·로그 → app

테이블이 아직 없는 DB에서도 실행된다 (IF EXISTS). 이후 적재 코드가 올바른 스키마에 만든다.
alembic_version과 확장(vector, pg_trgm)은 public에 그대로 둔다.

Revision ID: 0003
Revises: 0002
"""

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
