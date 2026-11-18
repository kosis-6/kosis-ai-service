"""app.dynamic_plans.words: 저장한 칸을 고른 근거가 된 질문 낱말

칸 고르기 LLM(app/services/cell_pick.py)이 고른 칸은 그 질문의 말 때문에 고른 것이다
(예: 질문의 "결혼"을 표의 "혼인"으로). 지표 표현만 같은 다른 질문에 그 칸을 그대로 쓰면
새 질문이 묻지 않은 칸으로 계산하게 된다. 그래서 근거 낱말을 함께 남기고, 다시 쓸 때
그 낱말이 새 질문에도 있을 때만 그 칸을 쓴다 (dynamic_indicator.reusable_forced).

- words: [[질문 낱말, 표 낱말], ...] (cell_pick의 read_as). 규칙으로 고른 칸만 있으면 []

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dynamic_plans",
        sa.Column("words", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("dynamic_plans", "words", schema="app")
