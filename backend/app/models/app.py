"""app 스키마: 서비스 사용자, 통계표 적재 대기열

대화·검색 기록 테이블은 아직 모델로 옮기지 않았다.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
    # 화면 표시용 닉네임. 같은 닉네임도 된다. 0011 전에 가입한 계정은 비어 있다
    nickname: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(server_default=true())
    is_admin: Mapped[bool] = mapped_column(server_default=false())
    # 토큰에 넣는 번호. 비밀번호 변경·계정 정지 때 1 올려 그 전 토큰을 끊는다
    token_version: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSession(Base):
    """로그인 한 번(기기 하나)에 줄 하나. Refresh Token 보관과 예전 Access Token 막기를 함께 한다

    - refresh_salt: Refresh Token 서명에 넣는 이 줄의 난수. 토큰 자체는 두지 않는다
    - refresh_gen: 지금 Refresh Token의 세대. 새로 받을 때마다 1 오른다.
      그보다 낮은 세대의 진짜 토큰이 다시 오면 탈취로 보고 끊는다
    - access_jti: 지금 쓸 수 있는 Access Token 번호. 새 토큰을 받으면 바뀌어 예전 토큰은 못 쓴다
    - expires_at: 마지막으로 새로 받은 뒤 refresh_token_days, 단 created_at + session_max_days까지
    - revoked_at: 로그아웃·비밀번호 변경·계정 정지·Refresh Token 재사용 감지로 끊은 시각
    """

    __tablename__ = "user_sessions"
    __table_args__ = (Index("user_sessions_user", "user_id"), {"schema": "app"})

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app.users.id", ondelete="CASCADE"))
    refresh_salt: Mapped[str]
    refresh_gen: Mapped[int]
    # False면 "로그인 상태 유지"를 고르지 않은 로그인: 쿠키를 브라우저를 닫으면 없어지게 준다
    persistent: Mapped[bool] = mapped_column(server_default=text("true"))
    access_jti: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerification(Base):
    """회원가입 이메일 인증번호. 이메일마다 마지막으로 보낸 번호 하나만 둔다

    번호는 그대로 두지 않고 HMAC 값만 둔다. 확인에 성공하면 verified_at을 채우고,
    가입이 끝나면 지운다.
    """

    __tablename__ = "email_verifications"
    __table_args__ = {"schema": "app"}

    email: Mapped[str] = mapped_column(primary_key=True)
    code_hash: Mapped[str]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # 작을수록 먼저: 질문 0, 지표 사전·수동 등록 1, 갱신 2
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
    # 일시 오류로 다시 시도할 때 이 시각 전에는 꺼내지 않는다
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    error: Mapped[str | None]
