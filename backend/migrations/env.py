"""Alembic 실행 환경. app/models의 정의와 DB를 비교한다.

아직 모델로 옮기지 않은 테이블(public의 검색·대화 테이블, load_stats.py가 만드는 표)과
하위 파티션은 비교에서 뺀다. 모델에 없다고 지우는 마이그레이션이 만들어지면 안 된다.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.models import Base

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

MANAGED_SCHEMAS = {"app", "catalog", "raw"}


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    return True


def include_object(obj, name, type_, reflected, compare_to):
    # DB에만 있는 테이블은 모델로 관리하지 않는 테이블이다
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def run() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            include_schemas=True,
            include_name=include_name,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("오프라인 모드는 쓰지 않습니다. DB에 연결해 실행하세요.")
run()
