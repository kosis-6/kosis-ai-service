"""raw.stat_value.prd_se를 CHAR(1)에서 VARCHAR(2)로: 부정기 주기는 KOSIS가 'IR'로 보낸다

인구 통계표 2,364개 중 부정기 주기 표 29개(성씨·본관 등)는 수치 행의 PRD_SE가 'IR'이라
CHAR(1)에 들어가지 않아 적재가 실패했다 (StringDataRightTruncation).

파티션 테이블이라 상위 테이블을 바꾸면 모든 파티션이 함께 바뀐다. 칸 형식이 바뀌어 테이블을
다시 쓰므로 이미 적재된 행이 많으면 오래 걸린다.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "stat_value",
        "prd_se",
        schema="raw",
        type_=sa.String(2),
        existing_type=sa.CHAR(1),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 'IR' 행이 남아 있으면 되돌릴 수 없다 (부정기 표를 먼저 지워야 함)
    op.alter_column(
        "stat_value",
        "prd_se",
        schema="raw",
        type_=sa.CHAR(1),
        existing_type=sa.String(2),
        existing_nullable=False,
    )
