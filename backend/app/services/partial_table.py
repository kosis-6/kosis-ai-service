import contextvars
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, NamedTuple

from app.services import timeref

MAX_REQUESTS = int(os.environ.get("PARTIAL_MAX_REQUESTS", "3"))
YEARLY = ("A", "F")
REQUEST_PERIODS = ("Y", "F")
CHECK_REASON = "칸만 받은 표라 표 전체로 하는 검증을 할 수 없음 (적재 뒤 계산)"


class Unsupported(ValueError):
    pass


class Row(NamedTuple):
    period: int
    prd_se: str
    codes: tuple[str, ...]
    value: Decimal | None
    unit: str


@dataclass
class Table:
    tbl_id: str
    org_id: str
    meta: dict[str, Any]
    dims: dict[int, tuple[str, dict[str, tuple[int, str]]]]
    region_dim: int | None
    region_rows: list[dict[str, Any]]
    span: tuple[int, int]
    rows: dict[tuple, Row] = field(default_factory=dict)
    asked: set[tuple] = field(default_factory=set)
    full_items: set[str] = field(default_factory=set)
    requests: int = 0


@dataclass
class Fetch:
    requests: list[dict[str, str]]
    items: set[str]
    full: bool


_ACTIVE: contextvars.ContextVar[dict[str, Table] | None] = contextvars.ContextVar(
    "partial_tables", default=None
)


def active(tbl_id: str) -> Table | None:
    tables = _ACTIVE.get()
    return tables.get(tbl_id) if tables else None


def register(table: Table) -> None:
    tables = _ACTIVE.get()
    if tables is None:
        tables = {}
        _ACTIVE.set(tables)
    tables[table.tbl_id] = table


@contextmanager
def shared_tables():
    token = _ACTIVE.set(dict(_ACTIVE.get() or {}))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


@lru_cache
def _regions() -> tuple[tuple, ...]:
    from app.core.stats_db import rows

    return tuple(
        (r["region_code"], r["region_name"], r["kind"], r["path"], r["valid_from"], r["valid_to"])
        for r in rows(
            "select region_code, region_name, kind, path, valid_from, valid_to from regions"
        )
    )


def from_meta(tbl_id: str, org_id: str, meta: dict[str, Any]) -> Table:
    from pipeline import region_map
    from scripts.collect_all import period_code
    from scripts.collect_kosis import build_hierarchy

    years = [
        (int(str(p["STRT_PRD_DE"])[:4]), int(str(p["END_PRD_DE"])[:4]))
        for p in meta["periods"]
        if period_code(p["PRD_SE"]) in REQUEST_PERIODS
    ]
    if not years:
        raise Unsupported("연 단위 자료가 없는 표")
    keys = iter(range(1, 1_000_000))
    dims = {0: ("항목", {x["ITM_ID"]: (next(keys), x["ITM_NM"]) for x in meta["items"]})}
    sources = {}
    for no, c in enumerate(meta["classes"], start=1):
        dims[no] = (c["name"], {v["ITM_ID"]: (next(keys), v["ITM_NM"]) for v in c["values"]})
        levels = build_hierarchy(c["values"])
        sources[no] = [
            region_map.Source(v["ITM_ID"], v["ITM_NM"], *levels[v["ITM_ID"]])
            for v in sorted(c["values"], key=lambda v: v["ITM_ID"])
        ]
    region_dim, region_rows = None, []
    src_dim = region_map.find_region_dim(tbl_id, [(n, d[0]) for n, d in dims.items() if n])
    if src_dim is not None:
        regions = _regions()
        idx = region_map.RegionIndex([r[:4] for r in regions])
        valid = {r[0]: (r[4], r[5]) for r in regions}
        region_rows = region_map.map_rows(idx, tbl_id, org_id, src_dim, sources[src_dim])
        if not any(r["region_code"] for r in region_rows):
            raise Unsupported("표의 지역 코드를 확인할 수 없음")
        region_dim = src_dim
        for r in region_rows:
            r["valid_from"], r["valid_to"] = valid.get(r["region_code"], (None, None))
    span = (min(a for a, _ in years), max(b for _, b in years))
    return Table(tbl_id, org_id, meta, dims, region_dim, region_rows, span)


