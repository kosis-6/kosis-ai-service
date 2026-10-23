"""지표 계산 (하위 태스크 4: 수치 분석)

LLM이 숫자를 직접 계산하지 않도록, 질문 유형별 계산을 함수로 제공한다.
모든 함수는 JSON으로 바꿀 수 있는 dict를 돌려준다.

  value(지표, 지역, 연도)            단순 조회   "2025년 서울 고령인구 비율은?"
  trend(지표, 지역, 시작, 끝)        추이 분석   "최근 5년 부산 고령인구 비율 변화는?" (증감률 포함)
  compare(지표, [지역...], 연도)     지역 비교   "서울과 부산 중 어디가 높아?"
  rank(지표, 연도, 수준)             순위        "고령인구 비율이 가장 높은 시도는?"
  projection(지표, 지역, 연도)       장래추계    수록 기간 이후 연도 안내용

결과의 status
  ok                  계산 성공
  unknown_indicator   지원하지 않는 지표
  unknown_region      모르는 지역
  ambiguous_region    같은 이름의 지역이 여럿 (예: 중구) → candidates로 되묻기
  out_of_range        수록 기간 밖 연도 → 추계가 있으면 suggestion에 담음 (답변 불가 사례 1)
  region_not_in_year  그 연도에 없던 지역 (예: 2025년 전남광주통합특별시)

실행(예시 질문 데모): python -m app.services.indicators
"""

import json
import re

from app.core.duckdb import CatalogException
from app.core.duckdb import rows as _rows
from app.services import analysis

POP_TABLE = "DT_1B04006"
POP_SOURCE = "국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006)"
POP_BASIS = "주민등록인구, 각 연도 12월 31일 기준"
PROJ_SOURCE = "국가데이터처 (구 통계청), 장래인구추계 시도편 주요 인구지표 (DT_1BPB002)"
PROJ_BASIS = "2022년 기준 장래인구추계, 각 연도 7월 1일 기준 (외국인 포함)"
PROJ_TABLE = "DT_1BPB002"
DEFAULT_SCENARIO = "중위 추계"

# kind에 따라 차이·변화의 단위가 달라진다
#   count : 사람 수   → 차이는 명, 변화는 증감률(%)
#   ratio : 비율(%)   → 차이·변화는 %p
#   index : 지수      → 차이·변화는 포인트
INDICATORS = {
    "총인구": {
        "unit": "명",
        "kind": "count",
        "formula": "주민등록 총인구",
        "calc": lambda c: c["total"],
        "projection_c3": "10",
    },
    "고령인구 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "65세 이상 인구 ÷ 총인구 × 100",
        "calc": lambda c: _div(c["old"], c["total"]),
        "projection_c3": "33",
    },
    "유소년인구 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "0~14세 인구 ÷ 총인구 × 100",
        "calc": lambda c: _div(c["young"], c["total"]),
        "projection_c3": "31",
    },
    "노령화지수": {
        "unit": "유소년인구 100명당 명",
        "kind": "index",
        "formula": "65세 이상 인구 ÷ 0~14세 인구 × 100",
        "calc": lambda c: _div(c["old"], c["young"]),
        "projection_c3": "50",
    },
}
# 지표의 다른 이름(별칭)은 config/catalog_notes.toml의 지표 사전에서 읽는다 (_get_indicator)
DIFF_UNIT = {"count": "명", "ratio": "%p", "index": "포인트"}
SEXES = ("계", "남", "여")


# ---- 내부 도우미 ----------------------------------------------------------------


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def _div(a, b):
    return a / b * 100 if a is not None and b else None


def _round(v, kind):
    if v is None:
        return None
    return int(v) if kind == "count" else round(v, 2)


def _data_years():
    r = _rows("select min(year) as lo, max(year) as hi from population")[0]
    return r["lo"], r["hi"]


