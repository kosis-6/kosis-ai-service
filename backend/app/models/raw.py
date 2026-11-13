"""raw 스키마: KOSIS 수치와 표별 적재 상태

수치는 수억 행까지 늘어나므로 이름·단위 같은 글자는 catalog에 한 번만 두고 숫자 키만 넣는다.
적재는 ORM이 아니라 COPY로 한다 (pipeline/load_raw.py).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StatValue(Base):
    """수치 한 칸. 분류가 없는 칸은 None. value가 None이면 flag에 통계 부호(X, -, *)가 있을 수 있다

    작은 표는 기본 파티션의 해시 묶음에, 큰 표는 전용 파티션에 들어간다. 하위 파티션은 모델에 쓰지
    않는다 (기본 파티션: 마이그레이션 0001, 전용 파티션: load_raw.py).
    DB에 PRIMARY KEY·고유 인덱스가 없다. 고유 인덱스가 7억 행에서 수십 GB를 차지해, 중복은
    load_raw.py가 교체 전에 검사한다. 수치를 쓰는 경로는 load_raw.py 하나여야 한다.
    """

    __tablename__ = "stat_value"
    __table_args__ = (
        Index("stat_value_lookup", "tbl_key", "item_key", "period", "c1"),
        {"schema": "raw", "postgresql_partition_by": "LIST (tbl_key)"},
    )
    # DB에는 PRIMARY KEY가 없다. ORM이 행을 구분하는 데만 쓴다
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
    # 수록주기 코드. 대부분 한 글자(A 년, M 월, Q 분기, F 다년 …)지만 부정기는 'IR'
    prd_se: Mapped[str] = mapped_column(String(2))
    value: Mapped[Decimal | None]
    flag: Mapped[str | None]


class StatValueHistory(Base):
    """다시 적재할 때 값이 바뀐 칸의 예전 값 (pipeline/load_raw.py, 마이그레이션 0016).
    새 자료에 없는 칸은 여기가 아니라 stat_value에 그대로 남는다"""

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
    """표별 현재 적재 상태. 다음 수집 때 이 값과 비교해 바뀐 표만 다시 받는다"""

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
