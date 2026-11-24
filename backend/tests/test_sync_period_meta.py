import os

import psycopg
import pytest

from pipeline.load_raw import sync_period_meta

pytestmark = pytest.mark.needs_data

TBL = "DT_1MB2081"


@pytest.fixture
def pg():
    con = psycopg.connect(os.environ["DATABASE_URL"].replace("+psycopg", ""))
    try:
        yield con
    finally:
        con.rollback()
        con.close()


def loaded(pg):
    r = pg.execute(
        "select s.tbl_key, s.first_period, s.last_period from raw.stat_load s"
        " join catalog.stat_table t using (tbl_key) where t.tbl_id = %s",
        [TBL],
    ).fetchone()
    if r is None:
        pytest.skip(f"{TBL} 적재 안 됨")
    key, first, last = r
    return key, (first // 100 if first > 9999 else first), (last // 100 if last > 9999 else last)


def put(pg, start, end, edition):
    pg.execute(
        "update catalog.table_meta set start_year = %s, end_year = %s where tbl_id = %s",
        [start, end, TBL],
    )
    pg.execute("update catalog.table_families set edition = %s where tbl_id = %s", [edition, TBL])


def meta(pg):
    return pg.execute(
        "select m.start_year, m.end_year, f.edition from catalog.table_meta m"
        " join catalog.table_families f using (tbl_id) where m.tbl_id = %s",
        [TBL],
    ).fetchone()


def test_widens_when_values_are_newer(pg):
    key, lo, hi = loaded(pg)
    put(pg, lo, hi - 1, f"{lo}~{hi - 1}")
    sync_period_meta(pg, key)
    assert meta(pg) == (lo, hi, f"{lo}~{hi}")


def test_same_result_when_called_twice(pg):
    key, lo, hi = loaded(pg)
    put(pg, lo, hi - 1, f"{lo}~{hi - 1}")
    sync_period_meta(pg, key)
    sync_period_meta(pg, key)
    assert meta(pg) == (lo, hi, f"{lo}~{hi}")


def test_keeps_when_values_are_shorter(pg):
    key, lo, hi = loaded(pg)
    put(pg, lo - 2, hi + 1, f"{lo - 2}~{hi + 1}")
    sync_period_meta(pg, key)
    assert meta(pg) == (lo - 2, hi + 1, f"{lo - 2}~{hi + 1}")


def test_keeps_edition_of_other_shape(pg):
    key, lo, hi = loaded(pg)
    put(pg, lo, hi - 1, "특별판")
    sync_period_meta(pg, key)
    assert meta(pg) == (lo, hi, "특별판")