def _get_indicator(indicator):
    """지표 이름·별칭 → (지표 이름, 정의). 별칭은 카탈로그 지표 사전에서 읽고, 공백은 무시한다."""
    key = _norm(indicator)
    aliases = {name: [] for name in INDICATORS}
    for r in _catalog_rows("select name, aliases from indicator_dict where method = 'computed'"):
        if r["name"] in aliases:
            aliases[r["name"]] = json.loads(r["aliases"])
    for name, names in aliases.items():
        if key in {_norm(n) for n in [name, *names]}:
            return name, INDICATORS[name]
    return None, None


def _base(name, ind, status="ok", **extra):
    out = {
        "status": status,
        "indicator": name,
        "unit": ind["unit"],
        "formula": ind["formula"],
        "source": POP_SOURCE,
        "basis": POP_BASIS,
    }
    out.update(extra)
    return out


def _display(reg):
    if reg["kind"] in ("전국", "시도") or not reg.get("sido_name"):
        return reg["region_name"]
    return f"{reg['sido_name']} {reg['region_name']}"


def _change_notes(region_code, y0=None, y1=None):
    """지역에 영향을 준 행정구역 변경 이력 (y0 < 변경 연도 <= y1 범위만, 범위가 없으면 전부)

    시도는 시도 경계가 바뀐 변경(코드 목록에 시도가 2개 이상, 예: 군위군 편입)과 자기 코드만 본다.
    """
    notes = []
    for ch in _rows("select year, description, region_codes from region_changes order by year"):
        if y0 is not None and not (y0 < ch["year"] <= y1):
            continue
        codes = ch["region_codes"].split(",")
        if region_code in codes:
            hit = True
        elif len(region_code) == 2 and region_code != "00":
            sidos = {c[:2] for c in codes}
            hit = len(sidos) > 1 and region_code in sidos
        else:
            hit = False
        if hit:
            notes.append(f"{ch['year']}년: {ch['description']}")
    return notes


def _catalog_rows(sql, params=()):
    """catalog.py가 만든 테이블 조회. catalog.py를 아직 안 돌렸으면 빈 목록"""
    try:
        return _rows(sql, params)
    except CatalogException:
        return []


def _series_break_notes(y0, y1):
    """주민등록인구 통계 기준이 바뀐 해 중 y0 < 연도 <= y1 (그 해 변화에 기준 변경 효과가 섞임)"""
    rows = _catalog_rows(
        """
        select year, description from series_breaks
        where tbl_id = ? and year > ? and year <= ? order by year
    """,
        [POP_TABLE, y0, y1],
    )
    return [f"{r['year']}년: {r['description']}" for r in rows]


def _out_of_range_reason(lo, hi):
    reason = f"수집된 주민등록인구 자료는 {lo}~{hi}년(연간)만 있습니다."
    rows = _catalog_rows("select latest_available from table_notes where tbl_id = ?", [POP_TABLE])
    if rows and rows[0]["latest_available"]:
        reason += f" KOSIS 원표에는 {rows[0]['latest_available']}까지 자료가 있습니다."
    return reason


def _components(codes, y0, y1, sex):
    """연도·지역별 총인구, 0~14세, 65세 이상 (100세 이상 포함)"""
    marks = ", ".join("?" for _ in codes)
    rows = _rows(
        f"""
        select year, region_code,
               sum(population) filter (where age is null)          as total,
               sum(population) filter (where age between 0 and 14) as young,
               sum(population) filter (where age >= 65)            as old
        from population
        where sex = ? and year between ? and ? and region_code in ({marks})
        group by year, region_code
        order by region_code, year
    """,
        [sex, y0, y1, *codes],
    )
    return {(r["year"], r["region_code"]): r for r in rows}


