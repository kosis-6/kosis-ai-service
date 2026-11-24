import json
import re
import tomllib
from functools import lru_cache
from pathlib import Path

from app.core.stats_db import StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import analysis, population_latest
from app.services.josa import josa

POP_TABLE = "DT_1B04006"
POP_SOURCE = "국가데이터처 (구 통계청), 행정구역(시군구)별/1세별 주민등록인구 (DT_1B04006)"
POP_BASIS = "주민등록인구, 각 연도 12월 31일 기준"
AREA_SOURCE = "국토교통부 지적통계, 행정구역별·지목별 국토이용현황_시군구 (DT_MLTM_2300)"
AREA_TABLE = "DT_MLTM_2300"
AREA_ORG = "116"
AREA_BASIS = "지목 전체(계) 면적, 각 연도 12월 31일 기준"
HH_TABLE = "DT_1B040B3"
HH_SOURCE = "행정안전부, 행정구역(시군구)별 주민등록세대수 (DT_1B040B3)"
HH_BASIS = "주민등록 세대수, 각 연도 12월 31일 기준"
FOREIGN_TABLE = "DT_1B040A9C"
FOREIGN_ORG = "111"
FOREIGN_SOURCE = "법무부, 시군구별 및 국적(지역)별 등록외국인 현황 (DT_1B040A9C)"
FOREIGN_BASIS = "법무부 출입국·외국인정책 통계의 등록외국인"
FOREIGN_SEX = {"계": "0", "남": "1", "여": "2"}
PROJ_SOURCE = "국가데이터처 (구 통계청), 장래인구추계 시도편 주요 인구지표 (DT_1BPB002)"
PROJ_BASIS = "2022년 기준 장래인구추계, 각 연도 7월 1일 기준 (외국인 포함)"
PROJ_TABLE = "DT_1BPB002"
DEFAULT_SCENARIO = "중위 추계"
KOSIS_URL = "https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"

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
        "operands": [("old", "65세 이상 인구", "명"), ("total", "총인구", "명")],
    },
    "유소년인구 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "0~14세 인구 ÷ 총인구 × 100",
        "calc": lambda c: _div(c["young"], c["total"]),
        "projection_c3": "31",
        "operands": [("young", "0~14세 인구", "명"), ("total", "총인구", "명")],
    },
    "노령화지수": {
        "unit": "유소년인구 100명당 명",
        "kind": "index",
        "formula": "65세 이상 인구 ÷ 0~14세 인구 × 100",
        "calc": lambda c: _div(c["old"], c["young"]),
        "projection_c3": "50",
        "meaning": "유소년인구(0~14세) 100명당 65세 이상 인구가 {value}명",
    },
    "생산연령인구 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "15~64세 인구 ÷ 총인구 × 100",
        "calc": lambda c: _div(c["working"], c["total"]),
        "operands": [("working", "15~64세 인구", "명"), ("total", "총인구", "명")],
    },
    "노년부양비": {
        "unit": "생산연령인구 100명당 명",
        "kind": "index",
        "formula": "65세 이상 인구 ÷ 15~64세 인구 × 100",
        "calc": lambda c: _div(c["old"], c["working"]),
        "meaning": "생산연령인구(15~64세) 100명당 65세 이상 인구가 {value}명",
    },
    "노인 1명당 생산연령인구": {
        "unit": "명",
        "kind": "value",
        "diff_unit": "명",
        "formula": "15~64세 인구 ÷ 65세 이상 인구",
        "calc": lambda c: c["working"] / c["old"] if c.get("old") else None,
        "meaning": "65세 이상 인구 1명당 생산연령인구(15~64세)가 {value}명"
        " (실제 부양자 수가 아닌 연령별 인구 기준)",
    },
    "유소년부양비": {
        "unit": "생산연령인구 100명당 명",
        "kind": "index",
        "formula": "0~14세 인구 ÷ 15~64세 인구 × 100",
        "calc": lambda c: _div(c["young"], c["working"]),
        "meaning": "생산연령인구(15~64세) 100명당 0~14세 인구가 {value}명",
    },
    "총부양비": {
        "unit": "생산연령인구 100명당 명",
        "kind": "index",
        "formula": "(0~14세 인구 + 65세 이상 인구) ÷ 15~64세 인구 × 100",
        "calc": lambda c: _div((c["young"] or 0) + (c["old"] or 0), c["working"]),
        "meaning": "생산연령인구(15~64세) 100명당 부양할 인구(0~14세 + 65세 이상)가 {value}명",
    },
    "성비": {
        "unit": "여자 100명당 남자 수",
        "kind": "index",
        "formula": "남자 인구 ÷ 여자 인구 × 100",
        "calc": lambda c: _div(c.get("male"), c.get("female")),
        "sexes": True,
        "meaning": "여자 100명당 남자가 {value}명",
    },
    "중위연령": {
        "unit": "세",
        "kind": "value",
        "diff_unit": "세",
        "digits": 1,
        "formula": "인구를 나이순으로 세웠을 때 가운데 사람의 나이 (1세별 인구로 보간)",
        "calc": lambda c: c.get("median_age"),
        "median": True,
        "meaning": "인구의 절반이 {value}세 미만",
    },
    "세대당 인구": {
        "unit": "명",
        "kind": "value",
        "diff_unit": "명",
        "formula": "주민등록 총인구 ÷ 주민등록 세대수",
        "rate": True,
        "calc": lambda c: c["total"] / c["households"] if c.get("households") else None,
        "households": True,
        "meaning": "한 세대에 평균 {value}명",
        "operands": [("total", "주민등록인구", "명"), ("households", "주민등록 세대수", "세대")],
    },
    "등록외국인 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "등록외국인 수 ÷ 주민등록 총인구 × 100",
        "calc": lambda c: _div(c.get("foreigners"), c["total"]),
        "foreigners": True,
        "operands": [("foreigners", "등록외국인", "명"), ("total", "주민등록인구", "명")],
    },
    "인구밀도": {
        "unit": "명/㎢",
        "kind": "value",
        "diff_unit": "명/㎢",
        "digits": 1,
        "rate": True,
        "formula": "주민등록 총인구 ÷ 행정구역 면적(㎢)",
        "calc": lambda c: c["total"] / c["area"] if c.get("area") else None,
        "area": True,
        "meaning": "1㎢(가로세로 1km 넓이)마다 평균 {value}명",
        "operands": [("total", "주민등록인구", "명"), ("area", "면적", "㎢")],
    },
}
DIFF_UNIT = {"count": "명", "ratio": "%p", "index": "포인트"}
SEXES = ("계", "남", "여")
NOTES_PATH = Path(__file__).resolve().parents[2] / "config" / "catalog_notes.toml"
AMBIGUOUS_NAMES = {
    "충청도": ["충청북도", "충청남도"],
    "충청": ["충청북도", "충청남도"],
    "경상도": ["경상북도", "경상남도"],
    "전라도": ["전북특별자치도", "전라남도"],
}


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def _div(a, b):
    return analysis.share(a, b) if a is not None and b else None


