"""수집한 표(CSV)를 숫자 키 구조로 PostgreSQL에 넣는다 (초안). 구조: app/models, migrations

조회 지표(app/services/lookup.py)가 이 수치를 읽는다. run_all.py가 검증·교체 뒤 마지막에 실행한다.

표 하나씩 처리한다: 임시 테이블에 COPY → 중복 검사 → CSV와 행 수·값 합계 대조 → 인덱스 → 교체.
DB에 고유 인덱스가 없으므로(용량) 이 중복 검사가 중복을 막는 유일한 장치다.
중간에 실패해도 그 표의 기존 값은 그대로 남고, 끝낸 표는 다시 처리하지 않아도 된다.

  큰 표(BIG_ROWS 이상)  전용 파티션을 만들어 통째로 교체. 지우고 넣지 않아 빈 공간이 쌓이지 않는다
  작은 표               기본 파티션의 해시 묶음에서 지우고 넣기

실행: alembic upgrade head                          테이블이 없으면 먼저
      python pipeline/load_raw.py                      data/processed(KOSIS_RAW_DIR)의 모든 표
      python pipeline/load_raw.py DT_1B04006 DT_1MR2060  이 표만
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.stats_build import conninfo  # noqa: E402

# run_all.py가 원본 폴더를 넘길 때 환경변수를 쓴다 (절대 경로면 그대로)
CSV_DIR = ROOT / os.environ.get("KOSIS_RAW_DIR", "data/processed")
BIG_ROWS = 1_000_000
MAX_DIMS = 5
VALUE_COLUMNS = (
    "tbl_key", "period", "item_key", "c1", "c2", "c3", "c4", "c5",
    "unit_key", "prd_se", "value", "flag",
)  # fmt: skip


# 칸 하나를 정하는 값. 이 조합이 겹치면 중복이다
CELL_KEY = ("item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5")


@dataclass
class Scan:
    """CSV를 한 번 읽어 얻은 코드 목록과 대조값"""

    row_count: int = 0
    value_sum: Decimal = Decimal(0)
    first_period: int | None = None
    last_period: int | None = None
    last_changed: date | None = None
    # (칸 번호, 코드) → (이름, 단계, 경로). 칸 번호 0은 항목
    codes: dict[tuple[int, str], tuple[str, int | None, str | None]] = field(default_factory=dict)
    units: set[str] = field(default_factory=set)


def dim_count(header: list[str]) -> int:
    """CSV 머리글의 분류 칸 수. 구조가 담지 못하면 값을 버리지 않고 멈춘다"""
    n = sum(1 for c in header if c[0] == "c" and c.endswith("_id"))
    if n > MAX_DIMS:
        raise SystemExit(f"분류가 {n}개입니다. raw.stat_value는 {MAX_DIMS}개까지 담습니다.")
    return n


def duplicate_message(name: str, found: list[tuple]) -> str:
    """중복 칸 예시. 행은 CELL_KEY 순서의 값 + 횟수"""
    lines = [
        ", ".join(f"{c}={v}" for c, v in zip(CELL_KEY, r[:-1], strict=True) if v is not None)
        + f" ({r[-1]}번)"
        for r in found
    ]
    return f"{name}: 같은 칸이 중복되어 교체하지 않습니다. 예시: " + " / ".join(lines)


def is_big(row_count: int) -> bool:
    return row_count >= BIG_ROWS


def scan_csv(path: Path) -> Scan:
    scan = Scan()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        dims = dim_count(reader.fieldnames or [])
        for r in reader:
            scan.row_count += 1
            if r["value"]:
                scan.value_sum += Decimal(r["value"])
            period = int(r["period"])
            if scan.first_period is None or period < scan.first_period:
                scan.first_period = period
            if scan.last_period is None or period > scan.last_period:
                scan.last_period = period
            if r["last_changed"]:
                changed = date.fromisoformat(r["last_changed"])
                if scan.last_changed is None or changed > scan.last_changed:
                    scan.last_changed = changed
            if r["unit"]:
                scan.units.add(r["unit"])
            scan.codes[(0, r["item_id"])] = (r["item_nm"], None, None)
            for d in range(1, dims + 1):
                level = r[f"c{d}_level"]
                scan.codes[(d, r[f"c{d}_id"])] = (
                    r[f"c{d}_nm"],
                    int(level) if level else None,
                    r[f"c{d}_path"] or None,
                )
    return scan


def value_rows(path: Path, tbl_key: int, keys: dict[tuple[int, str], int], units: dict[str, int]):
    """CSV 행 → raw.stat_value 행"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        dims = dim_count(reader.fieldnames or [])
        for r in reader:
            classes = [keys[(d, r[f"c{d}_id"])] for d in range(1, dims + 1)]
            classes += [None] * (MAX_DIMS - dims)
            yield (
                tbl_key,
                int(r["period"]),
                keys[(0, r["item_id"])],
                *classes,
                units.get(r["unit"]),
                r["prd_se"],
                r["value"] or None,
                r["value_flag"] or None,
            )