def _check(name, ind, reg, y0, y1):
    """수록 기간·지역 유효 기간 확인. 문제가 있으면 안내용 dict, 없으면 None"""
    lo, hi = _data_years()
    if y0 < lo or y1 > hi:
        out = _base(
            name,
            ind,
            "out_of_range",
            region=_display(reg),
            reason=_out_of_range_reason(lo, hi),
            available_years=[lo, hi],
        )
        if y1 > hi and reg["kind"] in ("전국", "시도"):
            proj = projection(name, reg["region_code"], y1)
            if proj["status"] == "ok":
                out["suggestion"] = proj
        return out
    vf, vt = reg.get("valid_from"), reg.get("valid_to")
    if (vf is not None and y0 < vf) or (vt is not None and y1 > vt):
        if vf is not None and vt is not None:
            period = f"{vf}~{vt}년에만"
        elif vf is not None:
            period = f"{vf}년부터"
        else:
            period = f"{vt}년까지"
        return _base(
            name,
            ind,
            "region_not_in_year",
            region=_display(reg),
            reason=f"{_display(reg)}은(는) {period} 있는 지역입니다.",
            notes=_change_notes(reg["region_code"]),
        )
    return None


def _prepare(indicator, region_names, year=None):
    """지표·지역 해석. (지표 이름, 지표 정의, 지역 목록, 오류 dict)"""
    name, ind = _get_indicator(indicator)
    if name is None:
        return (
            None,
            None,
            None,
            {"status": "unknown_indicator", "indicator": indicator, "supported": list(INDICATORS)},
        )
    regions = []
    for rn in region_names:
        reg = resolve_region(rn, year)
        if reg["status"] != "ok":
            return name, ind, None, {**reg, "indicator": name}
        regions.append(reg)
    return name, ind, regions, None


# ---- 공개 함수 ------------------------------------------------------------------


def resolve_region(name, year=None):
    """지역 이름·별칭·코드 → 지역 정보

    "서울", "서울특별시", "11" → 서울특별시
    "중구" → ambiguous_region (후보 목록)
    "서울 중구" → 서울특별시 중구 (시도 + 시군구)
    같은 이름에 코드가 여럿이면(예: 부천 원미구) year로 그 해에 있던 코드를 고른다.
    """
    sql = """
        select distinct r.region_code, r.region_name, r.kind, r.sido_code, r.sido_name,
                        r.valid_from, r.valid_to
        from regions r left join region_aliases a using (region_code)
        where (replace(a.alias, ' ', '') = ? or r.region_code = ?) {extra}
        order by r.region_code
    """
    key = _norm(name)
    found = _rows(sql.format(extra=""), [key, key])

    parts = str(name).split()
    if not found and len(parts) >= 2:  # "서울 중구"
        sido = resolve_region(parts[0])
        if sido["status"] == "ok" and sido["kind"] == "시도":
            rest = _norm("".join(parts[1:]))
            found = _rows(
                sql.format(extra="and r.sido_code = ?"), [rest, rest, sido["region_code"]]
            )

    if len(found) > 1 and year is not None:
        found = [
            r
            for r in found
            if (r["valid_from"] is None or year >= r["valid_from"])
            and (r["valid_to"] is None or year <= r["valid_to"])
        ] or found

    if not found:
        return {
            "status": "unknown_region",
            "region": name,
            "reason": f"'{name}'에 해당하는 지역을 찾지 못했습니다.",
        }
    if len(found) > 1:
        return {
            "status": "ambiguous_region",
            "region": name,
            "reason": f"'{name}'에 해당하는 지역이 여러 곳입니다. 어느 지역인지 확인이 필요합니다.",
            "candidates": [_display(r) for r in found],
        }
    return {"status": "ok", **found[0], "display": _display(found[0])}


def value(indicator, region, year, sex="계"):
    """단순 조회: 한 지역·한 연도의 지표 값"""
    name, ind, regs, err = _prepare(indicator, [region], year)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, year, year):
        return bad
    comp = _components([reg["region_code"]], year, year, sex).get((year, reg["region_code"]))
    v = ind["calc"](comp) if comp else None
    return _base(name, ind, region=reg["display"], year=year, sex=sex, value=_round(v, ind["kind"]))