def _diff(base, current, kind):
    return (analysis.point_change if kind == "ratio" else analysis.change)(base, current)


def _diff_unit(ind):
    return ind.get("diff_unit") or DIFF_UNIT[ind["kind"]]


def _round(v, kind, digits=2):
    if v is None:
        return None
    return int(v) if kind == "count" else round(v, digits)


def _operand(v):
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(v, 2)


def _r(v, ind):
    return _round(v, ind["kind"], ind.get("digits", 2))


def _data_years():
    r = _rows("select min(year) as lo, max(year) as hi from population")[0]
    return r["lo"], r["hi"]


def spec_tables():
    return sorted(
        {
            src["table"]
            for s in specs().values()
            for src in s.get("sources") or []
            if not src.get("years")
        }
    )


@lru_cache
def specs():
    with open(NOTES_PATH, "rb") as f:
        return {ind["name"]: ind for ind in tomllib.load(f)["indicators"]}


def spec(indicator):
    name = canonical_name(indicator or "")
    return specs().get(name) if name else None


ALT_NOTES = {
    "인구총조사": "인구총조사 기준입니다. 실제로 사는 곳에서 외국인까지 센 인구(11월 1일)라, "
    "주소를 둔 곳에서 내국인만 센 주민등록인구와 값이 다릅니다",
    "장래인구추계": "장래인구추계(중위 추계) 기준입니다. 인구총조사를 바탕으로 7월 1일 인구를 "
    "추정한 값(외국인 포함)이라, 주소를 둔 곳에서 내국인만 센 주민등록인구와 값이 다릅니다",
}


def alt_spec(sp, survey):
    alts = [a for a in sp.get("alt_sources") or [] if a.get("survey") == survey]
    if not alts:
        return None
    base = INDICATORS.get(sp["name"], {})
    out = {
        "name": sp["name"],
        "unit": sp.get("unit") or base.get("unit", ""),
        "kind": sp.get("kind") or base.get("kind"),
        "diff_unit": sp.get("diff_unit") or base.get("diff_unit"),
        "method": "lookup",
        "survey": survey,
        "note": ALT_NOTES.get(survey, f"{survey} 공표값입니다"),
        "sources": [{k: v for k, v in a.items() if k != "survey"} for a in alts],
    }
    if any(a.get("den_item_id") for a in alts):
        formula = base.get("formula") or sp.get("formula") or ""
        out["formula"] = f"{formula} ({survey} 연령별 인구로 계산)".strip()
        out["note"] = (
            f"{survey} 연령별 인구로 계산했습니다 (비율은 공표되지 않음). "
            + ALT_NOTES[survey].split(". ", 1)[1]
        )
    return out


@lru_cache(maxsize=512)
def _survey_of(tbl_id):
    r = _rows("select survey_name from table_meta where tbl_id = ?", [tbl_id])
    return r[0]["survey_name"] if r and r[0]["survey_name"] else None


def source_surveys(sp):
    names = []
    for s in sp.get("sources") or []:
        try:
            name = _survey_of(s["table"])
        except Exception:
            name = None
        if name and name not in names:
            names.append(name)
    return names


def alt_surveys(sp):
    return list(dict.fromkeys(a["survey"] for a in sp.get("alt_sources") or []))


def _alias_map():
    return {name: ind.get("aliases", []) for name, ind in specs().items()}


def _exclusions():
    return {name: ind["not_in"] for name, ind in specs().items() if ind.get("not_in")}


def excluded_phrase(name, text):
    s = _norm(text)
    spans, gaps = _exclusion_spans(text)
    aliases = [
        (m.start(), m.end())
        for n in {_norm(x) for x in [name, *_alias_map().get(name, [])]}
        for m in re.finditer(re.escape(n), s)
    ]
    for ex in spans:
        if ex[2] != name:
            continue
        hit = [al for al in aliases if al[0] < ex[1] and ex[0] < al[1]]
        if not hit or any(not _separated(ex, al, gaps) for al in hit):
            return ex[3]
    return None


def _exclusion_spans(text):
    s = _norm(text)
    found = [
        (m.start(), m.end(), name, n, bool(re.search(r"\s", raw)))
        for name, phrases in _exclusions().items()
        for raw in phrases
        for n in [_norm(raw)]
        for m in re.finditer(re.escape(n), s)
    ]
    if not found:
        return [], set()
    gaps, k = set(), 0
    for i, ch in enumerate(str(text)):
        if ch.isspace():
            continue
        if i and str(text)[i - 1].isspace():
            gaps.add(k)
        k += 1
    try:
        gaps |= {k for a, b, _ in _region_matches(s) for k in (a, b)}
    except StatsTableMissing:
        pass
    return list(dict.fromkeys(found)), gaps


