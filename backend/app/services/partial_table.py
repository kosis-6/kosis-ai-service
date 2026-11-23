"""칸만 받은 표: 적재 안 된 큰 표에서 답에 필요한 칸만 KOSIS에서 받아 그 질문에만 쓴다

큰 표(바로 받기 요청이 table_cache.INLINE_MAX_REQUESTS번을 넘는 표)는 표 전체를 받는 데 요청이
수십~수백 번 들어 대기열(load_queue)이 뒤에서 적재한다. 그동안 질문은 이렇게 답한다
(dynamic_indicator._build_partial):

  ① 표의 메타(항목·분류 코드와 이름, 수록 기간)로 칸 목록과 지역 매핑을 만든다 (from_meta)
     적재와 같은 규칙을 쓴다: 분류 번호는 분류 순번 순서(c1, c2 …, load_raw.save_codes),
     칸 단계·경로는 collect_kosis.build_hierarchy, 지역 매핑은 적재 후 단계와 같은 region_map 함수
  ② 칸 고르기(dynamic_indicator.build)가 그 칸 목록으로 칸을 고른다
  ③ 고른 칸만 요청한다 (requests_for → table_cache.fetch_cells). 요청이 MAX_REQUESTS번을 넘으면
     받지 않는다 (지금처럼 표 안내)
  ④ 계산은 lookup이 그대로 한다. lookup·dynamic_indicator에서 DB를 읽는 함수(칸 목록·지역 칸·
     수록 기간·값 합산·단위)가 이 표가 등록돼 있으면 DB 대신 여기를 읽는다.
     sum_rows는 lookup._sum의 SQL과, unit_of는 dynamic_indicator._unit의 SQL과 같은 결과를 낸다

약속
  - 받은 값은 DB(raw.stat_value)에 넣지 않는다. 적재 여부는 지금처럼 raw.stat_load로만 판단하고,
    stat_value에 일부 칸만 있는 상태를 만들지 않는다 (load_queue.py)
  - 등록은 질문 하나 안에서만 보인다 (contextvars. 스레드에서 도는 계산에도 넘어간다).
    다른 질문과 lookup의 lru 캐시에는 들어가지 않아, 적재가 끝나면 DB 값을 읽는다
  - 표 키(lookup._tbl_key)는 바꾸지 않는다. 여기로 돌리지 않은 DB 조회는 지금처럼 '적재 안 됨'으로
    멈춘다 (빈 값으로 답하지 않는다)
  - 표 전체를 보는 검증(분자 ≤ 분모 part_within_whole, 부부 단위 건수 couple_counted)은 할 수 없다.
    그 검증이 필요한 계산(비율·구성비·연령 구간 합 등)은 적재를 기다린다

설정 (환경변수)
  PARTIAL_MAX_REQUESTS  칸만 받기에 쓰는 KOSIS 요청 수 상한 (기본 3)
"""

import contextvars
import os
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, NamedTuple

MAX_REQUESTS = int(os.environ.get("PARTIAL_MAX_REQUESTS", "3"))
YEARLY = ("A", "F")  # lookup.YEARLY와 같은 연 단위 시점 (A 연간, F 다년)
REQUEST_PERIODS = ("Y", "F")  # 요청 수록주기 코드 (응답에는 Y가 A로 온다)
CHECK_REASON = "칸만 받은 표라 표 전체로 하는 검증을 할 수 없음 (적재 뒤 계산)"


class Unsupported(ValueError):
    """칸만 받아서는 답할 수 없는 표 (예: 연 단위 자료가 없음)"""


class Row(NamedTuple):
    period: int
    prd_se: str
    codes: tuple[str, ...]  # (항목, c1, c2 …) KOSIS 코드
    value: Decimal | None  # 통계부호(비공개 등)는 None
    unit: str


@dataclass
class Table:
    tbl_id: str
    org_id: str
    meta: dict[str, Any]  # collect_kosis.fetch_meta 결과
    dims: dict[int, tuple[str, dict[str, tuple[int, str]]]]  # 번호 → (이름, {코드: (키, 이름)})
    region_dim: int | None
    region_rows: list[dict[str, Any]]  # mart.region_code_map과 같은 칸
    span: tuple[int, int]  # 메타의 연 단위 수록 기간
    rows: dict[tuple, Row] = field(default_factory=dict)
    asked: set[tuple] = field(default_factory=set)  # 보낸 요청 (같은 요청을 다시 보내지 않는다)
    full_items: set[str] = field(default_factory=set)  # 연 단위 기간 전체를 받은 항목
    requests: int = 0


