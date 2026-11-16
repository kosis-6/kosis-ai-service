"""app.admin_audit 추가, app.search_logs 를 사용자와 잇고 날짜 인덱스 추가

1. app.admin_audit: 관리자 API 호출 기록 (누가 언제 무엇을 봤는지·바꿨는지).
   관리자가 사용자의 질문과 이메일을 볼 수 있어 그 열람을 남긴다 (app/services/admin_audit.py).
2. app.search_logs.user_id → app.users.id (ON DELETE SET NULL).
   지금까지는 연결이 없어 사용자를 지워도 그 번호가 기록에 남았다. 대화 턴 기록(app.chat_turns)처럼
   사용자를 지우면 기록은 남고 번호만 비운다. 이미 없는 사용자를 가리키는 번호는 먼저 비운다.
3. app.search_logs(created_at) 인덱스: 기간으로 거르고(관리자 화면), 30일 지난 기록을 지울 때 쓴다
   (app/services/search_log.py 의 KEEP_DAYS).

app.search_logs 는 처음 기록할 때 만들어지는 테이블이라(search_log.DDL) 아직 없을 수 있다.
없으면 같은 모양으로 먼저 만든다.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# app/services/search_log.py 의 DDL 과 같다
SEARCH_LOGS = """
CREATE TABLE IF NOT EXISTS app.search_logs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    session_id   text,
    user_id      bigint,
    question     text NOT NULL,
    status       text,
    tbl_id       text,
    slots        jsonb,
    candidates   jsonb,
    response     jsonb,
    hcx_raw      jsonb,
    timings      jsonb,
    elapsed_ms   int,
    error        text
)
"""


def upgrade() -> None:
    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "admin_id",
            sa.BigInteger(),
            sa.ForeignKey("app.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("admin_email", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("query", JSONB(), nullable=True),
        schema="app",
    )
    op.create_index("admin_audit_created", "admin_audit", ["created_at"], schema="app")

    op.execute(SEARCH_LOGS)
    op.execute("ALTER TABLE app.search_logs ADD COLUMN IF NOT EXISTS user_id bigint")
    op.execute(
        "UPDATE app.search_logs s SET user_id = NULL WHERE s.user_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM app.users u WHERE u.id = s.user_id)"
    )
    op.create_foreign_key(
        "search_logs_user_fk",
        "search_logs",
        "users",
        ["user_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_index("search_logs_created", "search_logs", ["created_at"], schema="app")
    op.create_index("search_logs_user", "search_logs", ["user_id"], schema="app")


def downgrade() -> None:
    op.drop_index("search_logs_user", table_name="search_logs", schema="app")
    op.drop_index("search_logs_created", table_name="search_logs", schema="app")
    op.drop_constraint("search_logs_user_fk", "search_logs", schema="app", type_="foreignkey")
    op.drop_index("admin_audit_created", table_name="admin_audit", schema="app")
    op.drop_table("admin_audit", schema="app")
