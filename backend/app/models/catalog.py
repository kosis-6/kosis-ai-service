from sqlalchemy import CheckConstraint, ForeignKey, Identity, SmallInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StatTable(Base):
    __tablename__ = "stat_table"
    __table_args__ = {"schema": "catalog"}

    tbl_key: Mapped[int] = mapped_column(Identity(), primary_key=True)
    org_id: Mapped[str]
    tbl_id: Mapped[str] = mapped_column(unique=True)
    tbl_nm: Mapped[str]


class StatDimension(Base):
    __tablename__ = "stat_dimension"
    __table_args__ = (
        CheckConstraint("dim_no BETWEEN 1 AND 5", name="stat_dimension_dim_no_check"),
        {"schema": "catalog"},
    )

    tbl_key: Mapped[int] = mapped_column(ForeignKey("catalog.stat_table.tbl_key"), primary_key=True)
    dim_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str]


class StatCode(Base):
    __tablename__ = "stat_code"
    __table_args__ = (
        UniqueConstraint("tbl_key", "dim_no", "code", name="stat_code_tbl_key_dim_no_code_key"),
        CheckConstraint("dim_no BETWEEN 0 AND 5", name="stat_code_dim_no_check"),
        {"schema": "catalog"},
    )

    code_key: Mapped[int] = mapped_column(Identity(), primary_key=True)
    tbl_key: Mapped[int] = mapped_column(ForeignKey("catalog.stat_table.tbl_key"))
    dim_no: Mapped[int] = mapped_column(SmallInteger)
    code: Mapped[str]
    name: Mapped[str]
    level: Mapped[int | None] = mapped_column(SmallInteger)
    path: Mapped[str | None]
    is_total: Mapped[bool | None]


class StatUnit(Base):
    __tablename__ = "stat_unit"
    __table_args__ = {"schema": "catalog"}

    unit_key: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
