from decimal import Decimal
from functools import lru_cache
from typing import Any

import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings

SEARCH_PATH = "raw,mart,catalog,public"


class StatsNotLoadedError(RuntimeError):
    pass


class StatsTableMissing(StatsNotLoadedError):
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
            "cursor_factory": psycopg.ClientCursor,
        },
    )


def to_driver_sql(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


def plain(value: Any) -> Any:
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
