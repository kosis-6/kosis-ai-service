"""raw.stat_value_history: 표를 다시 적재할 때 값이 바뀐 칸의 예전 값

KOSIS 갱신으로 표를 다시 받으면 새 값으로 답하되, 예전 값은 지우지 않고 여기에 남긴다
(pipeline/load_raw.py). 새로 받은 자료에 아예 없는 칸(KOSIS가 뺀 예전 시점 등)은 이 테이블이 아니라
raw.stat_value에 그대로 남아 질문에 계속 답한다.

  archived_at  예전 값이 새 값으로 바뀐 시각 (그 적재 시각)
  나머지 칸     raw.stat_value와 같다 (바뀌기 전 값)

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stat_value_history",
        sa.Column("tbl_key", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.Integer(), nullable=False),
        sa.Column("c1", sa.Integer()),
        sa.Column("c2", sa.Integer()),
        sa.Column("c3", sa.Integer()),
        sa.Column("c4", sa.Integer()),
        sa.Column("c5", sa.Integer()),
        sa.Column("unit_key", sa.SmallInteger()),
        sa.Column("prd_se", sa.String(2), nullable=False),
        sa.Column("value", sa.Numeric()),
        sa.Column("flag", sa.String()),
        sa.Column(
            "archived_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="raw",
    )
    op.create_index(
        "stat_value_history_lookup",
        "stat_value_history",
        ["tbl_key", "item_key", "period", "c1"],
        schema="raw",
    )


def downgrade() -> None:
    op.drop_index("stat_value_history_lookup", table_name="stat_value_history", schema="raw")
    op.drop_table("stat_value_history", schema="raw")
