"""수집한 표(CSV)를 숫자 키 구조로 PostgreSQL에 넣는다 (초안). 구조: app/models, migrations

조회 지표(app/services/lookup.py)가 이 수치를 읽는다. run_all.py가 검증·교체 뒤 마지막에 실행한다.

표 하나씩 처리한다: 임시 테이블에 COPY → 중복 검사 → CSV와 행 수·값 합계 대조 → 인덱스 → 교체.
DB에 고유 인덱스가 없으므로(용량) 이 중복 검사가 중복을 막는 유일한 장치다.
중간에 실패해도 그 표의 기존 값은 그대로 남고, 끝낸 표는 다시 처리하지 않아도 된다.

  큰 표(BIG_ROWS 이상)  전용 파티션을 만들어 통째로 교체. 지우고 넣지 않아 빈 공간이 쌓이지 않는다
  작은 표               기본 파티션의 해시 묶음에서 지우고 넣기

갱신(일부만 받은 자료, merge=True)은 기존 표에 덧붙인다 (merge_into)
  새 칸은 넣고, 이미 있던 칸은 새 값으로 바꾸며 값이 달랐으면 예전 값을 이력에 남긴다.
  기존 행을 복사하지 않아 큰 표(수천만 행)도 받은 만큼만 일한다

표 전체를 다시 넣을 때도 기존 값은 지우지 않는다 (carry_missing, archive_changed, archive_dropped)
  새 자료에 없는 시점의 칸  예) KOSIS가 뺀 예전 시점. 새 표에 그대로 옮겨 질문에 계속 답한다
  같은 시점에서 빠진 칸     예) 코드가 바뀐 지역(강원 42 → 51). 옮기면 두 번 세므로 이력에만 남긴다
  값이 바뀐 칸              새 값으로 답하고, 예전 값은 raw.stat_value_history에 남긴다
  CSV와의 행 수·합계 대조는 옮기기 전(새 자료만)에 한다.
  raw.stat_load에는 옮긴 칸까지 센 값을 적는다

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


# 같은 칸인지: 분류가 없는 칸(NULL)끼리도 같게 본다. 키는 양수라 -1과 겹치지 않는다.
# IS NOT DISTINCT FROM은 해시 조인을 못 써 큰 표에서 느려서 coalesce로 맞춘다
SAME_CELL = sql.SQL(" AND ").join(
    [sql.SQL("n.{c} = o.{c}").format(c=sql.Identifier(c)) for c in ("item_key", "period", "prd_se")]
    + [
        sql.SQL("coalesce(n.{c}, -1) = coalesce(o.{c}, -1)").format(c=sql.Identifier(c))
        for c in ("c1", "c2", "c3", "c4", "c5")
    ]
)


@dataclass
class Kept:
    """다시 적재할 때 남긴 기존 값"""

    carried: int = 0  # 새 자료에 없어 그대로 옮긴 칸
    carried_sum: Decimal = Decimal(0)
    first_period: int | None = None
    last_period: int | None = None
    archived: int = 0  # 값이 바뀌어 이력으로 보낸 예전 값
    dropped: int = 0  # 새 자료의 같은 시점에서 빠진 칸 (코드가 바뀐 지역 등). 이력으로만 남긴다


def _old(tbl_key: int) -> sql.Composable:
    return sql.SQL("(SELECT * FROM raw.stat_value WHERE tbl_key = {}) o").format(
        sql.Literal(tbl_key)
    )


_NAMES = sql.SQL(", ").join(map(sql.Identifier, VALUE_COLUMNS))
_OLD_COLS = sql.SQL(", ").join(sql.SQL("o.{}").format(sql.Identifier(c)) for c in VALUE_COLUMNS)


def archive_changed(
    pg: psycopg.Connection, new: sql.Composable, tbl_key: int, old: sql.Composable | None = None
) -> int:
    """값이 바뀐 칸의 예전 값 → raw.stat_value_history. 교체와 같은 트랜잭션에서 부른다
    (교체가 실패하면 이력도 남지 않게). old는 비교할 기존 행 (기본: 그 표 전체). 보낸 행 수"""
    return pg.execute(
        sql.SQL(
            "INSERT INTO raw.stat_value_history ({names}) SELECT {cols} FROM {old} JOIN {new} n"
            " ON {same} WHERE o.value IS DISTINCT FROM n.value OR o.flag IS DISTINCT FROM n.flag"
            " OR o.unit_key IS DISTINCT FROM n.unit_key"
        ).format(names=_NAMES, cols=_OLD_COLS, old=old or _old(tbl_key), new=new, same=SAME_CELL)
    ).rowcount


def _new_periods(new: sql.Composable) -> sql.Composable:
    """새 자료에 있는 시점 (주기와 함께)"""
    return sql.SQL("(o.prd_se, o.period) IN (SELECT DISTINCT prd_se, period FROM {})").format(new)


def carry_missing(pg: psycopg.Connection, new: sql.Composable, tbl_key: int) -> Kept:
    """새 자료에 없는 시점의 기존 칸 → new에 옮겨 넣는다 (교체 뒤에도 남아 질문에 답한다).
    예) KOSIS가 수록 기간에서 뺀 예전 시점.
    새 자료에 있는 시점에서 빠진 칸은 옮기지 않는다 (archive_dropped가 이력으로만 남긴다):
    KOSIS가 지역 코드를 바꾸면(강원 42 → 51) 예전 코드 칸이 새 자료에서 빠지는데,
    그것을 옮기면 같은 지역·시점이 두 코드로 두 번 들어가 합계·순위가 틀어진다.
    새 자료를 CSV와 대조한 뒤에 부른다. 처음 적재하는 표는 기존 칸이 없어 아무것도 하지 않는다"""
    pg.execute("DROP TABLE IF EXISTS pg_temp.carried")
    pg.execute(
        sql.SQL("CREATE TEMP TABLE carried AS SELECT {cols} FROM {old} WHERE NOT {periods}").format(
            cols=_OLD_COLS, old=_old(tbl_key), periods=_new_periods(new)
        )
    )
    count, total, first, last = pg.execute(
        "SELECT count(*), coalesce(sum(value), 0), min(period), max(period) FROM pg_temp.carried"
    ).fetchone()
    if count:
        pg.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM pg_temp.carried").format(
            new, _NAMES, _NAMES
        ))  # fmt: skip
    pg.execute("DROP TABLE pg_temp.carried")
    return Kept(count, total, first, last)


def archive_dropped(pg: psycopg.Connection, new: sql.Composable, tbl_key: int) -> int:
    """새 자료의 같은 시점에서 빠진 기존 칸 → raw.stat_value_history (교체와 같은 트랜잭션).
    carry_missing이 옮기지 않은 칸이다. 지우기 전에 예전 값을 이력에 남긴다. 보낸 행 수"""
    return pg.execute(
        sql.SQL(
            "INSERT INTO raw.stat_value_history ({names}) SELECT {cols} FROM {old}"
            " WHERE {periods} AND NOT EXISTS (SELECT 1 FROM {new} n WHERE {same})"
        ).format(
            names=_NAMES,
            cols=_OLD_COLS,
            old=_old(tbl_key),
            periods=_new_periods(new),
            new=new,
            same=SAME_CELL,
        )
    ).rowcount


def replace_big(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> Kept:
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
    kept = carry_missing(pg, new, tbl_key)
    # 부모의 stat_value_lookup과 같은 칸이어야 붙일 때 그 인덱스의 일부가 된다
    pg.execute(sql.SQL("CREATE INDEX ON {} (tbl_key, item_key, period, c1)").format(new))
    pg.execute(sql.SQL("ANALYZE {}").format(new))
    old = sql.Identifier("raw", part)
    with pg.transaction():
        kept.archived = archive_changed(pg, new, tbl_key)
        kept.dropped = archive_dropped(pg, new, tbl_key)
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
    return kept


def replace_small(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> Kept:
    with pg.transaction():
        pg.execute(
            "CREATE TEMP TABLE new_rows (LIKE raw.stat_value INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        target = sql.Identifier("new_rows")
        copy_rows(pg, target, rows)
        check_duplicates(pg, target, name)
        check_loaded(pg, target, scan, name)
        kept = carry_missing(pg, target, tbl_key)
        kept.archived = archive_changed(pg, target, tbl_key)
        kept.dropped = archive_dropped(pg, target, tbl_key)
        pg.execute("DELETE FROM raw.stat_value WHERE tbl_key = %s", [tbl_key])
        part = sql.Identifier("raw", f"stat_value_t{tbl_key}")
        if pg.execute("SELECT to_regclass(%s)", [f"raw.stat_value_t{tbl_key}"]).fetchone()[0]:
            pg.execute(sql.SQL("DROP TABLE {}").format(part))  # 전에 큰 표였으면 파티션을 없앤다
        pg.execute("INSERT INTO raw.stat_value SELECT * FROM new_rows")
    return kept


MERGE_WORK_MEM = "256MB"  # 덧붙이기 비교(해시 조인)가 디스크로 넘치지 않게. 그 트랜잭션에서만


@dataclass
class Merged:
    """덧붙이기 결과"""

    replaced: int  # 이미 있던 칸 (새 값으로 바꿈)
    replaced_sum: Decimal  # 바꾸기 전 값의 합계
    archived: int  # 그중 값이 달라 이력으로 보낸 칸


def merge_into(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> Merged:
    """받은 일부 자료를 기존 표에 덧붙인다 (갱신: 빠진 주기·새 시점만 받은 경우).
    기존 칸은 건드리지 않아 큰 표도 기존 행을 복사하지 않는다.
    이미 있는 칸이 오면 새 값으로 바꾸고, 값이 달랐으면 예전 값을 이력에 남긴다"""
    with pg.transaction():
        pg.execute(
            "CREATE TEMP TABLE new_rows (LIKE raw.stat_value INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        target = sql.Identifier("new_rows")
        copy_rows(pg, target, rows)
        check_duplicates(pg, target, name)
        check_loaded(pg, target, scan, name)
        pg.execute(f"SET LOCAL work_mem = '{MERGE_WORK_MEM}'")
        pg.execute("ANALYZE new_rows")
        # 겹칠 수 있는 기존 행은 새 자료와 주기·시점이 같은 것뿐이다.
        # 빠진 주기·새 시점만 받으면 거의 없어서, 표 전체(수천만 행)와 맞대지 않고 먼저 골라 둔다
        pg.execute(
            sql.SQL(
                "CREATE TEMP TABLE old_rows ON COMMIT DROP AS SELECT * FROM raw.stat_value"
                " WHERE tbl_key = {} AND (prd_se, period) IN"
                " (SELECT DISTINCT prd_se, period FROM new_rows)"
            ).format(sql.Literal(tbl_key))
        )
        archived, replaced, replaced_sum = 0, 0, Decimal(0)
        if pg.execute("SELECT 1 FROM old_rows LIMIT 1").fetchone():
            pg.execute("ANALYZE old_rows")
            archived = archive_changed(pg, target, tbl_key, sql.SQL("old_rows o"))
            replaced, replaced_sum = pg.execute(
                sql.SQL(
                    "WITH d AS (DELETE FROM raw.stat_value o USING new_rows n"
                    " WHERE o.tbl_key = {} AND (o.prd_se, o.period) IN"
                    " (SELECT DISTINCT prd_se, period FROM old_rows) AND {} RETURNING o.value)"
                    " SELECT count(*), coalesce(sum(value), 0) FROM d"
                ).format(sql.Literal(tbl_key), SAME_CELL)
            ).fetchone()
        pg.execute("INSERT INTO raw.stat_value SELECT * FROM new_rows")
    return Merged(replaced, replaced_sum, archived)


def load_table(pg: psycopg.Connection, path: Path, merge: bool = False) -> None:
    """표 하나 적재. merge면 기존 표에 덧붙인다 (적재된 표만. 처음 적재하는 표는 그냥 넣는다)"""
    start = time.perf_counter()
    scan = scan_csv(path)
    tbl_key, keys, units = save_codes(pg, path, scan)
    rows = value_rows(path, tbl_key, keys, units)
    loaded = pg.execute(
        "SELECT row_count, value_sum, first_period, last_period FROM raw.stat_load"
        " WHERE tbl_key = %s",
        [tbl_key],
    ).fetchone()
    if merge and loaded:
        done = merge_into(pg, tbl_key, rows, scan, path.stem)
        old_rows, old_sum, old_first, old_last = loaded
        periods = [
            p for p in (old_first, old_last, scan.first_period, scan.last_period) if p is not None
        ]
        pg.execute(
            """
            UPDATE raw.stat_load SET row_count = %s, value_sum = %s, first_period = %s,
                   last_period = %s, last_changed = greatest(last_changed, %s),
                   source_file = %s, loaded_at = now()
            WHERE tbl_key = %s
            """,
            [
                old_rows + scan.row_count - done.replaced,
                (old_sum or 0) + scan.value_sum - done.replaced_sum,
                min(periods) if periods else None,
                max(periods) if periods else None,
                scan.last_changed,
                path.name,
                tbl_key,
            ],
        )
        print(
            f"{path.stem}: 덧붙임 {scan.row_count:,}행 (이미 있던 칸 {done.replaced:,}개,"
            f" 이력으로 보낸 예전 값 {done.archived:,}개), {time.perf_counter() - start:.1f}초",
            flush=True,
        )
        return
    # 한 번 큰 표(전용 파티션)였으면 그대로 둔다. 갱신은 일부 시점만 받아 행 수가 적어도
    # 기존 칸과 합치면 큰 표이고, 작은 표로 옮기면 전용 파티션이 없어진다
    was_big = pg.execute(
        "SELECT is_big FROM raw.stat_load WHERE tbl_key = %s", [tbl_key]
    ).fetchone()
    big = is_big(scan.row_count) or bool(was_big and was_big[0])
    kept = (replace_big if big else replace_small)(pg, tbl_key, rows, scan, path.stem)
    periods = [p for p in (scan.first_period, scan.last_period) if p is not None]
    periods += [p for p in (kept.first_period, kept.last_period) if p is not None]
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
            scan.row_count + kept.carried,  # 표에 실제로 있는 행 (남긴 기존 칸 포함)
            scan.value_sum + kept.carried_sum,
            min(periods) if periods else None,
            max(periods) if periods else None,
            scan.last_changed,
            big,
            path.name,
        ],
    )
    kind = "전용 파티션" if big else "기본 파티션"
    kept_text = (
        f", 남긴 기존 칸 {kept.carried:,}개, 이력으로 보낸 예전 값 {kept.archived:,}개"
        f", 같은 시점에서 빠져 이력으로만 남긴 칸 {kept.dropped:,}개"
        if kept.carried or kept.archived or kept.dropped
        else ""
    )
    print(
        f"{path.stem}: {scan.row_count:,}행, 코드 {len(keys):,}개, {kind}{kept_text},"
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
