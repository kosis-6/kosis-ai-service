"""전처리 결과(kosis.duckdb)를 서비스 DB(PostgreSQL)로 옮긴다.

DuckDB는 전처리·검증 도구로만 쓰고, 서버는 PostgreSQL만 읽는다 (app/core/stats_db.py).

  raw      facts                 KOSIS 수치
  mart     population, regions…  검증된 지표 계산에 쓰는 정리된 데이터, 지표 사전
  catalog  catalog, table_notes… 표 설명과 주의사항

표마다 `<표>__new`에 넣어 원본과 행 수·값 합계를 대조한 뒤, 한 트랜잭션에서 전부 바꾼다.
중간에 실패하면 기존 표가 그대로 남아 서비스가 계속 동작한다.

실행: python pipeline/run_all.py && python pipeline/load_stats.py
"""

import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb
import psycopg
from psycopg import sql
from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

DB_PATH = ROOT / os.environ.get("DUCKDB_PATH", "data/processed/kosis.duckdb")
BATCH = 50_000

TABLES = {
    "raw": ["facts"],
    "mart": [
        "population",
        "regions",
        "region_aliases",
        "region_changes",
        "region_code_map",
        "indicator_dict",
        "indicator_sources",
    ],
    "catalog": [
        "catalog",
        "table_notes",
        "table_groups",
        "compatible_groups",
        "series_breaks",
        "region_notes",
    ],
}

# 쿼리가 거르는 칸 순서에 맞춘 인덱스 (app/services/lookup.py, indicators.py)
INDEXES = {
    "facts": [
        ("tbl_id", "year", "region_code"),
        ("tbl_id", "item_id", "year"),
    ],
    "population": [
        ("sex", "region_code", "year"),
        ("year", "region_code"),
    ],
}

# 값을 합해 원본과 대조할 칸
CHECK_SUM = {"facts": "value", "population": "population"}

PG_TYPES = {
    "VARCHAR": "text",
    "BIGINT": "bigint",
    "INTEGER": "integer",
    "DOUBLE": "double precision",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
}


def pg_type(duck_type: str) -> str:
    """모르는 타입을 그냥 넘기면 값이 바뀔 수 있으므로 멈춘다"""
    if duck_type not in PG_TYPES:
        raise SystemExit(f"옮기는 방법을 정하지 않은 DuckDB 타입: {duck_type}")
    return PG_TYPES[duck_type]


def conninfo() -> str:
    """SQLAlchemy 주소(postgresql+psycopg://) → psycopg 주소"""
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def same_total(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9)


def copy_table(duck: duckdb.DuckDBPyConnection, pg: psycopg.Connection, schema: str, table: str):
    """<표>__new에 복사하고 인덱스를 만든 뒤 원본과 대조한다"""
    columns = duck.execute(f'describe "{table}"').fetchall()
    new = sql.Identifier(schema, f"{table}__new")
    names = [sql.Identifier(c[0]) for c in columns]
    defs = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(c[0]), sql.SQL(pg_type(c[1]))) for c in columns
    )
    pg.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(new))
    pg.execute(sql.SQL("CREATE TABLE {} ({})").format(new, defs))

    cursor = duck.execute(f'select * from "{table}"')
    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(new, sql.SQL(", ").join(names))
    with pg.cursor() as cur, cur.copy(copy_sql) as copy:
        while batch := cursor.fetchmany(BATCH):
            for row in batch:
                copy.write_row(row)

    for i, cols in enumerate(INDEXES.get(table, []), start=1):
        pg.execute(
            sql.SQL("CREATE INDEX {} ON {} ({})").format(
                sql.Identifier(f"{table}__new_{i}"),
                new,
                sql.SQL(", ").join(map(sql.Identifier, cols)),
            )
        )

    want = duck.execute(f'select count(*) from "{table}"').fetchone()[0]
    got = pg.execute(sql.SQL("SELECT count(*) FROM {}").format(new)).fetchone()[0]
    if got != want:
        raise SystemExit(f"{table}: 행 수가 다릅니다 (원본 {want}, 적재 {got})")
    if column := CHECK_SUM.get(table):
        want_sum = duck.execute(f'select sum("{column}")::double from "{table}"').fetchone()[0]
        got_sum = pg.execute(
            sql.SQL("SELECT sum({})::double precision FROM {}").format(sql.Identifier(column), new)
        ).fetchone()[0]
        if not same_total(want_sum, got_sum):
            raise SystemExit(f"{table}: 값 합계가 다릅니다 (원본 {want_sum}, 적재 {got_sum})")
    pg.execute(sql.SQL("ANALYZE {}").format(new))
    return want


def swap(pg: psycopg.Connection, schema: str, table: str) -> None:
    pg.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(schema, table)))
    pg.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(
            sql.Identifier(schema, f"{table}__new"), sql.Identifier(table)
        )
    )
    for i in range(1, len(INDEXES.get(table, [])) + 1):
        pg.execute(
            sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                sql.Identifier(schema, f"{table}__new_{i}"),
                sql.Identifier(f"{table}_idx_{i}"),
            )
        )


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH}가 없습니다. 먼저 python pipeline/run_all.py를 실행하세요.")
    built = datetime.fromtimestamp(DB_PATH.stat().st_mtime)
    start = time.perf_counter()
    counts: dict[tuple[str, str], int] = {}

    duck = duckdb.connect(str(DB_PATH), read_only=True)
    existing = {r[0] for r in duck.execute("show tables").fetchall()}
    expected = {t for tables in TABLES.values() for t in tables}
    if existing != expected:
        raise SystemExit(
            f"표 목록이 다릅니다. 없음 {expected - existing}, 모름 {existing - expected}"
        )

    with psycopg.connect(conninfo(), autocommit=True) as pg:
        for schema in TABLES:
            pg.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        pg.execute(
            """
            CREATE TABLE IF NOT EXISTS raw.load_log (
                table_schema text NOT NULL,
                table_name   text NOT NULL,
                row_count    bigint NOT NULL,
                source_built_at timestamp NOT NULL,
                loaded_at    timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for schema, tables in TABLES.items():
            for table in tables:
                counts[(schema, table)] = copy_table(duck, pg, schema, table)
                print(f"{schema}.{table}: {counts[(schema, table)]:,}행", flush=True)

        # 여기까지 실패하면 기존 표는 그대로다. 교체는 한 번에
        with pg.transaction():
            for (schema, table), count in counts.items():
                swap(pg, schema, table)
                pg.execute(
                    "INSERT INTO raw.load_log"
                    " (table_schema, table_name, row_count, source_built_at)"
                    " VALUES (%s, %s, %s, %s)",
                    [schema, table, count, built],
                )
    print(f"적재 완료: 표 {len(counts)}개, {time.perf_counter() - start:.1f}초")


if __name__ == "__main__":
    main()
