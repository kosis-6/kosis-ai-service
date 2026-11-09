"""app 스키마: 서비스 사용자, 통계표 적재 대기열

대화·검색 기록 테이블은 아직 모델로 옮기지 않았다.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    SmallInteger,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """로그인 사용자. 이메일은 소문자로 저장하고, 비밀번호는 argon2 해시만 둔다"""

    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=true())
    is_admin: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoadJob(Base):
    """통계표 적재 대기열. 질문·지표 사전·갱신에서 적재가 필요한 표를 넣고, 적재 프로세스가 꺼낸다

    같은 표의 대기·진행 중 작업은 하나만 둔다 (부분 고유 인덱스). 다시 요청되면 requested_count만
    올린다. 끝난 작업(done, failed)은 기록으로 남고, 다시 요청하면 새 작업이 생긴다.
    표가 적재됐는지는 이 테이블이 아니라 raw.stat_load로 판단한다.
    """

    __tablename__ = "load_jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'done', 'failed')", name="load_jobs_status_check"
        ),
        CheckConstraint(
            "reason in ('question', 'indicator', 'update')", name="load_jobs_reason_check"
        ),
        Index(
            "load_jobs_active_tbl",
            "tbl_id",
            unique=True,
            postgresql_where=text("status in ('queued', 'running')"),
        ),
        Index("load_jobs_queue", "status", "priority", "requested_at"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tbl_id: Mapped[str]
    org_id: Mapped[str]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'queued'"))
    # 작을수록 먼저: 질문 0, 지표 사전 1, 갱신 2
    priority: Mapped[int] = mapped_column(SmallInteger)
    requested_count: Mapped[int] = mapped_column(server_default=text("1"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    error: Mapped[str | None]