def _separated(ex, alias, gaps):
    if ex[4]:
        return False
    return any(ex[0] < g < ex[1] and not (alias[0] < g < alias[1]) for g in gaps)


def canonical_name(indicator):
    key = _norm(indicator)
    for name, names in _alias_map().items():
        if key in {_norm(n) for n in [name, *names]}:
            return name
    return None


def age_label(band):
    lo, hi = band
    if hi is None:
        return f"{lo}세 이상"
    if lo % 10 == 0 and hi == lo + 9:
        return f"{lo}대"
    if lo == hi:
        return f"{lo}세"
    return f"{lo}~{hi}세"


def _get_indicator(indicator, ages=None):
    name = canonical_name(indicator)
    if name == "총인구" and ages:
        lo, hi = ages[0]
        label = age_label(ages[0])
        upper = "" if hi is None else f" ~ {hi}세"
        formula = (
            f"주민등록인구 중 {lo}세 인구"
            if lo == hi
            else f"주민등록인구 중 {lo}세{upper or ' 이상'} 인구의 합"
        )
        return f"{label} 인구", {
            "unit": "명",
            "kind": "count",
            "formula": formula,
            "calc": lambda c: c["band"],
            "band": (lo, hi),
        }
    if name in INDICATORS:
        return name, INDICATORS[name]
    return None, None


def find_in_text(text):
    s = _norm(text)
    where = [i for i, ch in enumerate(str(text)) if not ch.isspace()]

    def one_word(start, end):
        return end - start > 2 or where[end - 1] - where[start] == end - start - 1

    spans = []
    for name, names in _alias_map().items():
        for n in {_norm(x) for x in [name, *names]}:
            spans += [
                (m.start(), m.end(), name)
                for m in re.finditer(re.escape(n), s)
                if one_word(m.start(), m.end())
            ]
    blocked, gaps = _exclusion_spans(text)
    spans = [
        a
        for a in spans
        if not any(
            b[2] == a[2] and b[0] < a[1] and a[0] < b[1] and not _separated(b, a, gaps)
            for b in blocked
        )
    ]
    return [
        a
        for a in spans
        if not any(b[0] <= a[0] and a[1] <= b[1] and b[1] - b[0] > a[1] - a[0] for b in spans)
    ]


def kosis_url(tbl_id):
    rows = _rows("select org_id from catalog where tbl_id = ?", [tbl_id]) or _rows(
        "select org_id from table_meta where tbl_id = ?", [tbl_id]
    )
    return KOSIS_URL.format(org_id=rows[0]["org_id"], tbl_id=tbl_id) if rows else None


def _base(name, ind, status="ok", **extra):
    out = {
        "status": status,
        "indicator": name,
        "kind": ind.get("kind"),
        "unit": ind["unit"],
        "formula": ind["formula"],
        "source": POP_SOURCE,
        "basis": POP_BASIS,
        "tbl_id": POP_TABLE,
        "url": kosis_url(POP_TABLE),
    }
    if ind.get("households"):
        out["extra_sources"] = [
            {
                "source": HH_SOURCE,
                "basis": HH_BASIS,
                "tbl_id": HH_TABLE,
                "url": kosis_url(HH_TABLE),
            }
        ]
    if ind.get("foreigners"):
        out["extra_sources"] = [
            {
                "source": FOREIGN_SOURCE,
                "basis": FOREIGN_BASIS,
                "tbl_id": FOREIGN_TABLE,
                "url": KOSIS_URL.format(org_id=FOREIGN_ORG, tbl_id=FOREIGN_TABLE),
            }
        ]
    if ind.get("area"):
        out["extra_sources"] = [
            {
                "source": AREA_SOURCE,
                "basis": AREA_BASIS,
                "tbl_id": AREA_TABLE,
                "url": KOSIS_URL.format(org_id=AREA_ORG, tbl_id=AREA_TABLE),
            }
        ]
    out.update(extra)
    if (note := (specs().get(name) or {}).get("note")) and status in ("ok", "out_of_range"):
        notes = list(out.get("notes") or [])
        if note not in notes:
            out["notes"] = [*notes, note]
    return out


def _display(reg):
    name = _base_name(reg["region_name"])
    if reg["kind"] in ("전국", "시도") or not reg.get("sido_name"):
        return name
    if reg["kind"] == "일반구":
        city = _rows(
            "select region_name from regions where kind = '시군구' and region_code = ?",
            [reg["region_code"][:4] + "0"],
        )
        if city:
            return f"{reg['sido_name']} {_base_name(city[0]['region_name'])} {name}"
    return f"{reg['sido_name']} {name}"


def _change_notes(region_code, y0=None, y1=None):
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
    try:
        return _rows(sql, params)
    except StatsTableMissing:
        return []


def _series_break_notes(y0, y1):
    rows = _catalog_rows(
        """
        select year, description from series_breaks
        where tbl_id = ? and year > ? and year <= ? order by year
    """,
        [POP_TABLE, y0, y1],
    )
    return [f"{r['year']}년: {r['description']}" for r in rows]


def _out_of_range_reason(lo, hi, ind=None):
    if (ind or {}).get("foreigners"):
        return f"등록외국인(법무부) 자료가 {lo}~{hi}년(연간)만 있어 이 기간만 계산할 수 있습니다."
    reason = f"수집된 주민등록인구 자료는 {lo}~{hi}년(연간)만 있습니다."
    if m := population_latest.components("00"):
        return reason + f" KOSIS 원표에는 {m['year']}년 {m['month']}월(월별)까지 자료가 있습니다."
    rows = _catalog_rows("select latest_available from table_notes where tbl_id = ?", [POP_TABLE])
    if rows and rows[0]["latest_available"]:
        reason += f" KOSIS 원표에는 {rows[0]['latest_available']}까지 자료가 있습니다."
    return reason