def trend(indicator, region, start_year, end_year, sex="계"):
    """추이 분석: 연도별 값 + 처음 대비 마지막 변화 (총인구면 증감률 %)"""
    name, ind, regs, err = _prepare(indicator, [region], end_year)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, start_year, end_year):
        return bad
    comps = _components([reg["region_code"]], start_year, end_year, sex)
    series = [
        {"year": y, "value": _round(ind["calc"](c), ind["kind"])}
        for (y, _), c in sorted(comps.items())
    ]
    first, last = series[0]["value"], series[-1]["value"]
    change = {
        "from_year": series[0]["year"],
        "to_year": series[-1]["year"],
        "diff": _round(last - first, ind["kind"])
        if ind["kind"] == "count"
        else round(last - first, 2),
        "diff_unit": DIFF_UNIT[ind["kind"]],
    }
    if ind["kind"] == "count":
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    return _base(
        name,
        ind,
        region=reg["display"],
        sex=sex,
        series=series,
        change=change,
        notes=_change_notes(reg["region_code"], start_year, end_year)
        + _series_break_notes(start_year, end_year),
    )


def compare(indicator, regions, year, sex="계"):
    """지역 비교: 여러 지역의 같은 연도 값, 높은 순 정렬 + 최고·최저 차이"""
    name, ind, regs, err = _prepare(indicator, regions, year)
    if err:
        return err
    for reg in regs:
        if bad := _check(name, ind, reg, year, year):
            return bad
    comps = _components([r["region_code"] for r in regs], year, year, sex)
    values = []
    for reg in regs:
        comp = comps.get((year, reg["region_code"]))
        values.append(
            {
                "region": reg["display"],
                "value": _round(ind["calc"](comp), ind["kind"]) if comp else None,
            }
        )
    values.sort(key=lambda x: (x["value"] is None, -(x["value"] or 0)))
    valid = [x for x in values if x["value"] is not None]
    gap = valid[0]["value"] - valid[-1]["value"] if len(valid) >= 2 else None
    return _base(
        name,
        ind,
        year=year,
        sex=sex,
        values=values,
        highest=valid[0]["region"] if valid else None,
        lowest=valid[-1]["region"] if valid else None,
        gap=_round(gap, ind["kind"])
        if ind["kind"] == "count"
        else (round(gap, 2) if gap is not None else None),
        gap_unit=DIFF_UNIT[ind["kind"]],
    )


def rank(indicator, year, level="시도", sido=None, top=None, ascending=False, sex="계"):
    """순위: 시도 전체 또는 시군구(기초자치단체) 전체 순위. sido를 주면 그 시도 안의 시군구만"""
    name, ind = _get_indicator(indicator)
    if name is None:
        return {
            "status": "unknown_indicator",
            "indicator": indicator,
            "supported": list(INDICATORS),
        }
    lo, hi = _data_years()
    if not lo <= year <= hi:
        return _base(
            name, ind, "out_of_range", reason=_out_of_range_reason(lo, hi), available_years=[lo, hi]
        )

    where, params = ("r.kind = '시도'", []) if level == "시도" else ("r.is_basic", [])
    if sido:
        s = resolve_region(sido)
        if s["status"] != "ok":
            return {**s, "indicator": name}
        where += " and r.sido_code = ?"
        params.append(s["region_code"])
    regs = _rows(
        f"""
        select distinct r.region_code, r.region_name, r.kind, r.sido_name
        from regions r join population p on p.region_code = r.region_code and p.year = ?
        where {where}
    """,
        [year, *params],
    )
    comps = _components([r["region_code"] for r in regs], year, year, sex)
    items = []
    for reg in regs:
        comp = comps.get((year, reg["region_code"]))
        v = ind["calc"](comp) if comp else None
        if v is not None:
            items.append({"region": _display(reg), "value": _round(v, ind["kind"])})
    items.sort(key=lambda x: x["value"], reverse=not ascending)
    for i, it in enumerate(items, 1):
        it["rank"] = i
    return _base(
        name,
        ind,
        year=year,
        sex=sex,
        level=level,
        order="낮은 순" if ascending else "높은 순",
        total_regions=len(items),
        ranking=items[:top] if top else items,
    )


