import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.stats_build import BUILD_SCHEMA, conninfo  # noqa: E402

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

INDEXES = {
    "facts": [("tbl_id", "year", "region_code"), ("tbl_id", "item_id", "year")],
    "population": [("sex", "region_code", "year"), ("year", "region_code")],
}


def main() -> None:
    start = time.perf_counter()
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        built = {
            r[0]
            for r in pg.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", [BUILD_SCHEMA]
            )
        }
        expected = {t for tables in TABLES.values() for t in tables}
        if built != expected:
            raise SystemExit(
                f"표 목록이 다릅니다. 없음 {expected - built}, 모름 {built - expected}"
            )

        for table, indexes in INDEXES.items():
            for i, cols in enumerate(indexes, start=1):
                pg.execute(
                    sql.SQL("CREATE INDEX {} ON {} ({})").format(
                        sql.Identifier(f"{table}_idx_{i}"),
                        sql.Identifier(BUILD_SCHEMA, table),
                        sql.SQL(", ").join(map(sql.Identifier, cols)),
                    )
                )
        for table in sorted(expected):
            pg.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(BUILD_SCHEMA, table)))
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

        with pg.transaction():
            for schema, tables in TABLES.items():
                for table in tables:
                    old = sql.Identifier(schema, table)
                    new = sql.Identifier(BUILD_SCHEMA, table)
                    count = pg.execute(sql.SQL("SELECT count(*) FROM {}").format(new)).fetchone()[0]
                    pg.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(old))
                    pg.execute(
                        sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(new, sql.Identifier(schema))
                    )
                    pg.execute(
                        "INSERT INTO raw.load_log"
                        " (table_schema, table_name, row_count, source_built_at)"
                        " VALUES (%s, %s, %s, now())",
                        [schema, table, count],
                    )
                    print(f"{schema}.{table}: {count:,}행", flush=True)
            pg.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(BUILD_SCHEMA)))
    print(f"교체 완료: {time.perf_counter() - start:.1f}초")


if __name__ == "__main__":
    main()
