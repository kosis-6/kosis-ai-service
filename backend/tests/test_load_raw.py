from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import StatValue
from pipeline.load_raw import (
    BIG_ROWS,
    CELL_KEY,
    VALUE_COLUMNS,
    UnitFixes,
    dim_count,
    duplicate_message,
    is_big,
    load_unit_fixes,
    row_unit,
    scan_csv,
    value_rows,
)

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


def test_copy_columns_match_model() -> None:
    assert set(VALUE_COLUMNS) == {c.name for c in StatValue.__table__.columns}


def test_cell_key_covers_every_column_that_identifies_a_cell() -> None:
    # tbl_key는 표마다 따로 검사하므로 빠지고, 값·부호·단위는 칸을 정하지 않는다
    assert set(CELL_KEY) == set(VALUE_COLUMNS) - {"tbl_key", "unit_key", "value", "flag"}


def test_duplicate_message_shows_cell_and_count() -> None:
    message = duplicate_message("T", [(7, 2024, "A", 8, None, None, None, None, 2)])
    assert message.startswith("T: 같은 칸이 중복")
    assert "item_key=7, period=2024, prd_se=A, c1=8 (2번)" in message
    assert "c2" not in message


def test_is_big_threshold() -> None:
    assert is_big(BIG_ROWS) and not is_big(BIG_ROWS - 1)


# ---- 합쳐진 단위 나누기 (config/unit_fixes.toml) ------------------------------------------

MERGED = "명 가임여자 1명당 명"
FIXES = UnitFixes(
    frozenset({MERGED, "명 건"}),
    {"T": {"T2": "가임여자 1명당 명"}, "G": {"c1:12": "가임여자 1명당 명"}, "D": {"*": "기본"}},
)


def test_merged_unit_is_split_per_item_and_others_are_kept(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        f"T,101,2024,A,T1,출생아수(명),00,전국,1,전국,238317,,{MERGED},\n"
        f"T,101,2024,A,T2,합계출산율,00,전국,1,전국,0.748,,{MERGED},\n"
        "T,101,2024,A,T3,인구,00,전국,1,전국,5,,명,\n",
    )
    scan = scan_csv(path, FIXES)
    assert scan.units == {"명", "가임여자 1명당 명"}  # 합쳐진 단위는 DB에 들어가지 않는다
    assert (scan.fixed_units, scan.unsplit) == (2, set())
    keys = {(0, "T1"): 1, (0, "T2"): 2, (0, "T3"): 3, (1, "00"): 9}
    rows = list(value_rows(path, 3, keys, {"명": 10, "가임여자 1명당 명": 11}, FIXES))
    assert [r[8] for r in rows] == [10, 11, 10]


@pytest.mark.parametrize(
    ("tbl", "item", "c1", "unit"),
    [
        ("G", "T1", ("10", "출생아수(명)"), "명"),  # 칸 이름 괄호의 단위
        ("G", "T1", ("11", "조출생률(천명당)"), "천명당"),
        ("G", "T1", ("12", "합계출산율"), "가임여자 1명당 명"),  # 표의 칸별 단위 (분류 칸)
        ("G", "T1", ("23", "일반혼인율(남편)"), "명 건"),  # 괄호가 단위가 아님 → 못 나눔
        ("D", "T5", ("00", "전국"), "기본"),  # 표의 기본값
    ],
)
def test_row_unit_order(tbl: str, item: str, c1: tuple[str, str], unit: str) -> None:
    r = {"unit": "명 건", "item_id": item, "item_nm": "항목", "c1_id": c1[0], "c1_nm": c1[1]}
    got, ok = row_unit(r, 1, tbl, FIXES)
    assert got == unit and ok is (unit != "명 건")


def test_unsplit_merged_unit_is_reported(tmp_path: Path) -> None:
    path = write(tmp_path, "T,101,2024,A,I1,무엇,00,전국,1,전국,1,,명 건,\n")
    scan = scan_csv(path, FIXES)
    assert scan.units == {"명 건"} and scan.unsplit == {("명 건", "무엇 / 전국")}


def test_unit_fixes_file_covers_the_known_tables() -> None:
    fixes = load_unit_fixes()
    assert len(fixes.merged) == 4
    assert set(fixes.tables) == {"DT_1B8000G", "DT_1B81A23", "DT_1B81A21", "DT_1B81A17"}
