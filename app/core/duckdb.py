"""전처리 DB(kosis.duckdb) 읽기 전용 연결."""

from pathlib import Path
from typing import Any

import duckdb

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent.parent

CatalogException = duckdb.CatalogException

_con: duckdb.DuckDBPyConnection | None = None


class DuckDBNotFoundError(RuntimeError):
    pass


def db_path() -> Path:
    path = Path(get_settings().duckdb_path)
    return path if path.is_absolute() else ROOT / path


def get_connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        path = db_path()
        if not path.exists():
            raise DuckDBNotFoundError(f"{path}가 없습니다. 먼저 pipeline/run_all.py를 실행하세요.")
        _con = duckdb.connect(str(path), read_only=True)
    return _con


def rows(sql: str, params: Any = ()) -> list[dict[str, Any]]:
    # 요청마다 다른 스레드에서 호출되므로 연결을 공유하지 않고 cursor를 새로 만든다
    cur = get_connection().cursor()
    try:
        cur.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        cur.close()