def save_codes(pg: psycopg.Connection, path: Path, scan: Scan) -> tuple[int, dict, dict]:
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    row = pg.execute(
        """
        INSERT INTO catalog.stat_table (org_id, tbl_id, tbl_nm) VALUES (%s, %s, %s)
        ON CONFLICT (tbl_id) DO UPDATE SET org_id = EXCLUDED.org_id, tbl_nm = EXCLUDED.tbl_nm
        RETURNING tbl_key
        """,
        [meta["org_id"], meta["tbl_id"], meta["tbl_nm"]],
    ).fetchone()
    tbl_key = row[0]
    for no, (_, name) in enumerate(sorted(meta["classes"].items()), start=1):
        pg.execute(
            """
            INSERT INTO catalog.stat_dimension (tbl_key, dim_no, name) VALUES (%s, %s, %s)
            ON CONFLICT (tbl_key, dim_no) DO UPDATE SET name = EXCLUDED.name
            """,
            [tbl_key, no, name],
        )
    keys = {}
    for (dim, code), (name, level, code_path) in scan.codes.items():
        keys[(dim, code)] = pg.execute(
            """
            INSERT INTO catalog.stat_code (tbl_key, dim_no, code, name, level, path)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tbl_key, dim_no, code) DO UPDATE SET
                name = EXCLUDED.name, level = EXCLUDED.level, path = EXCLUDED.path
            RETURNING code_key
            """,
            [tbl_key, dim, code, name, level, code_path],
        ).fetchone()[0]
    units = {}
    for unit in scan.units:
        pg.execute(
            "INSERT INTO catalog.stat_unit (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", [unit]
        )
        units[unit] = pg.execute(
            "SELECT unit_key FROM catalog.stat_unit WHERE name = %s", [unit]
        ).fetchone()[0]
    return tbl_key, keys, units


def copy_rows(pg: psycopg.Connection, target: sql.Composable, rows) -> None:
    columns = sql.SQL(", ").join(map(sql.Identifier, VALUE_COLUMNS))
    with (
        pg.cursor() as cur,
        cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, columns)) as copy,
    ):
        for row in rows:
            copy.write_row(row)


def check_duplicates(pg: psycopg.Connection, target: sql.Composable, name: str) -> None:
    """같은 칸이 두 번 들어오면 합계가 틀어지므로 교체하지 않는다. GROUP BY는 NULL끼리 같게 본다"""
    query = sql.SQL(
        "SELECT {key}, count(*) FROM {target} GROUP BY {key} HAVING count(*) > 1 LIMIT 5"
    )
    key = sql.SQL(", ").join(map(sql.Identifier, CELL_KEY))
    found = pg.execute(query.format(key=key, target=target)).fetchall()
    if found:
        raise SystemExit(duplicate_message(name, found))


def check_loaded(pg: psycopg.Connection, target: sql.Composable, scan: Scan, name: str) -> None:
    count, total = pg.execute(
        sql.SQL("SELECT count(*), coalesce(sum(value), 0) FROM {}").format(target)
    ).fetchone()
    if count != scan.row_count:
        raise SystemExit(f"{name}: 행 수가 다릅니다 (CSV {scan.row_count}, 적재 {count})")
    if total != scan.value_sum:
        raise SystemExit(f"{name}: 값 합계가 다릅니다 (CSV {scan.value_sum}, 적재 {total})")


