from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    nickname: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(server_default=true())
    is_admin: Mapped[bool] = mapped_column(server_default=false())
    token_version: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("user_sessions_user", "user_id"), {"schema": "app"})

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app.users.id", ondelete="CASCADE"))
    refresh_salt: Mapped[str]
    refresh_gen: Mapped[int]
    persistent: Mapped[bool] = mapped_column(server_default=text("true"))
    access_jti: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    __table_args__ = {"schema": "app"}

    email: Mapped[str] = mapped_column(primary_key=True)
    code_hash: Mapped[str]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        Index("chat_turns_session", "session_id", "turn_no"),
        Index("chat_turns_created", "created_at"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[str]
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.users.id", ondelete="SET NULL")
    )
    turn_no: Mapped[int]
    message: Mapped[str]
    request_type: Mapped[str | None]
    status: Mapped[str | None]
    conditions_before: Mapped[dict | None] = mapped_column(JSONB)
    conditions_after: Mapped[dict | None] = mapped_column(JSONB)
    sources: Mapped[dict | None] = mapped_column(JSONB)
    tbl_id: Mapped[str | None]
    recomputed: Mapped[bool]
    answer: Mapped[str | None]
    response: Mapped[dict | None] = mapped_column(JSONB)
    flow: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("status in ('draft', 'ready', 'failed')", name="reports_status"),
        Index("reports_user", "user_id", "updated_at"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app.users.id", ondelete="CASCADE"))
    session_id: Mapped[str | None]
    title: Mapped[str]
    purpose: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'draft'"))
    content: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportItem(Base):
    """담은 답 하나. snapshot은 담을 때 복사한 화면 내용이라 chat_turns가 지워져도 남는다"""

    __tablename__ = "report_items"
    __table_args__ = ({"schema": "app"},)

    report_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.reports.id", ondelete="CASCADE"), primary_key=True
    )
    turn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminAudit(Base):
    __tablename__ = "admin_audit"
    __table_args__ = (Index("admin_audit_created", "created_at"), {"schema": "app"})

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.users.id", ondelete="SET NULL")
    )
    admin_email: Mapped[str]
    method: Mapped[str]
    path: Mapped[str]
    query: Mapped[dict | None] = mapped_column(JSONB)


class LoadJob(Base):
    __tablename__ = "load_jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'done', 'failed')", name="load_jobs_status_check"
        ),
        CheckConstraint(
            "reason in ('question', 'indicator', 'update', 'manual')",
            name="load_jobs_reason_check",
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
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    error: Mapped[str | None]


class DynamicPlan(Base):
    __tablename__ = "dynamic_plans"
    __table_args__ = (
        UniqueConstraint("key", "tbl_id", "forced", name="dynamic_plans_unique"),
        Index("dynamic_plans_key", "key"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key: Mapped[str]
    term: Mapped[str]
    tbl_id: Mapped[str]
    forced: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    name: Mapped[str]
    formula: Mapped[str | None]
    cells: Mapped[str | None]
    uses: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("conversations_user", "user_id", "updated_at"), {"schema": "app"})

    session_id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app.users.id", ondelete="CASCADE"))
    title: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
