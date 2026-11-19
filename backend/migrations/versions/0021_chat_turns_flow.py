"""app.chat_turns.flow: 이 턴의 처리 흐름 (질문이 거친 단계와 순서)

관리자 화면의 대화 기록에서 턴마다 어떤 단계(질문 이해 · 사전 계산 · 통계표 찾기 ·
표 칸으로 계산 …)를 거쳐 그 답이 됐는지 본다 (app/services/flow.py).

- flow: [{"stage": 단계, "result": 결과, "detail": 덧붙일 말}, ...]. 이 칸을 만들기 전의 턴은 null

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_turns", sa.Column("flow", JSONB(), nullable=True), schema="app")


def downgrade() -> None:
    op.drop_column("chat_turns", "flow", schema="app")
