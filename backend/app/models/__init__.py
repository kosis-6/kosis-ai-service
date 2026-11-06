"""DB 테이블 정의. 구조를 바꾸면 마이그레이션을 만든다:
alembic revision --autogenerate -m "설명" → 생성된 파일 확인 → alembic upgrade head
"""

from app.models.base import Base
from app.models.catalog import StatCode, StatDimension, StatTable, StatUnit
from app.models.raw import StatLoad, StatValue

__all__ = ["Base", "StatCode", "StatDimension", "StatLoad", "StatTable", "StatUnit", "StatValue"]