def _components(codes, y0, y1, sex, band=None, area=False, ind=None):
    marks = ", ".join("?" for _ in codes)
    lo, hi = band or (0, None)
    rows = _rows(
        f"""
        select year, region_code,
               sum(population) filter (where age is null)          as total,
               sum(population) filter (where age between 0 and 14) as young,
               sum(population) filter (where age between 15 and 64) as working,
               sum(population) filter (where age >= 65)            as old,
               sum(population) filter (where age >= ? and age <= coalesce(?, 999)) as band
        from population
        where sex = ? and year between ? and ? and region_code in ({marks})
        group by year, region_code
        order by region_code, year
    """,
        [lo, hi, sex, y0, y1, *codes],
    )
    out = {(r["year"], r["region_code"]): r for r in rows}
    if y1 > _data_years()[1]:
        latest = _month_values(list(codes), sex, y1)
        for code in codes:
            if m := latest.get(code):
                out[(y1, code)] = {
                    **{k: m[k] for k in ("total", "young", "working", "old")},
                    "year": y1,
                    "region_code": code,
                    "band": population_latest.band(m, lo, hi),
                }
    if area:
        _attach_area(out, codes, y1)
    ind = ind or {}
    if ind.get("sexes"):
        _attach_sexes(out, codes, y0, y1)
    if ind.get("median"):
        _attach_median(out, codes, y0, y1, sex)
    if ind.get("households"):
        _attach_households(out, codes, y0, y1)
    if ind.get("foreigners"):
        _attach_foreigners(out, codes, y0, y1, sex)
    return out


def _median_age(ages):
    counts = sorted((a, n) for a, n in ages.items() if a is not None and n)
    total = sum(n for _, n in counts)
    if not total:
        return None
    half, cum = total / 2, 0
    for age, n in counts:
        if cum + n >= half:
            return age + (half - cum) / n
        cum += n
    return None


def _attach_sexes(comps, codes, y0, y1):
    marks = ", ".join("?" for _ in codes)
    rows = _rows(
        f"""
        select year, region_code, sex, sum(population) as total from population
        where age is null and sex in ('남', '여') and year between ? and ?
              and region_code in ({marks})
        group by year, region_code, sex
    """,
        [y0, y1, *codes],
    )
    got = {(r["year"], r["region_code"], r["sex"]): r["total"] for r in rows}
    if y1 > _data_years()[1]:
        for s in ("남", "여"):
            for code, m in _month_values(list(codes), s, y1).items():
                got[(y1, code, s)] = m["total"]
    for (year, code), comp in comps.items():
        comp["male"] = got.get((year, code, "남"))
        comp["female"] = got.get((year, code, "여"))


def _attach_median(comps, codes, y0, y1, sex):
    marks = ", ".join("?" for _ in codes)
    rows = _rows(
        f"""
        select year, region_code, age, sum(population) as n from population
        where age is not null and sex = ? and year between ? and ? and region_code in ({marks})
        group by year, region_code, age
    """,
        [sex, y0, y1, *codes],
    )
    ages = {}
    for r in rows:
        ages.setdefault((r["year"], r["region_code"]), {})[r["age"]] = r["n"]
    if y1 > _data_years()[1]:
        for code, m in _month_values(list(codes), sex, y1).items():
            if m.get("ages"):
                ages[(y1, code)] = m["ages"]
    for key, comp in comps.items():
        comp["median_age"] = _median_age(ages.get(key) or {})


def _attach_households(comps, codes, y0, y1):
    from app.services import lookup

    try:
        got = lookup._fetch({"table": HH_TABLE, "item_id": "T1"}, list(codes), y0, y1)
    except StatsTableMissing:
        return
    for key, comp in comps.items():
        comp["households"] = got.get(key)


def _attach_foreigners(comps, codes, y0, y1, sex):
    from app.services import lookup

    src = {
        "table": FOREIGN_TABLE,
        "item_id": "H001",
        "filters": {"c2_id": FOREIGN_SEX.get(sex, "0"), "c3_id": "B001"},
    }
    try:
        got = lookup._fetch(src, list(codes), y0, y1)
    except StatsTableMissing:
        return
    for key, comp in comps.items():
        comp["foreigners"] = got.get(key)


def _years_of(ind):
    lo, hi = _data_years()
    if ind.get("foreigners"):
        from app.services import lookup

        try:
            flo, fhi = lookup._years({"table": FOREIGN_TABLE, "item_id": "H001"})
        except StatsTableMissing:
            return lo, hi, False
        return max(lo, flo), min(hi, fhi), False
    return lo, hi, True


def _attach_area(comps, codes, y1):
    marks = ", ".join("?" for _ in codes)
    rows = _rows(
        "select year, region_code, area_km2 from area"
        f" where year <= ? and region_code in ({marks})",
        [y1, *codes],
    )
    by_code = {}
    for r in rows:
        by_code.setdefault(r["region_code"], {})[r["year"]] = r["area_km2"]
    for (year, code), comp in comps.items():
        years = [y for y in by_code.get(code, {}) if y <= year]
        if years:
            comp["area"] = by_code[code][max(years)]
            comp["area_year"] = max(years)


def _area_missing(name, ind):
    if not ind.get("area"):
        return None
    try:
        if _rows("select 1 from area limit 1"):
            return None
    except StatsTableMissing:
        pass
    return _base(
        name,
        ind,
        "missing_data",
        reason="인구밀도 계산에 쓰는 행정구역 면적 자료가 아직 적재되지 않았습니다.",
    )