def projection(indicator, region, year, scenario=DEFAULT_SCENARIO):
    """장래인구추계 값 (전국·시도만). 실적(주민등록)과 기준이 달라 직접 이어 비교하지 않는다."""
    name, ind = _get_indicator(indicator)
    if name is None:
        return {
            "status": "unknown_indicator",
            "indicator": indicator,
            "supported": list(INDICATORS),
        }
    reg = resolve_region(region, year)
    if reg["status"] != "ok":
        return {**reg, "indicator": name}
    rows = _rows(
        """
        select year, value, unit, is_projection from facts
        where tbl_id = ? and region_code = ? and c3_id = ? and year = ? and scenario = ?
              and not is_missing
    """,
        [PROJ_TABLE, reg["region_code"], ind["projection_c3"], year, scenario],
    )
    if not rows:
        return {
            "status": "out_of_range",
            "indicator": name,
            "region": reg["display"],
            "reason": f"{reg['display']} {year}년 장래인구추계({scenario}) 값이 없습니다.",
        }
    r = rows[0]
    notes = [
        "실제 통계가 아닌 추계값입니다.",
        "주민등록인구와 기준(시점, 외국인 포함 여부)이 달라 실적과 직접 이어서 비교하지 않습니다.",
    ]
    # 추계표의 지역별 주의사항 (예: 대구는 군위군 미포함)
    notes += [
        n["description"]
        for n in _catalog_rows(
            """
        select description from region_notes
        where tbl_id = ? and region_code = ? and (year is null or year = ?)
    """,
            [PROJ_TABLE, reg["region_code"], year],
        )
    ]
    # 그 연도에는 없어진 지역 (예: 2030년 광주광역시 → 전남광주통합특별시로 통합)
    if reg.get("valid_to") is not None and year > reg["valid_to"]:
        notes.append(
            f"{reg['display']}은(는) {reg['valid_to']}년 이후 행정구역이 바뀌었습니다. "
            "추계값은 2022년 기준 추계 당시의 행정구역 기준입니다."
        )
        notes += _change_notes(reg["region_code"])
    return {
        "status": "ok",
        "indicator": name,
        "region": reg["display"],
        "year": year,
        "value": r["value"],
        "unit": r["unit"],
        "is_projection": r["is_projection"],
        "scenario": scenario,
        "source": PROJ_SOURCE,
        "basis": PROJ_BASIS,
        "notes": notes,
    }


# ---- 데모: 계획서 예시 질문 --------------------------------------------------------

if __name__ == "__main__":
    demos = [
        (
            "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?",
            lambda: compare("고령인구 비율", ["서울", "부산"], 2025),
        ),
        ("최근 5년간 부산 고령화율 변화는?", lambda: trend("고령화율", "부산", 2020, 2025)),
        (
            "2025년 고령인구 비율이 가장 높은 시도 3곳은?",
            lambda: rank("고령인구 비율", 2025, top=3),
        ),
        ("2015~2025년 대구 인구 증감률은?", lambda: trend("총인구", "대구", 2015, 2025)),
        (
            "2030년 서울 고령인구 비율은? (수록 기간 밖)",
            lambda: value("고령인구 비율", "서울", 2030),
        ),
        ("2025년 전남광주 인구는? (출범 전)", lambda: value("인구", "전남광주", 2025)),
        ("중구 인구는? (같은 이름 여럿)", lambda: value("인구", "중구", 2025)),
        ("서울 중구 노령화지수는?", lambda: value("노령화지수", "서울 중구", 2025)),
    ]
    for question, fn in demos:
        print(f"\n### {question}")
        print(json.dumps(fn(), ensure_ascii=False, indent=2))
