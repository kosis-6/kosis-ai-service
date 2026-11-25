from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StatValue(Base):
    __tablename__ = "stat_value"
    __table_args__ = (
        Index("stat_value_lookup", "tbl_key", "item_key", "period", "c1"),
        {"schema": "raw", "postgresql_partition_by": "LIST (tbl_key)"},
    )
    __mapper_args__ = {
        "primary_key": ["tbl_key", "item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5"]
    }

    tbl_key: Mapped[int]
    period: Mapped[int]
    item_key: Mapped[int]
    c1: Mapped[int | None]
    c2: Mapped[int | None]
    c3: Mapped[int | None]
    c4: Mapped[int | None]
    c5: Mapped[int | None]
    unit_key: Mapped[int | None] = mapped_column(SmallInteger)
    prd_se: Mapped[str] = mapped_column(String(2))
    value: Mapped[Decimal | None]
    flag: Mapped[str | None]


class StatValueHistory(Base):
    __tablename__ = "stat_value_history"
    __table_args__ = (
        Index("stat_value_history_lookup", "tbl_key", "item_key", "period", "c1"),
        {"schema": "raw"},
    )
    __mapper_args__ = {
        "primary_key": [
            "tbl_key", "item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5", "archived_at"
        ]
    }  # fmt: skip

    tbl_key: Mapped[int]
    period: Mapped[int]
    item_key: Mapped[int]
    c1: Mapped[int | None]
    c2: Mapped[int | None]
    c3: Mapped[int | None]
    c4: Mapped[int | None]
    c5: Mapped[int | None]
    unit_key: Mapped[int | None] = mapped_column(SmallInteger)
    prd_se: Mapped[str] = mapped_column(String(2))
    value: Mapped[Decimal | None]
    flag: Mapped[str | None]
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StatLoad(Base):
    __tablename__ = "stat_load"
    __table_args__ = {"schema": "raw"}

    tbl_key: Mapped[int] = mapped_column(ForeignKey("catalog.stat_table.tbl_key"), primary_key=True)
    row_count: Mapped[int]
    value_sum: Mapped[Decimal | None]
    first_period: Mapped[int | None]
    last_period: Mapped[int | None]
    last_changed: Mapped[date | None]
    is_big: Mapped[bool]
    source_file: Mapped[str]
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