def _area_note(comps, year):
    for (y, _), c in comps.items():
        if y == year and c.get("area_year") and c["area_year"] != year:
            return f"{year}년 면적 자료가 아직 없어 {c['area_year']}년 말 면적으로 계산했습니다"
    return None


def _month_values(codes, sex, year):
    latest = population_latest.components_many(codes, sex)
    out = {c: m for c, m in latest.items() if m["year"] == year}
    later = [c for c, m in latest.items() if m["year"] > year]
    if later:
        december = population_latest.components_many(later, sex, f"{year}12")
        out |= {c: m for c, m in december.items() if m["year"] == year}
    return out


def _monthly(code, sex, year):
    if year <= _data_years()[1]:
        return None
    return _month_values([code], sex, year).get(code)


def latest_year(region_names, sex="계"):
    hi = _data_years()[1]
    code = "00"
    if region_names:
        reg = resolve_region(region_names[0], None, sigungu=True)
        if reg.get("status") != "ok":
            return hi
        code = reg["region_code"]
    m = population_latest.components(code, sex)
    return m["year"] if m and m["year"] > hi else hi


def _mark_monthly(out, code, sex, year):
    m = _monthly(code, sex, year)
    if out.get("status") != "ok" or not m:
        return out
    hi = _data_years()[1]
    out["as_of"] = f"{m['year']}년 {m['month']}월 말"
    out["basis"] = f"주민등록인구, {out['as_of']} 기준 (KOSIS 월별 자료)"
    which = "12월" if m["month"] == 12 else "최신 달"
    out.setdefault("notes", []).append(
        f"{hi}년까지는 수집한 연간(12월 말) 자료이고, {year}년은 KOSIS 월별 자료의 {which}"
        f"({out['as_of']}) 값입니다"
    )
    return out


FOUNDED = {"세종특별자치시": 2012, "계룡시": 2003, "증평군": 2003}


def founded_year(reg) -> int | None:
    return FOUNDED.get(reg.get("region_name"))


def not_yet_founded(reg, year, indicator) -> dict | None:
    born = founded_year(reg)
    if born is None or year is None or year >= born:
        return None
    where = _display(reg)
    return {
        "status": "region_not_in_year",
        "indicator": indicator,
        "region": where,
        "reason": f"{josa(where, '은는')} {born}년에 생긴 지역이라 {year}년 자료가 없습니다.",
        "guide": f"{born}년부터의 연도로 다시 질문해 주세요.",
        "available_from": born,
        "final": True,
    }


def _not_yet(name, ind, reg, y1):
    born = founded_year(reg)
    if born is None or y1 >= born:
        return None
    return _base(
        name,
        ind,
        "region_not_in_year",
        region=_display(reg),
        reason=f"{josa(_display(reg), '은는')} {born}년에 생긴 지역이라 {y1}년 자료가 없습니다.",
        guide=f"{born}년부터의 연도로 다시 질문해 주세요.",
        available_from=born,
        final=True,
        notes=_change_notes(reg["region_code"]),
    )


def _check(name, ind, reg, y0, y1):
    if (out := _not_yet(name, ind, reg, y1)) is not None:
        return out
    lo, hi, month_ok = _years_of(ind)
    monthly = month_ok and y1 > hi and y0 >= lo and _monthly(reg["region_code"], "계", y1)
    if not monthly and (y0 < lo or y1 > hi):
        out = _base(
            name,
            ind,
            "out_of_range",
            region=_display(reg),
            reason=_out_of_range_reason(lo, hi, ind),
            available_years=[lo, hi],
        )
        if y1 > hi and reg["kind"] in ("전국", "시도") and "projection_c3" in ind:
            proj = projection(name, reg["region_code"], y1)
            if proj["status"] == "ok":
                out["suggestion"] = proj
            elif proj.get("available_years"):
                out["reason"] += " " + proj["reason"]
                out["available_years"] = proj["available_years"]
        elif y0 > hi and reg.get("sido_code") and "projection_c3" in ind:
            proj = projection(name, reg["sido_code"], y1)
            if proj["status"] == "ok":
                out.update(
                    status="prediction_question",
                    reason=f"{_display(reg)}의 {y1}년 값은 아직 없는 미래 값이고, "
                    "장래인구추계는 시도 단위까지만 있어 답할 수 없습니다.",
                    suggestion=proj,
                    sub_future=True,
                )
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
            reason=f"{josa(_display(reg), '은는')} {period} 있는 지역입니다.",
            notes=_change_notes(reg["region_code"]),
        )
    return None


def _prepare(indicator, region_names, year=None, ages=None):
    name, ind = _get_indicator(indicator, ages)
    if name is None:
        return (
            None,
            None,
            None,
            {"status": "unknown_indicator", "indicator": indicator, "supported": list(specs())},
        )
    if missing := _area_missing(name, ind):
        return name, ind, None, missing
    regions = []
    for rn in region_names:
        reg = resolve_region(rn, year, sigungu=True)
        if reg["status"] == "ambiguous_region" and _all_sido(r for r in region_names if r != rn):
            reg = resolve_region(rn, year)
        if reg["status"] != "ok":
            return name, ind, None, {**reg, "indicator": name}
        regions.append(in_year(reg, year))
    return name, ind, regions, None


def _all_sido(names):
    kinds = [resolve_region(n) for n in names]
    return bool(kinds) and all(k["status"] == "ok" and k["kind"] == "시도" for k in kinds)


