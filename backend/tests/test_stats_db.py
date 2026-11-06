from decimal import Decimal

import pandas as pd
import pytest

from app.core.stats_db import plain, to_driver_sql
from pipeline.stats_build import column_type, plain_rows


def test_to_driver_sql_keeps_percent_literal() -> None:
    sql = "select 1 from regions where name like '%시' and code = ? and year in (?, ?)"
    assert to_driver_sql(sql) == (
        "select 1 from regions where name like '%%시' and code = %s and year in (%s, %s)"
    )


@pytest.mark.parametrize(
    ("value", "expected", "kind"),
    [
        (Decimal("51234567"), 51234567, int),
        (Decimal("12.5"), 12.5, float),
        (3, 3, int),
        (None, None, type(None)),
        ("서울", "서울", str),
    ],
)
def test_plain_removes_decimal(value: object, expected: object, kind: type) -> None:
    got = plain(value)
    assert got == expected
    assert type(got) is kind


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["a", None], "text"),
        ([1, 2], "bigint"),
        ([None, 2030], "double precision"),  # 빈 값이 있으면 pandas가 실수로 읽는다
        ([1.5, None], "double precision"),
        ([True, False], "boolean"),
        ([None, None], "text"),
    ],
)
def test_column_type(values: list, expected: str) -> None:
    assert column_type(pd.Series(values)) == expected


def test_plain_rows_turns_nan_into_none() -> None:
    frame = pd.DataFrame({"a": [1.5, None], "b": ["x", None]})
    assert plain_rows(frame) == [[1.5, "x"], [None, None]]
