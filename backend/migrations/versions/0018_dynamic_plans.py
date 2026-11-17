"""app.dynamic_plans: 사전에 없는 지표의 계산 방법 캐시 (동적 지표 4단계)

사전에 없는 지표를 통계표에서 바로 계산했으면(app/services/dynamic_indicator.py)
어느 표의 어느 칸으로 계산했는지 남긴다.
같은 지표를 다시 물으면 통계표 찾기(HCX 3회)를 건너뛰고 이 표로 바로 계산한다.
지역·연도와 숫자는 저장하지 않는다 (숫자는 매일 0시 갱신으로 바뀔 수 있다).

- key: 지표 표현에서 공백·기호를 뺀 것 (예: "1인가구중여성비율")
- forced: 되묻기에서 사용자가 고른 칸 {분류 번호: 코드}
- uses·last_used_at: 자주 쓰인 계획은 정의를 확인해 지표 사전에 올린다 (관리자 화면)

Revision ID: 0018
Revises: 0017
"""

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
