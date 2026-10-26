"""조회 지표: KOSIS가 계산해 둔 값을 facts에서 꺼내 답한다 (지표 사전의 method = "lookup")

계산 지표(indicators.py)와 같은 모양의 결과를 돌려주므로 답변 문장(qa.summary)을 같이 쓴다.

  value(지표, 지역, 연도)          단순 조회     "2025년 인천 혼인 건수는?"
  trend(지표, 지역, 시작, 끝)      추이          "최근 5년 경기도 초혼 연령 변화는?"
  compare(지표, [지역...], 연도)   지역 비교     "2025년 결혼을 더 많이 한 곳은 부산, 대구?"
  rank(지표, 연도, 수준)           순위          "2025년 합계출산율이 가장 높은 시도 3곳은?"
  composition(지표, 지역, 연도)    구성비        "2024년 부산 전입 사유 중 직업 비율은?"

표 고르기: 지표 사전(config/catalog_notes.toml)의 sources 중 지역 수준과 연도가 맞는 첫 표.
  예) 합계출산율: 전국·시도 DT_1B81A21, 시군구 DT_1B81A23, 1993년 이전 전국은 INH_1B8000F_01
값 계산: 조건에 맞는 행을 더한다 (예: 셋째 이상 = 3아 + 4아 + ... + 8아 이상).
  den_item_id가 있으면 (값 ÷ 분모) × 100, age_dim이 있으면 질문의 연령 구간에 맞는 행만 더한다.
"""

import re
from functools import lru_cache

from app.core.duckdb import rows as _rows
from app.services import analysis
from app.services import indicators as ind_mod

LEVELS = {"전국": 0, "시도": 1, "시군구": 2}
REGION_LEVEL = {"전국": "전국", "시도": "시도", "시군구": "시군구", "일반구": "시군구"}
DIFF_UNIT = {"count": None, "ratio": "%p", "index": "포인트"}
SEX_WORDS = {"남": "남자", "여": "여자"}
# 분류 이름 → 연령 구간 (끝이 None이면 그 나이 이상)
AGE_NAMES = [
    (re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*세"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d+)\s*세\s*미만"), lambda m: (0, int(m[1]) - 1)),
    (re.compile(r"(\d+)\s*세\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*대"), lambda m: (int(m[1]), int(m[1]) + 9)),
]


class AgeMismatch(Exception):
    """질문의 연령 구간을 표의 연령 분류로 정확히 만들 수 없음"""


# ---- 지표 정의 -------------------------------------------------------------------


def kind_of(spec):
    """count(건수·인원) / ratio(%) / value(연령·율) / composition"""
    if spec.get("kind"):
        return spec["kind"]
    return {"명": "count", "건": "count", "%": "ratio"}.get(spec["unit"], "value")


def diff_unit(spec):
    k = kind_of(spec)
    return spec.get("diff_unit") or DIFF_UNIT.get(k) or spec["unit"]


def needs_split(spec, sex):
    """성별을 말하지 않았는데 표에 남녀 값만 있으면(예: 평균 초혼연령 남편·아내) 둘 다 보여준다"""
    return sex in (None, "", "계") and any(
        s.get("by_sex") and "계" not in s["by_sex"] for s in spec["sources"]
    )


