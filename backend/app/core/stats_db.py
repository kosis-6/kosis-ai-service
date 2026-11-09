"""통계 수치 DB(PostgreSQL) 읽기 전용 조회.

pipeline/run_all.py가 적재한 raw·mart·catalog 스키마를 읽는다. search_path로 스키마를 잡으므로
쿼리에는 스키마 이름을 쓰지 않는다 (`select ... from facts`).

연결은 세션 기본값을 읽기 전용으로 열어, 서버의 답변 경로에서 수치를 고칠 수 없게 한다.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Any

import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings

SEARCH_PATH = "raw,mart,catalog,public"


class StatsNotLoadedError(RuntimeError):
    """통계 DB에 연결할 수 없거나 아직 적재되지 않았다"""


class StatsTableMissing(StatsNotLoadedError):
    """조회한 테이블이 없다. tbl_id가 있으면 그 통계표의 수치가 아직 적재되지 않은 것
    (qa.ask가 적재 대기열에 넣고 data_loading으로 답한다)"""

    def __init__(self, message: str, tbl_id: str | None = None) -> None:
        super().__init__(message)
        self.tbl_id = tbl_id


@lru_cache
def _engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c search_path={SEARCH_PATH} -c default_transaction_read_only=on",
            # 값을 클라이언트에서 넣어 coalesce(NULL, 999)처럼 타입을 알 수 없는 인자도 동작한다
            "cursor_factory": psycopg.ClientCursor,
        },
    )


def to_driver_sql(sql: str) -> str:
    """`?` 자리표시자 → psycopg 형식. SQL 안의 % (like '%시')는 글자로 남긴다"""
    return sql.replace("%", "%%").replace("?", "%s")


def plain(value: Any) -> Any:
    """PostgreSQL sum(bigint)은 Decimal을 돌려준다. float와 계산할 수 있게 int·float로 바꾼다"""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def rows(sql: str, params: Any = ()) -> list[dict[str, Any]]:
    try:
        conn = _engine().raw_connection()
    except OperationalError as e:
        raise StatsNotLoadedError(f"통계 DB에 연결할 수 없습니다: {e}") from e
    try:
        with conn.cursor() as cur:
            cur.execute(to_driver_sql(sql), list(params))
            cols = [d[0] for d in cur.description]
            return [{c: plain(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]
    except psycopg.errors.UndefinedTable as e:
        raise StatsTableMissing(
            f"통계 테이블이 없습니다. 먼저 python pipeline/run_all.py를 실행하세요: {e}"
        ) from e
    finally:
        conn.close()
