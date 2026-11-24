import argparse
import csv
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.stats_build import conninfo  # noqa: E402

CSV_DIR = ROOT / os.environ.get("KOSIS_RAW_DIR", "data/processed")
BIG_ROWS = 1_000_000
MAX_DIMS = 5
VALUE_COLUMNS = (
    "tbl_key", "period", "item_key", "c1", "c2", "c3", "c4", "c5",
    "unit_key", "prd_se", "value", "flag",
)  # fmt: skip


CELL_KEY = ("item_key", "period", "prd_se", "c1", "c2", "c3", "c4", "c5")


@dataclass
class Scan:
    row_count: int = 0
    value_sum: Decimal = Decimal(0)
    first_period: int | None = None
    last_period: int | None = None
    last_changed: date | None = None
    codes: dict[tuple[int, str], tuple[str, int | None, str | None]] = field(default_factory=dict)
    units: set[str] = field(default_factory=set)
    unsplit: set[tuple[str, str]] = field(default_factory=set)
    fixed_units: int = 0


UNIT_FIXES = ROOT / "config" / "unit_fixes.toml"
BRACKET_UNIT = re.compile(
    r"\(\s*(명|건|가구|세대|세|%|천명당|천 명당|천명|천가구|개|호|원|배)\s*\)\s*$"
)


@dataclass
class UnitFixes:
    merged: frozenset[str] = frozenset()
    tables: dict[str, dict[str, str]] = field(default_factory=dict)


def load_unit_fixes(path: Path = UNIT_FIXES) -> UnitFixes:
    if not path.exists():
        return UnitFixes()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return UnitFixes(frozenset(data.get("merged") or []), dict(data.get("tables") or {}))


def row_unit(r: dict[str, str], dims: int, tbl_id: str, fixes: UnitFixes) -> tuple[str, bool]:
    unit = r["unit"]
    if unit not in fixes.merged:
        return unit, True
    table = fixes.tables.get(tbl_id) or {}
    keys = [r["item_id"], *(f"c{d}:{r[f'c{d}_id']}" for d in range(1, dims + 1))]
    for key in keys:
        if key in table:
            return table[key], True
    names = [r["item_nm"], *(r[f"c{d}_nm"] for d in range(1, dims + 1))]
    for name in names:
        if m := BRACKET_UNIT.search(name or ""):
            return m.group(1), True
    if "*" in table:
        return table["*"], True
    return unit, False


def dim_count(header: list[str]) -> int:
    n = sum(1 for c in header if c[0] == "c" and c.endswith("_id"))
    if n > MAX_DIMS:
        raise SystemExit(f"분류가 {n}개입니다. raw.stat_value는 {MAX_DIMS}개까지 담습니다.")
    return n


def duplicate_message(name: str, found: list[tuple]) -> str:
    lines = [
        ", ".join(f"{c}={v}" for c, v in zip(CELL_KEY, r[:-1], strict=True) if v is not None)
        + f" ({r[-1]}번)"
        for r in found
    ]
    return f"{name}: 같은 칸이 중복되어 교체하지 않습니다. 예시: " + " / ".join(lines)


def is_big(row_count: int) -> bool:
    return row_count >= BIG_ROWS


def scan_csv(path: Path, fixes: UnitFixes | None = None) -> Scan:
    scan = Scan()
    fixes = load_unit_fixes() if fixes is None else fixes
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        dims = dim_count(reader.fieldnames or [])
        for r in reader:
            if r["unit"] in fixes.merged:
                unit, ok = row_unit(r, dims, path.stem, fixes)
                if ok:
                    scan.fixed_units += 1
                else:
                    where = " / ".join([r["item_nm"], *(r[f"c{d}_nm"] for d in range(1, dims + 1))])
                    scan.unsplit.add((r["unit"], where))
                r["unit"] = unit
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


def value_rows(
    path: Path,
    tbl_key: int,
    keys: dict[tuple[int, str], int],
    units: dict[str, int],
    fixes: UnitFixes | None = None,
):
    fixes = load_unit_fixes() if fixes is None else fixes
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        dims = dim_count(reader.fieldnames or [])
        for r in reader:
            if r["unit"] in fixes.merged:
                r["unit"] = row_unit(r, dims, path.stem, fixes)[0]
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


SAME_CELL = sql.SQL(" AND ").join(
    [sql.SQL("n.{c} = o.{c}").format(c=sql.Identifier(c)) for c in ("item_key", "period", "prd_se")]
    + [
        sql.SQL("coalesce(n.{c}, -1) = coalesce(o.{c}, -1)").format(c=sql.Identifier(c))
        for c in ("c1", "c2", "c3", "c4", "c5")
    ]
)


@dataclass
class Kept:
    carried: int = 0
    carried_sum: Decimal = Decimal(0)
    first_period: int | None = None
    last_period: int | None = None
    archived: int = 0
    dropped: int = 0