def _effective(src, sex):
    """성별에 맞게 바꾼 출처 정의. 그 성별이 없으면 None"""
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
    """표 이름·기관·수록 기간·기준 시점"""
    c = _rows(
        "select tbl_nm, org_nm, start_year, end_year from catalog where tbl_id = ?", [tbl_id]
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
def _item_years(tbl_id, item_ids):
    """항목의 실제 수록 기간 (표 안에서도 항목마다 다를 수 있음, 예: 합계출산율은 1993년부터)"""
    marks = ", ".join("?" for _ in item_ids)
    r = _rows(
        "select min(year) as lo, max(year) as hi from facts"
        f" where tbl_id = ? and item_id in ({marks})",
        [tbl_id, *item_ids],
    )[0]
    return r["lo"], r["hi"]


def _items(src):
    v = src.get("item_id")
    return tuple(v) if isinstance(v, list) else (v,)


def _years(src):
    return _item_years(src["table"], _items(src))


# ---- 연령 구간 -------------------------------------------------------------------


def parse_age(text):
    """'20 - 24세', '20세미만', '85세이상', '20대' → (시작, 끝 또는 None). 연령이 아니면 None"""
    if "청년" in text:  # 청년층(19-34세) 같은 겹치는 묶음은 쓰지 않는다
        return None
    for pattern, to_band in AGE_NAMES:
        if m := pattern.search(text):
            return to_band(m)
    return None


@lru_cache
def _dim_labels(tbl_id, dim):
    return tuple(
        (r["id"], r["nm"])
        for r in _rows(
            f"select distinct {dim}_id as id, {dim}_nm as nm from facts"
            " where tbl_id = ? order by 1",
            [tbl_id],
        )
    )


def _age_codes(src, band):
    """연령 구간 → 더할 분류 코드 목록
    표의 최소 연령보다 넓게 물으면(예: 15세 이상, 표는 18세 이상) 합계"""
    lo, hi = band
    if hi is None and lo <= src.get("age_min", 0):
        return [src["age_total"]]
    found = []
    for code, name in _dim_labels(src["table"], src["age_dim"]):
        b = parse_age(name)
        if b and b[0] >= lo and (hi is None or (b[1] is not None and b[1] <= hi)):
            found.append((b, code))
    found.sort()
    bands = [b for b, _ in found]
    if not bands:
        raise AgeMismatch
    start_ok = bands[0][0] == lo or (bands[0][0] == 0 and lo <= src.get("age_min", 0))
    end_ok = bands[-1][1] == hi
    gaps = any(b[0] != a[1] + 1 for a, b in zip(bands, bands[1:], strict=False) if a[1] is not None)
    if not start_ok or not end_ok or gaps:
        raise AgeMismatch
    return [code for _, code in found]


# ---- 조회 ------------------------------------------------------------------------


def _conds(src, item_key, filters, age_codes):
    items = src.get(item_key) or src.get("item_id")
    items = items if isinstance(items, list) else [items]
    conds = [f"item_id in ({', '.join('?' for _ in items)})"]
    params = list(items)
    for col, val in filters.items():
        vals = val if isinstance(val, list) else [val]
        conds.append(f"{col} in ({', '.join('?' for _ in vals)})")
        params += vals
    if src.get("age_dim") and age_codes is not None:
        conds.append(f"{src['age_dim']}_id in ({', '.join('?' for _ in age_codes)})")
        params += age_codes
    return conds, params


def _sum(src, codes, y0, y1, item_key, filters, age_codes, group_dim=None):
    """조건에 맞는 행의 합 {(연도, 지역 코드[, 분류 코드]): 값}. 모두 결측이면 None"""
    conds, params = _conds(src, item_key, filters, age_codes)
    if src.get("national_only"):
        region_sql, region_params = "region_code is null", []
    else:
        region_sql = f"region_code in ({', '.join('?' for _ in codes)})"
        region_params = list(codes)
    part = f", {group_dim}_id as part" if group_dim else ""
    rows = _rows(
        f"""
        select year, coalesce(region_code, '00') as rc{part},
               sum(value) filter (where not is_missing) as v,
               count(*) filter (where not is_missing) as n
        from facts
        where tbl_id = ? and year between ? and ? and {region_sql} and {" and ".join(conds)}
        group by all
    """,
        [src["table"], y0, y1, *region_params, *params],
    )

    def key(r):
        return (r["year"], r["rc"], r["part"]) if group_dim else (r["year"], r["rc"])

    return {key(r): (r["v"] if r["n"] else None) for r in rows}


def _fetch(src, codes, y0, y1, age_codes=None):
    """{(연도, 지역 코드): 값}. 분모가 있으면 비율(%)"""
    num = _sum(src, codes, y0, y1, "item_id", src.get("filters", {}), age_codes)
    if not src.get("den_item_id"):
        return num
    den_filters = src.get("den_filters") or src.get("filters", {})
    den = _sum(src, codes, y0, y1, "den_item_id", den_filters, age_codes)
    return {k: (v / den[k] * 100 if v is not None and den.get(k) else None) for k, v in num.items()}


def _round(v, spec):
    if v is None:
        return None
    if kind_of(spec) == "count":
        return int(round(v))
    return round(v, 3 if abs(v) < 10 and kind_of(spec) == "value" else 2)


# ---- 지역·표 고르기 ----------------------------------------------------------------


def _resolve(spec, names, year):
    """지역 이름들 → 지역 정보 목록 또는 오류 dict"""
    sigungu = any(s["level"] == "시군구" for s in spec["sources"])
    regs = []
    for name in names:
        reg = ind_mod.resolve_region(name, year, sigungu=sigungu)
        if reg["status"] != "ok":
            return {**reg, "indicator": spec["name"]}
        regs.append(ind_mod.in_year(reg, year))
    return regs


def _pick(spec, level, y0, y1, sex):
    """지역 수준·연도에 맞는 출처. (출처, 오류 dict)"""
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
            "status": "missing_data",
            "indicator": spec["name"],
            "reason": f"{spec['name']}은(는) {top} 단위까지만 자료가 있습니다.",
        }
    for s in fits:
        lo, hi = _years(s)
        if lo <= y0 and y1 <= hi:
            return s, None
    spans = [_years(s) for s in fits]
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    return None, {
        "status": "out_of_range",
        "indicator": spec["name"],
        "reason": f"{spec['name']} 자료는 {lo}~{hi}년만 있습니다.",
        "available_years": [lo, hi],
    }


