from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.load_raw import BIG_ROWS, dim_count, is_big, scan_csv, value_rows

HEADER = (
    "tbl_id,org_id,period,prd_se,item_id,item_nm,c1_id,c1_nm,c1_level,c1_path,"
    "value,value_flag,unit,last_changed\n"
)


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "T.csv"
    path.write_text(HEADER + body, encoding="utf-8-sig")
    return path


def test_scan_collects_totals_and_codes(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "T,101,2023,A,I1,인구,00,전국,1,전국,10.5,,명,2025-01-02\n"
        "T,101,2024,A,I1,인구,11,서울,2,전국 > 서울,,X,명,2025-03-04\n",
    )
    scan = scan_csv(path)
    assert scan.row_count == 2
    assert scan.value_sum == Decimal("10.5")
    assert (scan.first_period, scan.last_period) == (2023, 2024)
    assert scan.last_changed == date(2025, 3, 4)
    assert scan.codes[(0, "I1")] == ("인구", None, None)
    assert scan.codes[(1, "11")] == ("서울", 2, "전국 > 서울")
    assert scan.units == {"명"}


def test_same_item_may_have_many_units_and_blank(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "T,101,2023,A,I1,인구,00,전국,1,전국,1,,명,\n"
        "T,101,2024,A,I1,인구,00,전국,1,전국,2,,세대,\n"
        "T,101,2025,A,I1,인구,00,전국,1,전국,3,,,\n",
    )
    assert scan_csv(path).units == {"명", "세대"}
    rows = list(value_rows(path, 3, {(0, "I1"): 7, (1, "00"): 8}, {"명": 1, "세대": 2}))
    assert [r[8] for r in rows] == [1, 2, None]


def test_value_rows_uses_keys_and_keeps_missing_value_flag(tmp_path: Path) -> None:
    path = write(tmp_path, "T,101,2024,A,I1,인구,11,서울,2,전국 > 서울,,X,명,\n")
    keys = {(0, "I1"): 7, (1, "11"): 8}
    assert list(value_rows(path, 3, keys, {"명": 1})) == [
        (3, 2024, 7, 8, None, None, None, None, 1, "A", None, "X")
    ]


def test_dim_count_stops_above_limit() -> None:
    header = [f"c{i}_id" for i in range(1, 7)]
    with pytest.raises(SystemExit):
        dim_count(header)
    assert dim_count(header[:5]) == 5


def test_is_big_threshold() -> None:
    assert is_big(BIG_ROWS) and not is_big(BIG_ROWS - 1)