def codes(table: Table, dim_no: int) -> dict[str, tuple[int, str]]:
    return table.dims.get(dim_no, ("", {}))[1]


def level(table: Table) -> str:
    found = {r["region_code"] for r in table.region_rows if r["region_code"]}
    if table.region_dim is None or not found:
        return "전국"
    if max(len(c) for c in found) >= 5:
        return "시군구"
    return "시도" if found - {"00"} else "전국"


def _dim_no(col: str) -> int:
    return 0 if col.startswith("item") else int(col[1])


def wanted(spec: dict) -> dict[int, set[str]]:
    want: dict[int, set[str]] = {}

    def add(col: str, val) -> None:
        if val is None:
            return
        values = val if isinstance(val, list | tuple) else [val]
        want.setdefault(_dim_no(col), set()).update(str(v) for v in values)

    for src in spec["sources"]:
        add("item_id", src.get("item_id"))
        add("item_id", src.get("den_item_id"))
        for filters in (src.get("filters"), src.get("den_filters")):
            for col, val in (filters or {}).items():
                add(col, val)
        for over in (src.get("by_sex") or {}).values():
            add("item_id", over.get("item_id"))
            for col, val in (over.get("filters") or {}).items():
                add(col, val)
        if src.get("age_dim"):
            add(src["age_dim"], src.get("age_total"))
            add(src["age_dim"], list(src.get("age_default") or []))
    if op := spec.get("parts_op"):
        add(op["col"], op["codes"])
    return want


def _asked_years(table: Table, spec: dict, parsed: dict) -> tuple[int, int]:
    lo, hi = table.span
    if not spec.get("parts_op") and parsed.get("type") == "trend":
        return timeref.lookup_trend_years(parsed, lo, hi, parsed.get("year") or hi)
    years = [parsed[k] for k in ("year", "start_year", "end_year") if parsed.get(k)]
    if not years:
        return hi, hi
    return min(years), max(years)


def _plan(table: Table, want: dict[int, set[str]], years: tuple[int, int] | None):
    from scripts.collect_all import (
        MAX_CODES,
        every_years,
        period_code,
        plan_requests,
        request_period,
    )
    from scripts.collect_kosis import KosisError

    meta = table.meta
    items = [x for x in meta["items"] if 0 not in want or x["ITM_ID"] in want[0]]
    classes = []
    for no, c in enumerate(meta["classes"], start=1):
        values = [v for v in c["values"] if no not in want or v["ITM_ID"] in want[no]]
        classes.append({**c, "values": values})
    if not items or any(not c["values"] for c in classes):
        return None
    narrow = {**meta, "items": items, "classes": classes}
    out = []
    for p in meta["periods"]:
        code = period_code(p["PRD_SE"])
        if code not in REQUEST_PERIODS:
            continue
        start, end = request_period(p["STRT_PRD_DE"], code), request_period(p["END_PRD_DE"], code)
        if years is not None:
            lo, hi = max(int(start[:4]), years[0]), min(int(end[:4]), years[1])
            if lo > hi:
                continue
            start, end = str(lo), str(hi)
        try:
            planned = plan_requests(narrow, code, start, end, every_years(p["PRD_SE"]))
        except KosisError:
            return None
        for params in planned:
            params = dict(params)
            for no, (c, full) in enumerate(zip(classes, meta["classes"], strict=True), start=1):
                key = f"objL{int(c.get('sn') or no)}"
                if params.get(key) != "ALL" or len(c["values"]) == len(full["values"]):
                    continue
                if len(c["values"]) > MAX_CODES:
                    return None
                params[key] = "+".join(v["ITM_ID"] for v in c["values"])
            out.append({"prdSe": code, **params})
    return out or None


def requests_for(table: Table, spec: dict, parsed: dict) -> Fetch | None:
    want = wanted(spec)
    for years in (None, _asked_years(table, spec, parsed)):
        planned = _plan(table, want, years)
        if planned is not None and len(planned) <= MAX_REQUESTS:
            todo = [r for r in planned if request_key(r) not in table.asked]
            return Fetch(todo, set(want.get(0) or codes(table, 0)), years is None)
    return None


