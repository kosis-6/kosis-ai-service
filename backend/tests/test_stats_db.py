from decimal import Decimal

import pytest

from app.core.stats_db import plain, to_driver_sql
from pipeline.load_stats import pg_type, same_total


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


def test_pg_type_stops_on_unknown_type() -> None:
    assert pg_type("BIGINT") == "bigint"
    with pytest.raises(SystemExit):
        pg_type("HUGEINT")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (1.0, 1.0, True),
        (1e12, 1e12 + 0.001, True),
        (1.0, 1.1, False),
        (None, None, True),
        (1.0, None, False),
    ],
)
def test_same_total(a: float | None, b: float | None, expected: bool) -> None:
    assert same_total(a, b) is expected
