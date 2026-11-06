"""통계 수치 저장 구조: catalog 표·칸·코드·단위, raw 수치·적재 상태

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_PARTS = 16


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS catalog")
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")

    op.create_table(
        "stat_table",
        sa.Column("tbl_key", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False),
        sa.Column("tbl_id", sa.String(), nullable=False, unique=True),
        sa.Column("tbl_nm", sa.String(), nullable=False),
        schema="catalog",
    )
    op.create_table(
        "stat_dimension",
        sa.Column(
            "tbl_key", sa.Integer(), sa.ForeignKey("catalog.stat_table.tbl_key"), primary_key=True
        ),
        sa.Column("dim_no", sa.SmallInteger(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.CheckConstraint("dim_no BETWEEN 1 AND 5", name="stat_dimension_dim_no_check"),
        schema="catalog",
    )
    op.create_table(
        "stat_code",
        sa.Column("code_key", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column(
            "tbl_key", sa.Integer(), sa.ForeignKey("catalog.stat_table.tbl_key"), nullable=False
        ),
        sa.Column("dim_no", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("level", sa.SmallInteger()),
        sa.Column("path", sa.String()),
        sa.Column("is_total", sa.Boolean()),
        sa.UniqueConstraint("tbl_key", "dim_no", "code", name="stat_code_tbl_key_dim_no_code_key"),
        sa.CheckConstraint("dim_no BETWEEN 0 AND 5", name="stat_code_dim_no_check"),
        schema="catalog",
    )
    op.create_table(
        "stat_unit",
        sa.Column("unit_key", sa.SmallInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        schema="catalog",
    )

    op.create_table(
        "stat_value",
        sa.Column("tbl_key", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.Integer(), nullable=False),
        sa.Column("c1", sa.Integer()),
        sa.Column("c2", sa.Integer()),
        sa.Column("c3", sa.Integer()),
        sa.Column("c4", sa.Integer()),
        sa.Column("c5", sa.Integer()),
        sa.Column("unit_key", sa.SmallInteger()),
        sa.Column("prd_se", sa.CHAR(1), nullable=False),
        sa.Column("value", sa.Numeric()),
        sa.Column("flag", sa.String()),
        schema="raw",
        postgresql_partition_by="LIST (tbl_key)",
    )
    # 작은 표는 기본 파티션의 해시 묶음에 넣는다. 큰 표의 전용 파티션은 load_raw.py가 만든다
    op.execute(
        "CREATE TABLE raw.stat_value_small PARTITION OF raw.stat_value DEFAULT"
        " PARTITION BY HASH (tbl_key)"
    )
    for i in range(HASH_PARTS):
        op.execute(
            f"CREATE TABLE raw.stat_value_small_{i} PARTITION OF raw.stat_value_small"
            f" FOR VALUES WITH (MODULUS {HASH_PARTS}, REMAINDER {i})"
        )
    op.create_index(
        "stat_value_key",
        "stat_value",
        ["tbl_key", "item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5"],
        unique=True,
        schema="raw",
        postgresql_nulls_not_distinct=True,
    )

    op.create_table(
        "stat_load",
        sa.Column(
            "tbl_key", sa.Integer(), sa.ForeignKey("catalog.stat_table.tbl_key"), primary_key=True
        ),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("value_sum", sa.Numeric()),
        sa.Column("first_period", sa.Integer()),
        sa.Column("last_period", sa.Integer()),
        sa.Column("last_changed", sa.Date()),
        sa.Column("is_big", sa.Boolean(), nullable=False),
        sa.Column("source_file", sa.String(), nullable=False),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="raw",
    )


def downgrade() -> None:
    op.drop_table("stat_load", schema="raw")
    op.drop_table("stat_value", schema="raw")  # 하위 파티션도 함께 지워진다
    op.drop_table("stat_unit", schema="catalog")
    op.drop_table("stat_code", schema="catalog")
    op.drop_table("stat_dimension", schema="catalog")
    op.drop_table("stat_table", schema="catalog")