def in_year(reg, year):
    vf, vt = reg.get("valid_from"), reg.get("valid_to")
    if year is None or ((vf is None or year >= vf) and (vt is None or year <= vt)):
        return reg
    found = _rows(
        """
        select region_code, region_name, kind, sido_code, sido_name, valid_from, valid_to
        from regions where region_name = ? and region_code != ?
          and (valid_from is null or valid_from <= ?) and (valid_to is null or valid_to >= ?)
    """,
        [reg["region_name"], reg["region_code"], year, year],
    )
    if len(found) > 1:
        linked = set()
        for ch in _rows("select region_codes from region_changes"):
            codes = ch["region_codes"].split(",")
            if reg["region_code"] in codes:
                linked.update(codes)
        found = [f for f in found if f["region_code"] in linked]
    if len(found) != 1:
        return reg
    old = {"status": "ok", **found[0], "display": _display(found[0])}
    changes = "; ".join(_change_notes(reg["region_code"]))
    old["note"] = f"{year}년 당시에는 {old['display']}이므로 그 값으로 답합니다" + (
        f" ({changes})" if changes else ""
    )
    return old


@lru_cache
def _region_names():
    names = {r["alias"] for r in _rows("select alias from region_aliases")}
    names |= {r["region_name"] for r in _rows("select region_name from regions")}
    names |= set(AMBIGUOUS_NAMES)
    names -= {"합계", "총계", "전국", "계"}
    return sorted((_norm(n) for n in names if len(_norm(n)) >= 2), key=len, reverse=True)


def _region_matches(text):
    taken, found = [], []
    for name in _region_names():
        for m in re.finditer(re.escape(name), text):
            if m.start() and text[m.start() - 1].isdigit():
                continue
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append((m.start(), m.end(), name))
    return sorted(found)


def find_regions(text):
    return [name for _, _, name in _region_matches(_norm(text))]


def region_mentions(text: str) -> list[tuple[int, int, str]]:
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    return [
        (positions[start], positions[end - 1] + 1, name)
        for start, end, name in _region_matches(_norm(text))
    ]


NATIONAL_WORDS = re.compile(
    r"(?<![가-힣])(?:전국|우리\s*나라|대한민국|한국\s*전체"
    r"|국내(?!\s*(?:이동|인구\s*이동))(?!(?:에서)?\s*(?:태어|출생(?!\s*아))))"
    r"(?:의|에서)?(?![가-힣])"
)


def without_national(term):
    rest = " ".join(NATIONAL_WORDS.sub(" ", str(term)).split())
    return rest or str(term)


def _base_name(name):
    return re.sub(r"\((?:계|통합)\)$", "", name or "")


def _change_groups():
    return [
        set(r["region_codes"].split(",")) for r in _rows("select region_codes from region_changes")
    ]


def _same_place(a, b):
    if _base_name(a["region_name"]) != _base_name(b["region_name"]):
        return False
    if a["region_code"] == b["region_code"]:
        return True
    if a.get("sido_code") and a.get("sido_code") == b.get("sido_code"):
        return True
    pair = {a["region_code"], b["region_code"]}
    return any(pair <= g for g in _change_groups())


def resolve_region(name, year=None, sigungu=False):
    if _norm(name) in AMBIGUOUS_NAMES:
        return {
            "status": "ambiguous_region",
            "region": name,
            "reason": josa(f"'{name}'", "은는") + " 여러 시도를 가리킵니다. "
            "어느 지역인지 확인이 필요합니다.",
            "candidates": AMBIGUOUS_NAMES[_norm(name)],
        }
    sql = """
        select distinct r.region_code, r.region_name, r.kind, r.sido_code, r.sido_name,
                        r.valid_from, r.valid_to
        from regions r left join region_aliases a using (region_code)
        where (? in (replace(a.alias, ' ', ''),
                     case when r.region_name like '%(통합)' or r.region_name like '%(계)'
                          then replace(replace(replace(r.region_name, '(통합)', ''), '(계)', ''),
                                       ' ', '') end)
               or r.region_code = ?) {extra}
        order by r.region_code
    """
    key = _norm(name)
    found = _rows(sql.format(extra=""), [key, key])
    for suffix in ("시", "군", "구"):
        if not found and not key.endswith(("시", "군", "구", "도")):
            found = _rows(sql.format(extra=""), [key + suffix, key + suffix])
    if not found and key.endswith("도") and len(key) >= 2:
        for base in dict.fromkeys((key[:-1], key)):
            for suffix in ("군", "시"):
                if not found and len(base) >= 1:
                    found = _rows(sql.format(extra=""), [base + suffix, base + suffix])

    parts = str(name).split()
    if not found and len(parts) >= 2:
        sido = resolve_region(parts[0])
        if sido["status"] == "ok" and sido["kind"] == "시도":
            rest = _norm("".join(parts[1:]))
            found = _rows(
                sql.format(extra="and r.sido_code = ?"), [rest, rest, sido["region_code"]]
            )
            for suffix in ("시", "군", "구"):
                if not found and not rest.endswith(("시", "군", "구")):
                    found = _rows(
                        sql.format(extra="and r.sido_code = ?"),
                        [rest + suffix, rest + suffix, sido["region_code"]],
                    )

    if not found and len(parts) >= 2:
        city = resolve_region(" ".join(parts[:-1]), year=year)
        if city["status"] == "ok" and city["kind"] == "시군구":
            gu = _norm(parts[-1])
            in_city = "and r.kind = '일반구' and substr(r.region_code, 1, 4) = ?"
            prefix = city["region_code"][:4]
            found = _rows(sql.format(extra=in_city), [gu, gu, prefix])
            if not found and not gu.endswith("구"):
                found = _rows(sql.format(extra=in_city), [gu + "구", gu + "구", prefix])

    if sigungu and len(found) == 1 and found[0]["kind"] == "시도":
        short = _norm(name)
        same = _rows(
            """
            select region_code, region_name, kind, sido_code, sido_name, valid_from, valid_to
            from regions where kind = '시군구' and region_name in (?, ?)
            order by region_code
        """,
            [short + "시", short + "군"],
        )
        if same and found[0]["region_name"] != short:
            if all(s["sido_code"] == found[0]["region_code"] for s in same):
                city = same[0]["region_name"]
                found = [
                    {
                        **found[0],
                        "note": f"지역 '{name}' → {found[0]['region_name']} 전체로 답했습니다 "
                        f"({city}만 보시려면 '{city}'로 질문해 주세요)",
                    }
                ]
            else:
                found = found + same

    if len(found) > 1 and year is not None:
        alive = [
            r
            for r in found
            if (r["valid_from"] is None or year >= r["valid_from"])
            and (r["valid_to"] is None or year <= r["valid_to"])
        ]
        found = [r for r in found if r in alive or not any(_same_place(r, a) for a in alive)]

    if len(found) > 1 and year is None and all(_same_place(r, found[0]) for r in found):
        found = [r for r in found if r["valid_to"] is None] or found

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


