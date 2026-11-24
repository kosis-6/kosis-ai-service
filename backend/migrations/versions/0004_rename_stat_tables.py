from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = ("pkey", "survey_idx", "years_idx")


def _rename(old: str, new: str) -> None:
    op.execute(f"ALTER TABLE IF EXISTS catalog.{old} RENAME TO {new}")
    for suffix in INDEXES:
        op.execute(f"ALTER INDEX IF EXISTS catalog.{old}_{suffix} RENAME TO {new}_{suffix}")


def upgrade() -> None:
    _rename("stat_tables", "table_meta")


def downgrade() -> None:
    _rename("table_meta", "stat_tables")
