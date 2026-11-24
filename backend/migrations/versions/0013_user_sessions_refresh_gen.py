from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM app.user_sessions")
    op.drop_column("user_sessions", "refresh_hash", schema="app")
    op.drop_column("user_sessions", "prev_refresh_hash", schema="app")
    op.add_column(
        "user_sessions", sa.Column("refresh_salt", sa.String(), nullable=False), schema="app"
    )
    op.add_column(
        "user_sessions", sa.Column("refresh_gen", sa.Integer(), nullable=False), schema="app"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.user_sessions")
    op.drop_column("user_sessions", "refresh_gen", schema="app")
    op.drop_column("user_sessions", "refresh_salt", schema="app")
    op.add_column(
        "user_sessions", sa.Column("refresh_hash", sa.String(), nullable=False), schema="app"
    )
    op.add_column(
        "user_sessions", sa.Column("prev_refresh_hash", sa.String(), nullable=True), schema="app"
    )