@dataclass
class Fetch:
    """보낼 요청. full: 연 단위 기간 전체를 받는다 (아니면 물은 연도만)"""

    requests: list[dict[str, str]]
    items: set[str]
    full: bool


# ---- 질문 단위 등록 ---------------------------------------------------------------------------

_ACTIVE: contextvars.ContextVar[dict[str, Table] | None] = contextvars.ContextVar(
    "partial_tables", default=None
)


def active(tbl_id: str) -> Table | None:
    """이 질문에서 칸만 받은 표 (없으면 None → DB를 읽는다)"""
    tables = _ACTIVE.get()
    return tables.get(tbl_id) if tables else None


def register(table: Table) -> None:
    """이 질문(지금 컨텍스트)에 등록한다. 같은 작업 안에서 이어지는 계산이 함께 본다"""
    tables = _ACTIVE.get()
    if tables is None:
        tables = {}
        _ACTIVE.set(tables)
    tables[table.tbl_id] = table


# ---- 메타 → 칸 목록·지역 매핑 -------------------------------------------------------------------


@lru_cache
def _regions() -> tuple[tuple, ...]:
    """기준 지역 목록 (코드, 이름, 종류, 경로, 유효 시작, 유효 끝)"""
    from app.core.stats_db import rows

    return tuple(
        (r["region_code"], r["region_name"], r["kind"], r["path"], r["valid_from"], r["valid_to"])
        for r in rows(
            "select region_code, region_name, kind, path, valid_from, valid_to from regions"
        )
    )


def from_meta(tbl_id: str, org_id: str, meta: dict[str, Any]) -> Table:
    """메타로 칸 목록과 지역 매핑을 만든다 (값은 아직 없다). 기준 지역 목록은 DB에서 읽는다"""
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
        rows = region_map.map_rows(idx, tbl_id, org_id, src_dim, sources[src_dim])
        if any(r["region_code"] for r in rows):  # 하나도 이어지지 않으면 대응을 만들지 않는다
            region_dim = src_dim
            for r in rows:
                r["valid_from"], r["valid_to"] = valid.get(r["region_code"], (None, None))
            region_rows = rows
    span = (min(a for a, _ in years), max(b for _, b in years))
    return Table(tbl_id, org_id, meta, dims, region_dim, region_rows, span)


def codes(table: Table, dim_no: int) -> dict[str, tuple[int, str]]:
    """lookup._codes와 같은 모양: {KOSIS 코드: (코드 키, 이름)}"""
    return table.dims.get(dim_no, ("", {}))[1]


def level(table: Table) -> str:
    """지역 수준 (dynamic_indicator.schema가 region_code_map으로 정하는 것과 같은 규칙)"""
    found = {r["region_code"] for r in table.region_rows if r["region_code"]}
    if table.region_dim is None or not found:
        return "전국"
    if max(len(c) for c in found) >= 5:
        return "시군구"
    return "시도" if len(found) > 1 else "전국"


# ---- 받을 칸 ------------------------------------------------------------------------------


def _dim_no(col: str) -> int:
    """'item_id' → 0, 'c2_id' · 'c2' → 2 (lookup._dim_no와 같음)"""
    return 0 if col.startswith("item") else int(col[1])


def wanted(spec: dict) -> dict[int, set[str]]:
    """지표 정의가 고른 칸 {분류 번호: 코드들}. 여기 없는 분류(지역 등)는 전부(ALL) 받는다"""
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
        if src.get("age_dim"):  # 연령을 말하지 않으면 합계(또는 정의의 기본 연령)만 쓴다
            add(src["age_dim"], src.get("age_total"))
            add(src["age_dim"], list(src.get("age_default") or []))
    if op := spec.get("parts_op"):
        add(op["col"], op["codes"])
    return want


def _asked_years(table: Table, parsed: dict) -> tuple[int, int]:
    years = [parsed[k] for k in ("year", "start_year", "end_year") if parsed.get(k)]
    if not years:
        return table.span[1], table.span[1]  # 연도를 말하지 않으면 마지막 해 (_with_spec과 같음)
    return min(years), max(years)


def _plan(table: Table, want: dict[int, set[str]], years: tuple[int, int] | None):
    """요청 목록. 요청 나누기는 표 전체 수집(collect_all.plan_requests)을 고른 칸으로 좁혀 쓴다.
    한 요청의 칸 수(4만)를 넘지 않게 나눈다. 만들 수 없으면 None"""
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
        return None  # 고른 칸이 표에 없다
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
            # plan_requests는 나누지 않은 분류를 ALL로 보낸다. 고른 칸만 받도록 코드를 적는다
            for no, (c, full) in enumerate(zip(classes, meta["classes"], strict=True), start=1):
                key = f"objL{int(c.get('sn') or no)}"
                if params.get(key) != "ALL" or len(c["values"]) == len(full["values"]):
                    continue
                if len(c["values"]) > MAX_CODES:
                    return None  # 주소 길이 제한
                params[key] = "+".join(v["ITM_ID"] for v in c["values"])
            out.append({"prdSe": code, **params})
    return out or None