def replace_big(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> None:
    """전용 파티션을 새로 만들어 대조한 뒤 기존 파티션과 맞바꾼다"""
    part = f"stat_value_t{tbl_key}"
    new = sql.Identifier("raw", f"{part}__new")
    pg.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(new))
    pg.execute(
        sql.SQL(
            "CREATE TABLE {} (LIKE raw.stat_value INCLUDING DEFAULTS, CHECK (tbl_key = {}))"
        ).format(new, sql.Literal(tbl_key))
    )
    copy_rows(pg, new, rows)
    check_duplicates(pg, new, name)
    check_loaded(pg, new, scan, name)
    # 부모의 stat_value_lookup과 같은 칸이어야 붙일 때 그 인덱스의 일부가 된다
    pg.execute(sql.SQL("CREATE INDEX ON {} (tbl_key, item_key, period, c1)").format(new))
    pg.execute(sql.SQL("ANALYZE {}").format(new))
    old = sql.Identifier("raw", part)
    with pg.transaction():
        # 전에 작은 표였으면 기본 파티션에 남은 행을 지운다 (붙일 때 겹치면 실패한다)
        pg.execute("DELETE FROM raw.stat_value_small WHERE tbl_key = %s", [tbl_key])
        if pg.execute("SELECT to_regclass(%s)", [f"raw.{part}"]).fetchone()[0]:
            pg.execute(sql.SQL("DROP TABLE {}").format(old))
        pg.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(new, sql.Identifier(part)))
        pg.execute(
            sql.SQL("ALTER TABLE raw.stat_value ATTACH PARTITION {} FOR VALUES IN ({})").format(
                old, sql.Literal(tbl_key)
            )
        )


def replace_small(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> None:
    with pg.transaction():
        pg.execute(
            "CREATE TEMP TABLE new_rows (LIKE raw.stat_value INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        target = sql.Identifier("new_rows")
        copy_rows(pg, target, rows)
        check_duplicates(pg, target, name)
        check_loaded(pg, target, scan, name)
        pg.execute("DELETE FROM raw.stat_value WHERE tbl_key = %s", [tbl_key])
        part = sql.Identifier("raw", f"stat_value_t{tbl_key}")
        if pg.execute("SELECT to_regclass(%s)", [f"raw.stat_value_t{tbl_key}"]).fetchone()[0]:
            pg.execute(sql.SQL("DROP TABLE {}").format(part))  # 전에 큰 표였으면 파티션을 없앤다
        pg.execute("INSERT INTO raw.stat_value SELECT * FROM new_rows")


def load_table(pg: psycopg.Connection, path: Path) -> None:
    start = time.perf_counter()
    scan = scan_csv(path)
    tbl_key, keys, units = save_codes(pg, path, scan)
    rows = value_rows(path, tbl_key, keys, units)
    big = is_big(scan.row_count)
    (replace_big if big else replace_small)(pg, tbl_key, rows, scan, path.stem)
    pg.execute(
        """
        INSERT INTO raw.stat_load (tbl_key, row_count, value_sum, first_period, last_period,
                                   last_changed, is_big, source_file)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tbl_key) DO UPDATE SET
            row_count = EXCLUDED.row_count, value_sum = EXCLUDED.value_sum,
            first_period = EXCLUDED.first_period, last_period = EXCLUDED.last_period,
            last_changed = EXCLUDED.last_changed, is_big = EXCLUDED.is_big,
            source_file = EXCLUDED.source_file, loaded_at = now()
        """,
        [
            tbl_key,
            scan.row_count,
            scan.value_sum,
            scan.first_period,
            scan.last_period,
            scan.last_changed,
            big,
            path.name,
        ],
    )
    kind = "전용 파티션" if big else "기본 파티션"
    print(
        f"{path.stem}: {scan.row_count:,}행, 코드 {len(keys):,}개, {kind},"
        f" {time.perf_counter() - start:.1f}초",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="수집한 표를 raw.stat_value에 적재")
    parser.add_argument("tables", nargs="*", help="표 ID (없으면 data/processed 전체)")
    args = parser.parse_args()
    paths = (
        [CSV_DIR / f"{t}.csv" for t in args.tables]
        if args.tables
        else sorted(CSV_DIR.glob("*.csv"))
    )
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        if pg.execute("SELECT to_regclass('raw.stat_value')").fetchone()[0] is None:
            raise SystemExit("raw.stat_value가 없습니다. 먼저 alembic upgrade head를 실행하세요.")
        for path in paths:
            load_table(pg, path)


if __name__ == "__main__":
    main()
