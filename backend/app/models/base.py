from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """모든 ORM 모델의 부모. migrations/env.py가 이 metadata와 DB를 비교한다"""