def _old(tbl_key: int) -> sql.Composable:
    return sql.SQL("(SELECT * FROM raw.stat_value WHERE tbl_key = {}) o").format(
        sql.Literal(tbl_key)
    )


_NAMES = sql.SQL(", ").join(map(sql.Identifier, VALUE_COLUMNS))
_OLD_COLS = sql.SQL(", ").join(sql.SQL("o.{}").format(sql.Identifier(c)) for c in VALUE_COLUMNS)


def archive_changed(
    pg: psycopg.Connection, new: sql.Composable, tbl_key: int, old: sql.Composable | None = None
) -> int:
    return pg.execute(
        sql.SQL(
            "INSERT INTO raw.stat_value_history ({names}) SELECT {cols} FROM {old} JOIN {new} n"
            " ON {same} WHERE o.value IS DISTINCT FROM n.value OR o.flag IS DISTINCT FROM n.flag"
            " OR o.unit_key IS DISTINCT FROM n.unit_key"
        ).format(names=_NAMES, cols=_OLD_COLS, old=old or _old(tbl_key), new=new, same=SAME_CELL)
    ).rowcount


def _new_periods(new: sql.Composable) -> sql.Composable:
    return sql.SQL("(o.prd_se, o.period) IN (SELECT DISTINCT prd_se, period FROM {})").format(new)


def carry_missing(pg: psycopg.Connection, new: sql.Composable, tbl_key: int) -> Kept:
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
    pg.execute(sql.SQL("CREATE INDEX ON {} (tbl_key, item_key, period, c1)").format(new))
    pg.execute(sql.SQL("ANALYZE {}").format(new))
    old = sql.Identifier("raw", part)
    with pg.transaction():
        kept.archived = archive_changed(pg, new, tbl_key)
        kept.dropped = archive_dropped(pg, new, tbl_key)
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
            pg.execute(sql.SQL("DROP TABLE {}").format(part))
        pg.execute("INSERT INTO raw.stat_value SELECT * FROM new_rows")
    return kept


MERGE_WORK_MEM = "256MB"


@dataclass
class Merged:
    replaced: int
    replaced_sum: Decimal
    archived: int


def merge_into(pg: psycopg.Connection, tbl_key: int, rows, scan: Scan, name: str) -> Merged:
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


def sync_period_meta(pg: psycopg.Connection, tbl_key: int) -> None:
    pg.execute(
        """
        WITH l AS (
            SELECT t.tbl_id,
                   CASE WHEN s.first_period > 9999 THEN s.first_period / 100
                        ELSE s.first_period END AS data_start,
                   CASE WHEN s.last_period > 9999 THEN s.last_period / 100
                        ELSE s.last_period END AS data_end
            FROM raw.stat_load s JOIN catalog.stat_table t USING (tbl_key)
            WHERE s.tbl_key = %s
        ), m AS (
            SELECT m.tbl_id, m.start_year AS old_start, m.end_year AS old_end,
                   least(m.start_year, l.data_start) AS new_start,
                   greatest(m.end_year, l.data_end) AS new_end
            FROM catalog.table_meta m JOIN l USING (tbl_id)
            WHERE l.data_start < m.start_year OR l.data_end > m.end_year
        ), fam AS (
            UPDATE catalog.table_families f
            SET edition = m.new_start || '~' || m.new_end
            FROM m
            WHERE f.tbl_id = m.tbl_id AND f.edition = m.old_start || '~' || m.old_end
        )
        UPDATE catalog.table_meta t
        SET start_year = m.new_start, end_year = m.new_end
        FROM m
        WHERE t.tbl_id = m.tbl_id
        """,
        [tbl_key],
    )


def load_table(pg: psycopg.Connection, path: Path, merge: bool = False) -> None:
    start = time.perf_counter()
    fixes = load_unit_fixes()
    scan = scan_csv(path, fixes)
    if scan.fixed_units:
        print(f"{path.stem}: 합쳐진 단위를 나눔 {scan.fixed_units:,}행 (config/unit_fixes.toml)")
    for unit, where in sorted(scan.unsplit)[:10]:
        print(f"{path.stem}: 경고 — 합쳐진 단위 '{unit}'를 나누지 못함 ({where})", flush=True)
    tbl_key, keys, units = save_codes(pg, path, scan)
    rows = value_rows(path, tbl_key, keys, units, fixes)
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
        sync_period_meta(pg, tbl_key)
        print(
            f"{path.stem}: 덧붙임 {scan.row_count:,}행 (이미 있던 칸 {done.replaced:,}개,"
            f" 이력으로 보낸 예전 값 {done.archived:,}개), {time.perf_counter() - start:.1f}초",
            flush=True,
        )
        return
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
            scan.row_count + kept.carried,
            scan.value_sum + kept.carried_sum,
            min(periods) if periods else None,
            max(periods) if periods else None,
            scan.last_changed,
            big,
            path.name,
        ],
    )
    sync_period_meta(pg, tbl_key)
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