def _level(regs):
    return max((REGION_LEVEL.get(r["kind"], "시군구") for r in regs), key=LEVELS.get)


def _ages(src, ages):
    """(더할 연령 코드 또는 None, 이름 앞에 붙일 연령 표현, 주의사항) 또는 AgeMismatch"""
    if not ages:
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


def _base(spec, src, status="ok", label="", **extra):
    tbl = _table(src["table"])
    name = label + spec["name"] + (f"({src['label']})" if src.get("label") else "")
    notes = [spec["note"]] if spec.get("note") else []
    if src.get("national_only"):
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
    """(지역 목록, 출처, 연령 코드, 이름 앞 연령 표현, 주의사항) 또는 오류 dict"""
    regs = _resolve(spec, names, y1)
    if isinstance(regs, dict):
        return regs
    src, err = _pick(spec, _level(regs), y0, y1, sex)
    if err:
        return {**err, "region": ", ".join(r["display"] for r in regs)}
    try:
        age_codes, label, notes = _ages(src, ages)
    except AgeMismatch:
        return {
            "status": "unknown_indicator",
            "indicator": spec["name"],
            "reason": f"{spec['name']}은(는) 질문한 연령 구간으로 나눈 자료가 없습니다.",
        }
    notes += [r["note"] for r in regs if r.get("note")]
    return regs, src, age_codes, label, notes


# ---- 공개 함수 -------------------------------------------------------------------


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


def trend(spec, region, start_year, end_year, sex="계", ages=None):
    prep = _prepare(spec, [region], start_year, end_year, sex, ages)
    if isinstance(prep, dict):
        return prep
    (reg,), src, age_codes, label, notes = prep
    code = reg["region_code"]
    vals = _fetch(src, [code], start_year, end_year, age_codes)
    series = [
        {"year": y, "value": _round(v, spec)}
        for (y, _), v in sorted(vals.items())
        if v is not None
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
    first, last = series[0]["value"], series[-1]["value"]
    k = kind_of(spec)
    change = {
        "from_year": series[0]["year"],
        "to_year": series[-1]["year"],
        "diff": _round(last - first, spec),
        "diff_unit": diff_unit(spec),
    }
    if k == "count" and first:
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    elif k in ("value", "ratio") and first:
        # 율 지표도 "증감률"을 물으면 쓴다
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    return _base(
        spec,
        src,
        label=label,
        region=reg["display"],
        sex="계" if src.get("label") else sex,
        series=series,
        change=change,
        notes=notes + ind_mod._change_notes(code, start_year, end_year),
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
    in_order = [v["value"] for v in values]
    values.sort(key=lambda x: (x["value"] is None, -(x["value"] or 0)))
    valid = [x for x in values if x["value"] is not None]
    gap = valid[0]["value"] - valid[-1]["value"] if len(valid) >= 2 else None
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
            "value": round(in_order[0] / in_order[1], 2),
        }
    return out


def rank(
    spec, year, level="시도", sido=None, top=None, ascending=False, sex="계", ages=None, target=None
):
    """순위. target(지역 이름)을 주면 그 지역이 몇 위인지도 알려준다"""
    src, err = _pick(spec, "시도" if level == "시도" and not sido else "시군구", year, year, sex)
    if err:
        return err
    try:
        age_codes, label, notes = _ages(src, ages)
    except AgeMismatch:
        return {
            "status": "unknown_indicator",
            "indicator": spec["name"],
            "reason": f"{spec['name']}은(는) 질문한 연령 구간으로 나눈 자료가 없습니다.",
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
    """,
        [*params, year, year],
    )
    vals = _fetch(src, [r["region_code"] for r in regs], year, year, age_codes)
    items = []
    for reg in regs:
        v = vals.get((year, reg["region_code"]))
        if v is not None:
            items.append({"region": ind_mod._display(reg), "value": _round(v, spec)})
    items.sort(key=lambda x: x["value"], reverse=not ascending)
    for i, it in enumerate(items, 1):
        it["rank"] = i
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
            out["target"] = {"region": t["display"], "rank": hit["rank"] if hit else None,
                             "value": hit["value"] if hit else None}
    return out


def composition(spec, region, year, sex="계", ages=None, top=None):
    """한 지역·한 연도의 구성비. parts가 없으면 분류의 모든 값(leaf_only면 가장 아래 분류만)"""
    prep = _prepare(spec, [region], year, year, sex, ages)
    if isinstance(prep, dict):
        return prep
    (reg,), src, age_codes, label, notes = prep
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
    unit = _rows(
        "select any_value(unit) as u from facts where tbl_id = ? and item_id = ?",
        [src["table"], _items(src)[0]],
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
