from typing import Any

import pandas as pd
import psycopg
from psycopg import sql
from sqlalchemy import make_url

from app.core.config import get_settings

BUILD_SCHEMA = "build"


def conninfo() -> str:
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def column_type(values: pd.Series) -> str:
    known = values.dropna()
    if known.empty:
        return "text"
    if pd.api.types.is_bool_dtype(known) or isinstance(known.iloc[0], bool):
        return "boolean"
    if pd.api.types.is_integer_dtype(known) or isinstance(known.iloc[0], int):
        return "bigint"
    if pd.api.types.is_float_dtype(known) or isinstance(known.iloc[0], float):
        return "double precision"
    return "text"


def plain_rows(frame: pd.DataFrame) -> list[list[Any]]:
    return frame.astype(object).where(frame.notna(), None).values.tolist()


class Result:
    def __init__(self, cursor: psycopg.Cursor) -> None:
        self.cursor = cursor

    def fetchall(self) -> list[tuple]:
        return self.cursor.fetchall()

    def df(self) -> pd.DataFrame:
        columns = [d[0] for d in self.cursor.description]
        return pd.DataFrame(self.cursor.fetchall(), columns=columns, dtype=object)


class Build:
    def __init__(self) -> None:
        self.conn = psycopg.connect(
            conninfo(), autocommit=True, options=f"-c search_path={BUILD_SCHEMA},public"
        )

    def reset(self) -> None:
        self.conn.execute(f"DROP SCHEMA IF EXISTS {BUILD_SCHEMA} CASCADE")
        self.conn.execute(f"CREATE SCHEMA {BUILD_SCHEMA}")

    def sql(self, query: str) -> Result:
        return Result(self.conn.execute(query))

    def execute(self, query: str, params: list[Any]) -> Result:
        return Result(self.conn.execute(query.replace("?", "%s"), params))

    def register(self, name: str, frame: pd.DataFrame, temp: bool = True) -> None:
        table = sql.Identifier(name)
        columns = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(column_type(frame[c])))
            for c in frame.columns
        )
        kind = sql.SQL("TEMP TABLE" if temp else "TABLE")
        self.conn.execute(sql.SQL("CREATE {} {} ({})").format(kind, table, columns))
        names = sql.SQL(", ").join(map(sql.Identifier, frame.columns))
        with (
            self.conn.cursor() as cur,
            cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(table, names)) as copy,
        ):
            for row in plain_rows(frame):
                copy.write_row(row)

    def unregister(self, name: str) -> None:
        self.conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))

    def copy_csv(self, table: str, columns: list[str], lines: Any) -> None:
        names = sql.SQL(", ").join(map(sql.Identifier, columns))
        query = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT csv, HEADER true)").format(
            sql.Identifier(table), names
        )
        with self.conn.cursor() as cur, cur.copy(query) as copy:
            for line in lines:
                copy.write(line)

    def close(self) -> None:
        self.conn.close()
