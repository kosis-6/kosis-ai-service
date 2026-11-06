"""catalog 스키마: 수치(raw.stat_value)의 숫자 키가 뜻하는 표·칸·코드·단위"""

from sqlalchemy import CheckConstraint, ForeignKey, Identity, SmallInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StatTable(Base):
    """통계표"""

    __tablename__ = "stat_table"
    __table_args__ = {"schema": "catalog"}

    tbl_key: Mapped[int] = mapped_column(Identity(), primary_key=True)
    org_id: Mapped[str]
    tbl_id: Mapped[str] = mapped_column(unique=True)
    tbl_nm: Mapped[str]


class StatDimension(Base):
    """표마다 c1~c5 칸의 뜻. 메타데이터 기준 2,364개 표의 분류는 최대 5개다"""

    __tablename__ = "stat_dimension"
    __table_args__ = (
        CheckConstraint("dim_no BETWEEN 1 AND 5", name="stat_dimension_dim_no_check"),
        {"schema": "catalog"},
    )

    tbl_key: Mapped[int] = mapped_column(ForeignKey("catalog.stat_table.tbl_key"), primary_key=True)
    dim_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    name: Mapped[str]


class StatCode(Base):
    """코드와 이름. dim_no 0은 항목(item), 1~5는 분류"""

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
    # 합계 줄 여부. None은 아직 확인하지 않았다는 뜻 (이름만 보고 추측하지 않는다)
    is_total: Mapped[bool | None]


class StatUnit(Base):
    """단위 이름. 같은 항목도 분류에 따라 단위가 다를 수 있어(명·세대) 항목이 아니라 행에 붙인다"""

    __tablename__ = "stat_unit"
    __table_args__ = {"schema": "catalog"}

    unit_key: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
