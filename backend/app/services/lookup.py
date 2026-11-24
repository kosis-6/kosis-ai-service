import re
import threading
import time
from contextvars import ContextVar
from functools import lru_cache

from app.core.stats_db import StatsNotLoadedError, StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import analysis, partial_table
from app.services import indicators as ind_mod
from app.services.josa import josa
from pipeline.region_map import find_region_dim

LEVELS = {"전국": 0, "시도": 1, "시군구": 2}
REGION_LEVEL = {"전국": "전국", "시도": "시도", "시군구": "시군구", "일반구": "시군구"}
DIFF_UNIT = {"count": None, "ratio": "%p", "index": "포인트"}
YEARLY = "prd_se in ('A', 'F')"
SEX_WORDS = {"남": "남자", "여": "여자"}
AGE_NAMES = [
    (re.compile(r"(\d+)\s*세?\s*[-~]\s*만?\s*(\d+)\s*세"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d+)\s*세\s*미만"), lambda m: (0, int(m[1]) - 1)),
    (re.compile(r"(\d+)\s*세\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*세\s*이하"), lambda m: (0, int(m[1]))),
    (re.compile(r"(\d+)\s*대\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*대\s*이하"), lambda m: (0, int(m[1]) + 9)),
    (re.compile(r"(\d+)\s*대"), lambda m: (int(m[1]), int(m[1]) + 9)),
    (re.compile(r"^\s*만?\s*(\d+)\s*세\s*$"), lambda m: (int(m[1]), int(m[1]))),
    (re.compile(r"^\s*(\d+)\s*[-~]\s*(\d+)\s*$"), lambda m: (int(m[1]), int(m[2]))),
]


class AgeMismatch(Exception):
    pass


def kind_of(spec):
    if spec.get("kind"):
        return spec["kind"]
    return {"명": "count", "건": "count", "%": "ratio"}.get(spec["unit"], "value")


def diff_unit(spec):
    k = kind_of(spec)
    return spec.get("diff_unit") or DIFF_UNIT.get(k) or spec["unit"]


def needs_split(spec, sex):
    return sex in (None, "", "계") and any(
        s.get("by_sex") and "계" not in s["by_sex"] for s in spec["sources"]
    )


def _effective(src, sex):
    by_sex = src.get("by_sex")
    if not by_sex:
        return src
    over = by_sex.get(sex or "계")
    if over is None:
        return None
    merged = {**src, **{k: v for k, v in over.items() if k != "filters"}}
    merged["filters"] = {**src.get("filters", {}), **over.get("filters", {})}
    return merged


@lru_cache
def _table(tbl_id):
    c = _rows("select tbl_nm, org_nm, start_year, end_year from catalog where tbl_id = ?", [tbl_id])
    if not c:
        c = _rows(
            "select table_name as tbl_nm, organization as org_nm, start_year, end_year"
            " from table_meta where tbl_id = ?",
            [tbl_id],
        )
    c = c[0] if c else {"tbl_nm": tbl_id, "org_nm": "", "start_year": None, "end_year": None}
    basis = ind_mod._catalog_rows(
        "select survey, reference_date from table_notes where tbl_id = ?", [tbl_id]
    )
    b = basis[0] if basis else {}
    return {
        **c,
        "basis": ", ".join(x for x in (b.get("survey"), b.get("reference_date")) if x),
    }


@lru_cache
def _tbl_key(tbl_id):
    r = _rows(
        "select t.tbl_key from stat_table t join stat_load l using (tbl_key) where t.tbl_id = ?",
        [tbl_id],
    )
    if not r:
        raise StatsTableMissing(f"{tbl_id} 수치가 raw.stat_value에 적재되지 않았습니다", tbl_id)
    return r[0]["tbl_key"]


def _codes(tbl_id, dim_no):
    if (part := partial_table.active(tbl_id)) is not None:
        return partial_table.codes(part, dim_no)
    return _codes_db(tbl_id, dim_no)


@lru_cache
def _codes_db(tbl_id, dim_no):
    return {
        r["code"]: (r["code_key"], r["name"])
        for r in _rows(
            "select code, code_key, name from stat_code where tbl_key = ? and dim_no = ?",
            [_tbl_key(tbl_id), dim_no],
        )
    }


def _dim_no(col):
    return 0 if col.startswith("item") else int(col[1])


def _in(column, tbl_id, dim_no, codes):
    known = _codes(tbl_id, dim_no)
    keys = [known[c][0] for c in codes if c in known]
    if not keys:
        return "false", []
    return f"{column} in ({', '.join('?' for _ in keys)})", keys


@lru_cache
def _projection(tbl_id):
    r = _rows("select is_projection_table from catalog where tbl_id = ?", [tbl_id])
    return bool(r and r[0]["is_projection_table"])


def _region_dim(tbl_id):
    if (part := partial_table.active(tbl_id)) is not None:
        return part.region_dim
    return _region_dim_db(tbl_id)


@lru_cache
def _region_dim_db(tbl_id):
    r = _rows("select min(src_dim) as d from region_code_map where tbl_id = ?", [tbl_id])
    if r[0]["d"] is not None:
        return r[0]["d"]
    dims = _rows(
        "select dim_no, name from stat_dimension where tbl_key = ? and dim_no > 0",
        [_tbl_key(tbl_id)],
    )
    return find_region_dim(tbl_id, [(d["dim_no"], d["name"]) for d in dims])


def _item_years(tbl_id, item_ids):
    if (part := partial_table.active(tbl_id)) is not None:
        return partial_table.item_years(part, item_ids)
    return _item_years_db(tbl_id, item_ids)


@lru_cache
def _item_years_db(tbl_id, item_ids):
    cond, keys = _in("item_key", tbl_id, 0, item_ids)
    r = _rows(
        "select min(period) as lo, max(period) as hi from stat_value"
        f" where tbl_key = ? and {YEARLY} and {cond}",
        [_tbl_key(tbl_id), *keys],
    )[0]
    return r["lo"], r["hi"]


def _items(src):
    v = src.get("item_id")
    return tuple(v) if isinstance(v, list) else (v,)


def _years(src):
    if src.get("years"):
        lo, hi = src["years"]
        return lo, hi
    return _item_years(src["table"], _items(src))


def parse_age(text):
    if "청년" in text:
        return None
    for pattern, to_band in AGE_NAMES:
        if m := pattern.search(text):
            return to_band(m)
    return None


def _dim_labels(tbl_id, dim):
    if partial_table.active(tbl_id) is not None:
        return _dim_labels_db.__wrapped__(tbl_id, dim)
    return _dim_labels_db(tbl_id, dim)


@lru_cache
def _dim_labels_db(tbl_id, dim):
    return tuple(sorted((code, name) for code, (_, name) in _codes(tbl_id, _dim_no(dim)).items()))


def _age_codes(src, band):
    lo, hi = band
    if hi is None and lo <= src.get("age_min", 0):
        return [src["age_total"]]
    starts = {}
    for code, name in _dim_labels(src["table"], src["age_dim"]):
        b = parse_age(name)
        if b and b[0] >= lo and (hi is None or (b[1] is not None and b[1] <= hi)):
            starts.setdefault(b[0], []).append((b, code))
    codes, pos = [], lo
    while True:
        cands = starts.get(pos)
        if not cands:
            raise AgeMismatch
        b, code = max(cands, key=lambda bc: float("inf") if bc[0][1] is None else bc[0][1])
        codes.append(code)
        if b[1] is None or b[1] == hi:
            break
        pos = b[1] + 1
    if b[1] != hi:
        raise AgeMismatch
    return codes


def _conds(src, item_key, filters, age_codes):
    tbl = src["table"]
    items = src.get(item_key) or src.get("item_id")
    pairs = [("v.item_key", 0, items)]
    pairs += [(f"v.c{_dim_no(col)}", _dim_no(col), val) for col, val in filters.items()]
    if src.get("age_dim") and age_codes is not None:
        pairs.append((f"v.c{_dim_no(src['age_dim'])}", _dim_no(src["age_dim"]), age_codes))
    conds, params = [], []
    for column, dim_no, val in pairs:
        cond, keys = _in(column, tbl, dim_no, val if isinstance(val, list) else [val])
        conds.append(cond)
        params += keys
    return conds, params


EMPTY_FLAGS: ContextVar[list | None] = ContextVar("empty_flags", default=None)


def _sum(src, codes, y0, y1, item_key, filters, age_codes, group_dim=None):
    tbl = src["table"]
    if (part := partial_table.active(tbl)) is not None:
        return partial_table.sum_rows(
            part, src, codes, y0, y1, item_key, filters, age_codes, group_dim, _projection(tbl)
        )
    conds, params = _conds(src, item_key, filters, age_codes)
    region_dim = _region_dim(tbl)
    if region_dim is None:
        region_sql, region_params, rc = "", [], "'00'"
    else:
        if src.get("national_only"):
            codes = ("00",)
        valid = (
            "true"
            if _projection(tbl)
            else "(m.valid_from is null or v.period >= m.valid_from)"
            " and (m.valid_to is null or v.period <= m.valid_to)"
        )
        region_sql = f"""
        join stat_code k on k.code_key = v.c{region_dim}
        join region_code_map m on m.tbl_id = ? and m.src_dim = ? and m.src_code = k.code
              and m.region_code in ({", ".join("?" for _ in codes)}) and {valid}"""
        region_params, rc = [tbl, region_dim, *codes], "m.region_code"
    part = f", v.c{_dim_no(group_dim)} as part" if group_dim else ""
    groups = "1, 2, 3" if group_dim else "1, 2"
    flags = EMPTY_FLAGS.get()
    flag_sql = ", max(v.flag) filter (where v.value is null) as flag" if flags is not None else ""
    rows = _rows(
        f"""
        select v.period as year, {rc} as rc{part}, sum(v.value)::float8 as v, count(v.value) as n
               {flag_sql}
        from stat_value v{region_sql}
        where v.tbl_key = ? and v.{YEARLY} and v.period between ? and ?
              and {" and ".join(conds)}
        group by {groups}
        order by {groups}
    """,
        [*region_params, _tbl_key(tbl), y0, y1, *params],
    )
    if group_dim:
        code_of = {key: code for code, (key, _) in _codes(tbl, _dim_no(group_dim)).items()}

    def key(r):
        return (r["year"], r["rc"], code_of[r["part"]]) if group_dim else (r["year"], r["rc"])

    if flags is not None:
        flags += [r["flag"] for r in rows if not r["n"] and r.get("flag")]
    return {key(r): (r["v"] if r["n"] else None) for r in rows}


def _fetch(src, codes, y0, y1, age_codes=None):
    num = _sum(src, codes, y0, y1, "item_id", src.get("filters", {}), age_codes)
    if not src.get("den_item_id"):
        return num
    den_filters = src.get("den_filters") or src.get("filters", {})
    den = _sum(src, codes, y0, y1, "den_item_id", den_filters, age_codes)
    scale = src.get("den_scale", 100)
    return {
        k: (analysis.share(v, den[k]) * scale / 100 if v is not None and den.get(k) else None)
        for k, v in num.items()
    }


def _diff(base, current, spec):
    fn = analysis.point_change if kind_of(spec) == "ratio" else analysis.change
    return fn(base, current)


def _round(v, spec):
    if v is None:
        return None
    if kind_of(spec) == "count":
        return int(round(v))
    return round(v, 3 if abs(v) < 10 and kind_of(spec) == "value" else 2)


def _resolve(spec, names, year):
    sigungu = any(s["level"] == "시군구" for s in spec["sources"])
    regs = []
    for name in names:
        reg = ind_mod.resolve_region(name, year, sigungu=sigungu)
        if reg["status"] != "ok":
            return {**reg, "indicator": spec["name"]}
        if (bad := ind_mod.not_yet_founded(reg, year, spec["name"])) is not None:
            return bad
        regs.append(ind_mod.in_year(reg, year))
    return regs


def _pick(spec, level, y0, y1, sex):
    fits = []
    for s in spec["sources"]:
        if s.get("national_only") and level != "전국":
            continue
        if LEVELS[s["level"]] < LEVELS[level]:
            continue
        eff = _effective(s, sex)
        if eff is not None:
            fits.append(eff)
    if not fits:
        top = max((s["level"] for s in spec["sources"]), key=LEVELS.get)
        return None, {
            "status": "no_region_level",
            "indicator": spec["name"],
            "reason": f"{josa(spec['name'], '은는')} {top} 단위까지만 자료가 있습니다.",
            "level": top,
        }
    spans = [(s, _years(s)) for s in fits]
    spans = [(s, span) for s, span in spans if None not in span]
    if not spans:
        return None, {
            "status": "missing_data",
            "indicator": spec["name"],
            "reason": f"{josa(spec['name'], '은는')} 이 표에 값이 없습니다.",
        }
    for s, (lo, hi) in spans:
        if lo <= y0 and y1 <= hi:
            return s, None
    lo, hi = min(a for _, (a, _) in spans), max(b for _, (_, b) in spans)
    return None, {
        "status": "out_of_range",
        "indicator": spec["name"],
        "reason": f"{spec['name']} 자료는 {lo}~{hi}년만 있습니다.",
        "available_years": [lo, hi],
    }


def _level(regs):
    return max((REGION_LEVEL.get(r["kind"], "시군구") for r in regs), key=LEVELS.get)


def _ages(src, ages):
    if not ages:
        if src.get("age_default"):
            return list(src["age_default"]), "", []
        return (([src["age_total"]] if src.get("age_dim") else None), "", [])
    if not src.get("age_dim"):
        raise AgeMismatch
    band = tuple(ages[0])
    codes = _age_codes(src, band)
    notes = []
    if codes == [src["age_total"]]:
        age = src["age_min"]
        notes.append(f"표에 {age}세 미만이 없어 {age}세 이상 전체로 계산했습니다")
        return codes, "", notes
    return codes, ind_mod.age_label(band) + " ", notes


def _band_mismatch(spec, src, ages):
    name = spec["name"]
    if not src.get("age_dim"):
        return {
            "status": "unknown_indicator",
            "indicator": name,
            "reason": f"{josa(name, '은는')} 연령으로 나눈 자료가 없습니다.",
        }
    lo, hi = ages[0]
    bands = sorted(
        {
            b
            for _, n in _dim_labels(src["table"], src["age_dim"])
            if (b := parse_age(n)) and b[0] >= src.get("age_min", 0)
        },
        key=lambda b: b[0],
    )
    top = hi if hi is not None else lo + 10
    near = [b for b in bands if b[0] <= top + 5 and (b[1] is None or b[1] >= lo - 5)]
    labels = [ind_mod.age_label(b) for b in (near or bands)]
    asked = ind_mod.age_label(tuple(ages[0]))
    return {
        "status": "band_mismatch",
        "indicator": name,
        "reason": f"{josa(name, '은는')} 표의 연령 구간이 질문한 {asked} 구간과 맞지 않아 "
        "그 값을 낼 수 없습니다.",
        "available_bands": labels,
        "alternative": f"대신 볼 수 있는 구간: {', '.join(labels)}",
    }


def _band_mismatch_any(spec, sex, ages):
    for s in spec["sources"]:
        eff = _effective(s, sex)
        if eff is None or not eff.get("age_dim"):
            continue
        try:
            _ages(eff, ages)
        except AgeMismatch:
            return _band_mismatch(spec, eff, ages)
        return None
    return None


def _base(spec, src, status="ok", label="", **extra):
    tbl = _table(src["table"])
    name = label + spec["name"] + (f"({src['label']})" if src.get("label") else "")
    notes = [spec["note"]] if spec.get("note") else []
    if src.get("note"):
        notes.append(src["note"])
    if src.get("national_only") and not src.get("years"):
        notes.append("전국 장기 추이 표(지역 구분 없음) 기준입니다")
    out = {
        "status": status,
        "indicator": name,
        "kind": kind_of(spec),
        "unit": spec["unit"],
        "formula": spec.get("formula") or f"KOSIS 공표 값 ({tbl['tbl_nm']})",
        "source": f"{tbl['org_nm']}, {tbl['tbl_nm']} ({src['table']})",
        "basis": tbl["basis"] or "KOSIS 공표 값",
        "tbl_id": src["table"],
        "url": ind_mod.kosis_url(src["table"]),
        "notes": notes,
    }
    if spec.get("answer_note"):
        out["answer_note"] = spec["answer_note"]
    out.update({k: v for k, v in extra.items() if k != "notes"})
    out["notes"] = out["notes"] + extra.get("notes", [])
    return out


def _prepare(spec, names, y0, y1, sex, ages):
    regs = _resolve(spec, names, y1)
    if isinstance(regs, dict):
        return regs
    src, err = _pick(spec, _level(regs), y0, y1, sex)
    if err:
        if ages and (band := _band_mismatch_any(spec, sex, ages)):
            return band
        return {**err, "region": ", ".join(r["display"] for r in regs)}
    try:
        age_codes, label, notes = _ages(src, ages)
    except AgeMismatch:
        return _band_mismatch(spec, src, ages)
    notes += [r["note"] for r in regs if r.get("note")]
    return regs, src, age_codes, label, notes


def value(spec, region, year, sex="계", ages=None):
    prep = _prepare(spec, [region], year, year, sex, ages)
    if isinstance(prep, dict):
        return prep
    (reg,), src, age_codes, label, notes = prep
    v = _fetch(src, [reg["region_code"]], year, year, age_codes).get((year, reg["region_code"]))
    return _base(
        spec,
        src,
        label=label,
        region=reg["display"],
        year=year,
        sex="계" if src.get("label") else sex,
        value=_round(v, spec),
        notes=notes,
    )


def _spans(spec, level, y0, y1, sex):
    spans, covered = [], set()
    for s in spec["sources"]:
        if s.get("national_only") and level != "전국":
            continue
        if LEVELS[s["level"]] < LEVELS[level]:
            continue
        eff = _effective(s, sex)
        if eff is None:
            continue
        lo, hi = _years(eff)
        years = [y for y in range(max(lo, y0), min(hi, y1) + 1) if y not in covered]
        if years:
            spans.append((eff, min(years), max(years)))
            covered.update(years)
    return spans


def _source_info(src):
    tbl = _table(src["table"])
    return {
        "source": f"{tbl['org_nm']}, {tbl['tbl_nm']} ({src['table']})",
        "basis": tbl["basis"] or "KOSIS 공표 값",
        "tbl_id": src["table"],
        "url": ind_mod.kosis_url(src["table"]),
    }


def _trend_series(spec, reg, start_year, end_year, sex, ages, out_of_range):
    spans = _spans(spec, REGION_LEVEL.get(reg["kind"], "시군구"), start_year, end_year, sex)
    if len(spans) < 2:
        return {**out_of_range, "region": reg["display"]}
    spans.sort(key=lambda s: s[1])
    vals, notes, label = {}, [], ""
    by_note = {}
    for src, a, b in spans:
        try:
            age_codes, label, age_notes = _ages(src, ages)
        except AgeMismatch:
            return {
                "status": "unknown_indicator",
                "indicator": spec["name"],
                "reason": josa(spec["name"], "은는")
                + " 질문한 연령 구간으로 나눈 자료가 없습니다.",
            }
        got = _fetch(src, [reg["region_code"]], a, b, age_codes)
        vals.update({k: v for k, v in got.items() if k not in vals})
        notes += [n for n in age_notes if n not in notes]
        if src.get("note") and src is not spans[-1][0]:
            lo, hi = by_note.get(src["note"], (a, b))
            by_note[src["note"]] = (min(lo, a), max(hi, b))
    notes += [f"{a}~{b}년: {n}" if a != b else f"{a}년: {n}" for n, (a, b) in by_note.items()]
    last = spans[-1][0]
    earlier = [_source_info(s) for s, _, _ in spans[:-1]]
    return last, label, notes, vals, earlier


def trend(spec, region, start_year, end_year, sex="계", ages=None):
    prep = _prepare(spec, [region], start_year, end_year, sex, ages)
    earlier = []
    if isinstance(prep, dict):
        if prep.get("status") != "out_of_range":
            return prep
        regs = _resolve(spec, [region], end_year)
        if isinstance(regs, dict):
            return regs
        reg = regs[0]
        joined = _trend_series(spec, reg, start_year, end_year, sex, ages, prep)
        if isinstance(joined, dict):
            return joined
        src, label, notes, vals, earlier = joined
        notes += [reg["note"]] if reg.get("note") else []
    else:
        (reg,), src, age_codes, label, notes = prep
        vals = _fetch(src, [reg["region_code"]], start_year, end_year, age_codes)
    code = reg["region_code"]
    series = [
        {"year": y, "value": _round(v, spec)} for (y, _), v in sorted(vals.items()) if v is not None
    ]
    if len(series) < 2:
        return _base(
            spec,
            src,
            "missing_data",
            label=label,
            region=reg["display"],
            reason=f"{reg['display']} {start_year}~{end_year}년 {spec['name']} 자료가 부족합니다.",
        )
    raw = [v for (_, _), v in sorted(vals.items()) if v is not None]
    first, last = raw[0], raw[-1]
    k = kind_of(spec)
    change = {
        "from_year": series[0]["year"],
        "to_year": series[-1]["year"],
        "diff": _round(_diff(first, last, spec), spec),
        "diff_unit": diff_unit(spec),
    }
    if k == "count" and first:
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    elif k in ("value", "ratio") and first:
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    years = series[-1]["year"] - series[0]["year"]
    if "rate" in change and change["diff_unit"] != "%p" and years and first > 0 and last > 0:
        change["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
    return _base(
        spec,
        src,
        label=label,
        region=reg["display"],
        sex="계" if src.get("label") else sex,
        series=series,
        change=change,
        notes=notes + ind_mod._change_notes(code, start_year, end_year),
        **({"extra_sources": earlier} if earlier else {}),
    )


def compare(spec, regions, year, sex="계", ages=None):
    prep = _prepare(spec, regions, year, year, sex, ages)
    if isinstance(prep, dict):
        return prep
    regs, src, age_codes, label, notes = prep
    vals = _fetch(src, [r["region_code"] for r in regs], year, year, age_codes)
    values = [
        {"region": r["display"], "value": _round(vals.get((year, r["region_code"])), spec)}
        for r in regs
    ]
    raw = {r["display"]: vals.get((year, r["region_code"])) for r in regs}
    in_order = [raw[r["display"]] for r in regs]
    values.sort(key=lambda x: (raw[x["region"]] is None, -(raw[x["region"]] or 0)))
    valid = [x for x in values if x["value"] is not None]
    gap = (
        _diff(raw[valid[-1]["region"]], raw[valid[0]["region"]], spec) if len(valid) >= 2 else None
    )
    out = _base(
        spec,
        src,
        label=label,
        year=year,
        sex="계" if src.get("label") else sex,
        values=values,
        highest=valid[0]["region"] if valid else None,
        lowest=valid[-1]["region"] if valid else None,
        gap=_round(gap, spec),
        gap_unit=diff_unit(spec),
        notes=notes,
    )
    if len(in_order) == 2 and None not in in_order and in_order[1]:
        out["ratio"] = {
            "a": regs[0]["display"],
            "b": regs[1]["display"],
            "value": round(analysis.times(in_order[0], in_order[1]), 2),
        }
    return out


def rank(
    spec, year, level="시도", sido=None, top=None, ascending=False, sex="계", ages=None, target=None
):
    src, err = _pick(spec, "시도" if level == "시도" and not sido else "시군구", year, year, sex)
    if err:
        return err
    try:
        age_codes, label, notes = _ages(src, ages)
    except AgeMismatch:
        return {
            "status": "unknown_indicator",
            "indicator": spec["name"],
            "reason": f"{josa(spec['name'], '은는')} 질문한 연령 구간으로 나눈 자료가 없습니다.",
        }
    cond = ind_mod.rank_where(level, sido)
    if isinstance(cond, dict):
        return {**cond, "indicator": spec["name"]}
    where, params, scope = cond
    regs = _rows(
        f"""
        select distinct r.region_code, r.region_name, r.kind, r.sido_name
        from regions r where {where}
          and (r.valid_from is null or r.valid_from <= ?)
          and (r.valid_to is null or r.valid_to >= ?)
        order by r.region_code
    """,
        [*params, year, year],
    )
    vals = _fetch(src, [r["region_code"] for r in regs], year, year, age_codes)
    items = []
    for reg in regs:
        v = vals.get((year, reg["region_code"]))
        if v is not None:
            items.append({"region": ind_mod._display(reg), "value": _round(v, spec)})
    items = analysis.rank_rows(items, descending=not ascending)
    out = _base(
        spec,
        src,
        label=label,
        year=year,
        sex="계" if src.get("label") else sex,
        level=scope,
        order="낮은 순" if ascending else "높은 순",
        total_regions=len(items),
        ranking=items[:top] if top else items,
        notes=notes,
    )
    if target:
        t = ind_mod.resolve_region(target, year)
        if t["status"] == "ok":
            hit = next((it for it in items if it["region"] == t["display"]), None)
            out["target"] = {
                "region": t["display"],
                "rank": hit["rank"] if hit else None,
                "value": hit["value"] if hit else None,
            }
    return out


def composition(spec, region, year, sex="계", ages=None, top=None):
    prep = _prepare(spec, [region], year, year, sex, ages)
    if isinstance(prep, dict):
        return prep
    (reg,), src, age_codes, label, notes = prep
    if "parts_dim" not in src:
        return _base(
            spec,
            src,
            "missing_data",
            label=label,
            region=reg["display"],
            reason=f"{reg['display']} {year}년 {spec['name']}의 구성 자료가 없습니다.",
        )
    dim = src["parts_dim"]
    code = reg["region_code"]
    filters = {k: v for k, v in src.get("filters", {}).items() if k != f"{dim}_id"}
    rows = _sum(src, [code], year, year, "item_id", filters, age_codes, group_dim=dim)
    by_code = {part: v for (_, _, part), v in rows.items()}
    names = dict(_dim_labels(src["table"], dim))
    total = by_code.get(src["parts_total"])
    if src.get("parts"):
        groups = {lbl: codes for lbl, codes in src["parts"].items()}
    else:
        codes = [c for c in names if c != src["parts_total"]]
        if src.get("leaf_only"):
            codes = [c for c in codes if not any(o != c and o.startswith(c) for o in codes)]
        skip = set(src.get("exclude_labels", []))
        groups = {names[c]: [c] for c in codes if names[c] not in skip}
    parts = []
    for lbl, codes in groups.items():
        vals = [by_code.get(c) for c in codes if by_code.get(c) is not None]
        if vals:
            parts.append({"label": lbl, "count": int(sum(vals))})
    if not total or not parts:
        return _base(
            spec,
            src,
            "missing_data",
            label=label,
            region=reg["display"],
            reason=f"{reg['display']} {year}년 {spec['name']} 자료가 없습니다.",
        )
    shares = analysis.composition({p["label"]: p["count"] for p in parts}, total)
    for p in parts:
        p["share"] = round(shares[p["label"]], 2)
    if not src.get("parts"):
        parts.sort(key=lambda p: -p["count"])
    item_cond, item_keys = _in("v.item_key", src["table"], 0, [_items(src)[0]])
    unit = _rows(
        "select mode() within group (order by u.name) as u"
        " from stat_value v join stat_unit u using (unit_key)"
        f" where v.tbl_key = ? and v.{YEARLY} and {item_cond}",
        [_tbl_key(src["table"]), *item_keys],
    )[0]["u"]
    subject = label + re.sub(r"\s*구성$", "", spec["name"])
    return _base(
        spec,
        src,
        label=label,
        region=reg["display"],
        year=year,
        sex=sex,
        by=subject,
        subject=subject,
        count_unit=unit or "명",
        total=int(total),
        parts=parts[:top] if top else parts,
        notes=notes,
    )


TERM_SUFFIX = re.compile(r"(수|율|률|비율|비중|건수|인구|현황|통계)$")
TERM_STOP = {"인구", "통계", "비율", "비중", "수", "명", "건", "얼마", "몇"}


@lru_cache
def _labels():
    rows = _rows(
        """
        select distinct t.tbl_id, c.tbl_nm, k.name as label
        from stat_code k
        join stat_table t using (tbl_key)
        join catalog c using (tbl_id)
        where k.dim_no in (0, 2, 3)
        union select tbl_id, tbl_nm, tbl_nm from catalog
    """
    )
    return tuple((r["tbl_id"], r["tbl_nm"], ind_mod._norm(r["label"])) for r in rows)


TABLE_CACHES = (
    _table,
    _tbl_key,
    _codes_db,
    _projection,
    _region_dim_db,
    _item_years_db,
    _dim_labels_db,
    _labels,
)
CHECK_EVERY = 30.0
_reload = {"checked_at": None, "version": None}
_reload_lock = threading.Lock()


def _load_version():
    r = _rows(
        """
        select (select count(*) from stat_load) as n,
               (select max(loaded_at) from stat_load) as loaded_at,
               (select max(finished_at) from app.load_jobs where status = 'done') as job_done_at
    """
    )[0]
    return r["n"], r["loaded_at"], r["job_done_at"]


def _forget_search_tables():
    from app.services import search

    search.mark_periods_stale()


def clear_table_caches():
    for cached in TABLE_CACHES:
        cached.cache_clear()
    _forget_search_tables()


def refresh_if_reloaded(now=None):
    now = time.monotonic() if now is None else now
    with _reload_lock:
        last = _reload["checked_at"]
        if last is not None and now - last < CHECK_EVERY:
            return False
        _reload["checked_at"] = now
    try:
        version = _load_version()
    except StatsNotLoadedError:
        return False
    with _reload_lock:
        changed = _reload["version"] is not None and version != _reload["version"]
        _reload["version"] = version
    if changed:
        for cached in TABLE_CACHES:
            cached.cache_clear()
        _forget_search_tables()
    return changed


def data_gap(term):
    words = []
    for w in re.findall(r"[가-힣A-Za-z0-9]+", str(term or "")):
        w = TERM_SUFFIX.sub("", w)
        if len(w) >= 2 and w not in TERM_STOP and not re.fullmatch(r"\d+(년|대|세)?", w):
            words.append(w)
    if not words:
        return {"gap": "not_collected"}
    related = {}
    for tbl_id, tbl_nm, label in _labels():
        if all(w in label for w in words):
            related[tbl_id] = f"{tbl_nm} ({tbl_id})"
    if related:
        return {"gap": "not_implemented", "related": sorted(related.values())[:3]}
    return {"gap": "not_collected"}