def request_key(params: dict[str, str]) -> tuple:
    return tuple(sorted(params.items()))


@lru_cache
def _unit_fixes():
    from pipeline.load_raw import load_unit_fixes

    return load_unit_fixes()


def add_records(table: Table, params: dict[str, str], records: list[dict[str, Any]]) -> None:
    from pipeline.load_raw import row_unit
    from scripts.collect_all import to_rows_by_sn

    n = len(table.meta["classes"])
    fixes = _unit_fixes()
    for r in to_rows_by_sn(records, table.meta):
        if r["unit"] in fixes.merged:
            r["unit"] = row_unit(r, n, table.tbl_id, fixes)[0]
        cells = (r["item_id"], *(r[f"c{d}_id"] for d in range(1, n + 1)))
        period = int(r["period"])
        value = Decimal(r["value"]) if r["value"] else None
        table.rows[(period, r["prd_se"], cells)] = Row(
            period, r["prd_se"], cells, value, r["unit"] or ""
        )
    table.asked.add(request_key(params))
    table.requests += 1


def done(table: Table, fetch: Fetch) -> None:
    if fetch.full:
        table.full_items |= fetch.items


def _yearly(table: Table, y0: int | None = None, y1: int | None = None):
    for r in table.rows.values():
        if r.prd_se in YEARLY and (y0 is None or y0 <= r.period <= y1):
            yield r


def item_years(table: Table, items: tuple[str, ...]) -> tuple[int, int]:
    periods = [r.period for r in _yearly(table) if r.codes[0] in items]
    if periods and set(items) <= table.full_items:
        return min(periods), max(periods)
    return table.span


def _matches(row: Row, want: dict[int, set[str]]) -> bool:
    return all(row.codes[n] in codes_ for n, codes_ in want.items())


def _want(pairs) -> dict[int, set[str]]:
    want: dict[int, set[str]] = {}
    for n, val in pairs:
        values = {str(v) for v in (val if isinstance(val, list | tuple) else [val])}
        want[n] = want[n] & values if n in want else values
    return want


def sum_rows(table, src, region_codes, y0, y1, item_key, filters, age_codes, group_dim, projection):
    items = src.get(item_key) or src.get("item_id")
    pairs = [(0, items), *((_dim_no(col), val) for col, val in filters.items())]
    if src.get("age_dim") and age_codes is not None:
        pairs.append((_dim_no(src["age_dim"]), age_codes))
    want = _want(pairs)
    region_dim = table.region_dim
    maps: dict[str, list[tuple]] = {}
    if region_dim is not None:
        asked = {"00"} if src.get("national_only") else set(region_codes)
        for m in table.region_rows:
            if m["src_dim"] == region_dim and m["region_code"] in asked:
                maps.setdefault(m["src_code"], []).append(
                    (m["region_code"], m["valid_from"], m["valid_to"])
                )
    part = _dim_no(group_dim) if group_dim else None
    sums: dict[tuple, list] = {}
    for r in _yearly(table, y0, y1):
        if not _matches(r, want):
            continue
        if region_dim is None:
            places = ["00"]
        else:
            places = [
                code
                for code, lo, hi in maps.get(r.codes[region_dim], [])
                if projection or ((lo is None or r.period >= lo) and (hi is None or r.period <= hi))
            ]
        for place in places:
            key = (r.period, place) if part is None else (r.period, place, r.codes[part])
            acc = sums.setdefault(key, [Decimal(0), 0])
            if r.value is not None:
                acc[0] += r.value
                acc[1] += 1
    return {k: (float(total) if n else None) for k, (total, n) in sums.items()}


def unit_of(table: Table, item: str, cells: dict[int, list[str]] | None = None):
    want = _want([(0, item), *(cells or {}).items()])
    rows = [r for r in _yearly(table) if r.unit and _matches(r, want)]
    if cells and not rows:
        return unit_of(table, item)
    if not rows:
        return None, False
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.unit] = counts.get(r.unit, 0) + 1
    unit = min(counts, key=lambda u: (-counts[u], u))
    values = [r.value for r in rows if r.value is not None]
    return unit, bool(values) and all(v == v.to_integral_value() for v in values)