def _region_notes(regs):
    return [r["note"] for r in regs if r.get("note")]


def _optional(note):
    return [note] if note else []


def value(indicator, region, year, sex="계", ages=None):
    name, ind, regs, err = _prepare(indicator, [region], year, ages)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, year, year):
        return bad
    comps = _components(
        [reg["region_code"]], year, year, sex, ind.get("band"), ind.get("area"), ind
    )
    comp = comps.get((year, reg["region_code"]))
    v = ind["calc"](comp) if comp else None
    out = _base(
        name,
        ind,
        region=reg["display"],
        year=year,
        sex=sex,
        value=_r(v, ind),
        notes=_region_notes(regs) + _optional(_area_note(comps, year)),
    )
    if ind.get("area") and comp and comp.get("area"):
        out["area_km2"] = round(comp["area"], 2)
        out["population"] = int(comp["total"])
    if ind.get("operands") and comp and v is not None:
        out["operands"] = [
            {"label": label, "value": _operand(comp.get(key)), "unit": unit}
            for key, label, unit in ind["operands"]
        ]
    return _mark_monthly(out, reg["region_code"], sex, year)


def trend(indicator, region, start_year, end_year, sex="계", ages=None):
    name, ind, regs, err = _prepare(indicator, [region], end_year, ages)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, start_year, end_year):
        return bad
    comps = _components(
        [reg["region_code"]], start_year, end_year, sex, ind.get("band"), ind.get("area"), ind
    )
    raw = [(y, ind["calc"](c)) for (y, _), c in sorted(comps.items())]
    raw = [p for p in raw if p[1] is not None] or raw
    series = [{"year": y, "value": _r(v, ind)} for y, v in raw]
    first, last = raw[0][1], raw[-1][1]
    change = {
        "from_year": series[0]["year"],
        "to_year": series[-1]["year"],
        "diff": _round(_diff(first, last, ind["kind"]), ind["kind"])
        if ind["kind"] == "count"
        else round(_diff(first, last, ind["kind"]), ind.get("digits", 2)),
        "diff_unit": _diff_unit(ind),
    }
    if ind["kind"] == "count" or ind.get("rate"):
        change["rate"] = round(analysis.growth_rate(first, last), 2)
        change["rate_unit"] = "%"
    elif ind["kind"] == "ratio" and first:
        change["relative_rate"] = round(analysis.growth_rate(first, last), 1)
    years = series[-1]["year"] - series[0]["year"]
    if "rate" in change and years and first and last and first > 0 and last > 0:
        change["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
    out = _base(
        name,
        ind,
        region=reg["display"],
        sex=sex,
        series=series,
        change=change,
        notes=_change_notes(reg["region_code"], start_year, end_year)
        + _series_break_notes(start_year, end_year)
        + _optional(_area_note(comps, end_year)),
    )
    if (m := _monthly(reg["region_code"], sex, end_year)) and series[-1]["year"] == end_year:
        hi = _data_years()[1]
        out["notes"].append(
            f"{end_year}년 값은 {m['year']}년 {m['month']}월 말 기준입니다 "
            f"(KOSIS 월별 자료의 최신 달, {hi}년까지는 12월 말)"
        )
    return out


def compare(indicator, regions, year, sex="계", ages=None):
    name, ind, regs, err = _prepare(indicator, regions, year, ages)
    if err:
        return err
    for reg in regs:
        if bad := _check(name, ind, reg, year, year):
            return bad
    comps = _components(
        [r["region_code"] for r in regs], year, year, sex, ind.get("band"), ind.get("area"), ind
    )
    values, raw = [], {}
    for reg in regs:
        comp = comps.get((year, reg["region_code"]))
        raw[reg["display"]] = ind["calc"](comp) if comp else None
        values.append({"region": reg["display"], "value": _r(raw[reg["display"]], ind)})
    values.sort(key=lambda x: (raw[x["region"]] is None, -(raw[x["region"]] or 0)))
    valid = [x for x in values if x["value"] is not None]
    gap = (
        _diff(raw[valid[-1]["region"]], raw[valid[0]["region"]], ind["kind"])
        if len(valid) >= 2
        else None
    )
    out = _base(
        name,
        ind,
        year=year,
        sex=sex,
        values=values,
        highest=valid[0]["region"] if valid else None,
        lowest=valid[-1]["region"] if valid else None,
        gap=_round(gap, ind["kind"])
        if ind["kind"] == "count"
        else (round(gap, ind.get("digits", 2)) if gap is not None else None),
        gap_unit=_diff_unit(ind),
        notes=_region_notes(regs) + _optional(_area_note(comps, year)),
    )
    return _mark_monthly(out, regs[0]["region_code"], sex, year)


AGE_GROUPS = [
    ("young", "유소년인구(0~14세)"),
    ("working", "생산연령인구(15~64세)"),
    ("old", "고령인구(65세 이상)"),
]
COMPOSITION = {
    "unit": "%",
    "formula": "항목별 인구 ÷ 총인구 × 100",
}


def composition(region, year, by="age", sex="계"):
    name, ind, regs, err = _prepare("총인구", [region], year)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, year, year):
        return bad
    code = reg["region_code"]
    if by == "sex":
        sex = "계"
        counts = {
            f"{s}자": (_components([code], year, year, s).get((year, code)) or {}).get("total")
            for s in ("남", "여")
        }
    else:
        comp = _components([code], year, year, sex).get((year, code)) or {}
        counts = {label: comp.get(key) for key, label in AGE_GROUPS}
    total = (_components([code], year, year, sex).get((year, code)) or {}).get("total")
    if not total or None in counts.values():
        return _base(
            "인구 구성",
            COMPOSITION,
            "missing_data",
            region=reg["display"],
            reason=f"{reg['display']} {year}년 인구 자료가 없습니다.",
        )
    shares = analysis.composition(counts, total)
    out = _base(
        "인구 구성",
        COMPOSITION,
        region=reg["display"],
        year=year,
        sex=sex,
        by=by,
        total=int(total),
        parts=[
            {"label": label, "count": int(n), "share": round(shares[label], 2)}
            for label, n in counts.items()
        ],
    )
    return _mark_monthly(out, code, sex, year)


