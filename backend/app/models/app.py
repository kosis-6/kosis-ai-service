"""app 스키마: 서비스 사용자 (대화·검색 기록 테이블은 아직 모델로 옮기지 않았다)"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, false, func, true
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