def requests_for(table: Table, spec: dict, parsed: dict) -> Fetch | None:
    """지표 정의를 계산하는 데 필요한 칸의 요청. 이미 보낸 요청은 뺀다.
    연 단위 기간 전체를 먼저 보고, 요청이 MAX_REQUESTS번을 넘으면 물은 연도만. 그래도 넘으면 None"""
    want = wanted(spec)
    for years in (None, _asked_years(table, parsed)):
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
    """KOSIS 응답을 행으로 더한다. 행 변환·단위 나누기는 적재와 같은 함수를 쓴다"""
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
    """요청을 다 받은 뒤: 기간 전체를 받은 항목은 수록 기간을 값으로 정할 수 있다"""
    if fetch.full:
        table.full_items |= fetch.items


# ---- DB 조회 대신 --------------------------------------------------------------------------


def _yearly(table: Table, y0: int | None = None, y1: int | None = None):
    for r in table.rows.values():
        if r.prd_se in YEARLY and (y0 is None or y0 <= r.period <= y1):
            yield r


def item_years(table: Table, items: tuple[str, ...]) -> tuple[int, int]:
    """lookup._item_years: 항목의 연 단위 수록 기간. 기간 전체를 받은 항목은 받은 값으로,
    물은 연도만 받았으면 메타의 수록 기간으로 본다"""
    periods = [r.period for r in _yearly(table) if r.codes[0] in items]
    if periods and set(items) <= table.full_items:
        return min(periods), max(periods)
    return table.span


def _matches(row: Row, want: dict[int, set[str]]) -> bool:
    return all(row.codes[n] in codes_ for n, codes_ in want.items())


def _want(pairs) -> dict[int, set[str]]:
    """(분류 번호, 코드 또는 코드들) → 조건. 같은 분류에 조건이 둘이면 둘 다 맞아야 한다
    (SQL의 and와 같음)"""
    want: dict[int, set[str]] = {}
    for n, val in pairs:
        values = {str(v) for v in (val if isinstance(val, list | tuple) else [val])}
        want[n] = want[n] & values if n in want else values
    return want


def sum_rows(table, src, region_codes, y0, y1, item_key, filters, age_codes, group_dim, projection):
    """lookup._sum과 같은 결과: 조건에 맞는 행의 합 {(연도, 지역 코드[, 분류 코드]): 값}.
    지역 코드는 지역 매핑의 유효 연도로 맞추고(장래추계는 보지 않음), 값이 모두 결측이면 None"""
    items = src.get(item_key) or src.get("item_id")
    pairs = [(0, items), *((_dim_no(col), val) for col, val in filters.items())]
    if src.get("age_dim") and age_codes is not None:
        pairs.append((_dim_no(src["age_dim"]), age_codes))
    want = _want(pairs)
    national = src.get("national_only") or table.region_dim is None
    maps: dict[str, list[tuple]] = {}
    if not national:
        asked = set(region_codes)
        for m in table.region_rows:
            if m["src_dim"] == table.region_dim and m["region_code"] in asked:
                maps.setdefault(m["src_code"], []).append(
                    (m["region_code"], m["valid_from"], m["valid_to"])
                )
    part = _dim_no(group_dim) if group_dim else None
    sums: dict[tuple, list] = {}
    for r in _yearly(table, y0, y1):
        if not _matches(r, want):
            continue
        if national:
            places = ["00"]
        else:
            places = [
                code
                for code, lo, hi in maps.get(r.codes[table.region_dim], [])
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
    """dynamic_indicator._unit과 같은 결과: (가장 많은 단위, 값이 모두 정수인가).
    단위가 없는 행은 보지 않는다. 고른 칸으로 좁힌 행이 없으면 항목만으로 본다"""
    want = _want([(0, item), *(cells or {}).items()])
    rows = [r for r in _yearly(table) if r.unit and _matches(r, want)]
    if cells and not rows:
        return unit_of(table, item)
    if not rows:
        return None, False
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.unit] = counts.get(r.unit, 0) + 1
    unit = min(counts, key=lambda u: (-counts[u], u))  # 같으면 이름 순으로 앞 (mode()와 같음)
    values = [r.value for r in rows if r.value is not None]
    return unit, bool(values) and all(v == v.to_integral_value() for v in values)