def rank_where(level, sido=None):
    if level == "시도":
        where, params, scope = "r.kind = '시도'", [], "시도"
    elif level == "시":
        where, params, scope = "r.is_basic and r.region_name like '%시'", [], "시"
    else:
        where, params, scope = "r.is_basic", [], "시군구"
    if sido:
        s = resolve_region(sido)
        if s["status"] != "ok":
            return s
        where += " and r.sido_code = ?"
        params.append(s["region_code"])
        scope = f"{s['display']} {scope}"
    return where, params, scope


def rank(
    indicator,
    year,
    level="시도",
    sido=None,
    top=None,
    ascending=False,
    sex="계",
    ages=None,
    target=None,
):
    name, ind = _get_indicator(indicator, ages)
    if name is None:
        return {
            "status": "unknown_indicator",
            "indicator": indicator,
            "supported": list(specs()),
        }
    if missing := _area_missing(name, ind):
        return missing
    lo, hi, month_ok = _years_of(ind)
    monthly = month_ok and year > hi and _monthly("00", "계", year)
    if not lo <= year <= hi and not monthly:
        return _base(
            name,
            ind,
            "out_of_range",
            reason=_out_of_range_reason(lo, hi, ind),
            available_years=[lo, hi],
        )

    cond = rank_where(level, sido)
    if isinstance(cond, dict):
        return {**cond, "indicator": name}
    where, params, _ = cond
    if monthly:
        regs = _rows(
            f"""
            select r.region_code, r.region_name, r.kind, r.sido_name
            from regions r
            where {where} and (r.valid_from is null or r.valid_from <= ?)
              and (r.valid_to is null or r.valid_to >= ?)
            order by r.region_code
        """,
            [*params, year, year],
        )
    else:
        regs = _rows(
            f"""
            select distinct r.region_code, r.region_name, r.kind, r.sido_name
            from regions r join population p on p.region_code = r.region_code and p.year = ?
            where {where}
            order by r.region_code
        """,
            [year, *params],
        )
    comps = _components(
        [r["region_code"] for r in regs], year, year, sex, ind.get("band"), ind.get("area"), ind
    )
    items, missing = [], []
    for reg in regs:
        comp = comps.get((year, reg["region_code"]))
        v = ind["calc"](comp) if comp else None
        if v is not None:
            items.append({"region": _display(reg), "value": _r(v, ind)})
        else:
            missing.append(_display(reg))
    items = analysis.rank_rows(items, descending=not ascending)
    notes = _optional(_area_note(comps, year))
    if missing:
        shown = ", ".join(missing[:5]) + (f" 외 {len(missing) - 5}곳" if len(missing) > 5 else "")
        notes.append(f"자료를 받지 못한 {len(missing)}곳은 순위에서 빠졌습니다 ({shown})")
    out = _base(
        name,
        ind,
        year=year,
        sex=sex,
        level=level,
        order="낮은 순" if ascending else "높은 순",
        total_regions=len(items),
        ranking=items[:top] if top else items,
        notes=notes,
    )
    if target:
        t = resolve_region(target, year)
        if t["status"] == "ok":
            hit = next((it for it in items if it["region"] == t["display"]), None)
            out["target"] = {
                "region": t["display"],
                "rank": hit["rank"] if hit else None,
                "value": hit["value"] if hit else None,
            }
    return _mark_monthly(out, "00", "계", year) if monthly else out


def projection(indicator, region, year, scenario=DEFAULT_SCENARIO):
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
        span = _rows(
            "select min(year) as lo, max(year) as hi from facts where tbl_id = ? and scenario = ?",
            [PROJ_TABLE, scenario],
        )[0]
        return {
            "status": "out_of_range",
            "indicator": name,
            "region": reg["display"],
            "reason": f"장래인구추계도 {span['hi']}년까지만 있어 {year}년 값은 없습니다.",
            "available_years": [span["lo"], span["hi"]],
        }
    r = rows[0]
    notes = [
        "주민등록인구와 기준(시점, 외국인 포함 여부)이 달라 실적과 직접 이어서 비교하지 않습니다.",
    ]
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
    if reg.get("valid_to") is not None and year > reg["valid_to"]:
        notes.append(
            f"{josa(reg['display'], '은는')} {reg['valid_to']}년 이후 행정구역이 바뀌었습니다. "
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
        "tbl_id": PROJ_TABLE,
        "url": kosis_url(PROJ_TABLE),
        "notes": notes,
    }


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
