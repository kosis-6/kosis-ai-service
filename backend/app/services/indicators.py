"""지표 계산 (하위 태스크 4: 수치 분석)

LLM이 숫자를 직접 계산하지 않도록, 질문 유형별 계산을 함수로 제공한다.
모든 함수는 JSON으로 바꿀 수 있는 dict를 돌려준다.

  value(지표, 지역, 연도)            단순 조회   "2025년 서울 고령인구 비율은?"
  trend(지표, 지역, 시작, 끝)        추이 분석   "최근 5년 부산 고령인구 비율 변화는?" (증감률 포함)
  compare(지표, [지역...], 연도)     지역 비교   "서울과 부산 중 어디가 높아?"
  rank(지표, 연도, 수준)             순위        "고령인구 비율이 가장 높은 시도는?"
  composition(지역, 연도, 기준)      구성비      "2025년 서울 인구의 연령대별 구성비는?"
  projection(지표, 지역, 연도)       장래추계    수록 기간 이후 연도 안내용

결과의 status
  ok                  계산 성공
  unknown_indicator   지원하지 않는 지표
  unknown_region      모르는 지역
  ambiguous_region    같은 이름의 지역이 여럿 (예: 중구) → candidates로 되묻기
  out_of_range        수록 기간 밖 연도 → 추계가 있으면 suggestion에 담음 (답변 불가 사례 1)
  region_not_in_year  그 연도에 없던 지역 (예: 2025년 전남광주통합특별시)
  missing_data        수록 기간 안이지만 그 지역·연도 값이 비어 있음

실행(예시 질문 데모): python -m app.services.indicators
"""

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
AREA_ORG = "116"  # 카탈로그(catalog·table_meta)에 없는 표라 기관 ID를 적어 둔다
AREA_BASIS = "지목 전체(계) 면적, 각 연도 12월 31일 기준"
HH_TABLE = "DT_1B040B3"
HH_SOURCE = "행정안전부, 행정구역(시군구)별 주민등록세대수 (DT_1B040B3)"
HH_BASIS = "주민등록 세대수, 각 연도 12월 31일 기준"
# 등록외국인 비율의 분자 (법무부). 성별 칸은 c2, 국적
# 계는 c3 B001 (지표 사전의 등록외국인 수와 같은 칸)
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

# kind에 따라 차이·변화의 단위가 달라진다
#   count : 사람 수   → 차이는 명, 변화는 증감률(%)
#   ratio : 비율(%)   → 차이·변화는 %p
#   index : 지수      → 차이·변화는 포인트
#   value : 그 밖의 값 → 차이는 지표의 diff_unit, 변화는 증감률(%)이 있으면 rate=True
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
        # 답변 문장에서 값의 뜻을 풀어 쓸 때 사용 (qa.summary)
        "meaning": "유소년인구(0~14세) 100명당 65세 이상 인구가 {value}명",
    },
    "생산연령인구 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "15~64세 인구 ÷ 총인구 × 100",
        "calc": lambda c: _div(c["working"], c["total"]),
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
        "sexes": True,  # _components가 남녀 인구(male, female)를 붙인다
        "meaning": "여자 100명당 남자가 {value}명",
    },
    "중위연령": {
        "unit": "세",
        "kind": "value",
        "diff_unit": "세",
        "digits": 1,
        "formula": "인구를 나이순으로 세웠을 때 가운데 사람의 나이 (1세별 인구로 보간)",
        "calc": lambda c: c.get("median_age"),
        "median": True,  # _components가 1세별 인구로 중위연령(median_age)을 붙인다
        "meaning": "인구의 절반이 {value}세 미만",
    },
    "세대당 인구": {
        "unit": "명",
        "kind": "value",
        "diff_unit": "명",
        "formula": "주민등록 총인구 ÷ 주민등록 세대수",
        "rate": True,  # 추이에 증감률도 (세대당 인구는 명 단위 값이라 증감률을 함께 본다)
        "calc": lambda c: c["total"] / c["households"] if c.get("households") else None,
        "households": True,  # _components가 세대수(households, DT_1B040B3)를 붙인다
        "meaning": "한 세대에 평균 {value}명",
        # 값 답에 보여 줄 분자 · 분모 (구성 요소 키, 이름, 단위). qa._body가 "계산: A ÷ B" 줄로
        "operands": [("total", "주민등록인구", "명"), ("households", "주민등록 세대수", "세대")],
    },
    "등록외국인 비율": {
        "unit": "%",
        "kind": "ratio",
        "formula": "등록외국인 수 ÷ 주민등록 총인구 × 100",
        "calc": lambda c: _div(c.get("foreigners"), c["total"]),
        # _components가 그해 등록외국인 수(foreigners, DT_1B040A9C)를 붙인다. 등록외국인 표에 있는
        # 연도만 답한다 (_years_of). 분모 주민등록인구에는 외국인이 들어 있지 않다
        "foreigners": True,
        "operands": [("foreigners", "등록외국인", "명"), ("total", "주민등록인구", "명")],
    },
    "인구밀도": {
        "unit": "명/㎢",
        "kind": "value",
        "diff_unit": "명/㎢",
        "digits": 1,
        "rate": True,  # 면적이 거의 그대로라 증감률은 인구 증감률과 비슷하다
        "formula": "주민등록 총인구 ÷ 행정구역 면적(㎢)",
        "calc": lambda c: c["total"] / c["area"] if c.get("area") else None,
        "area": True,  # _components가 면적(mart.area)도 붙인다
        "meaning": "1㎢(가로세로 1km 넓이)마다 평균 {value}명",
        "operands": [("total", "주민등록인구", "명"), ("area", "면적", "㎢")],
    },
}
# 지표의 다른 이름(별칭)은 config/catalog_notes.toml의 지표 사전에서 읽는다 (_get_indicator)
DIFF_UNIT = {"count": "명", "ratio": "%p", "index": "포인트"}
SEXES = ("계", "남", "여")
NOTES_PATH = Path(__file__).resolve().parents[2] / "config" / "catalog_notes.toml"
# 한 이름이 여러 시도를 가리키는 표현 (되묻는다)
AMBIGUOUS_NAMES = {
    "충청도": ["충청북도", "충청남도"],
    "충청": ["충청북도", "충청남도"],
    "경상도": ["경상북도", "경상남도"],
    "전라도": ["전북특별자치도", "전라남도"],
}


# ---- 내부 도우미 ----------------------------------------------------------------


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def _div(a, b):
    return analysis.share(a, b) if a is not None and b else None


def _diff(base, current, kind):
    """차이: 비율 지표는 %p (analysis.point_change), 그 밖은 증감량 (analysis.change)"""
    return (analysis.point_change if kind == "ratio" else analysis.change)(base, current)


def _diff_unit(ind):
    """차이의 단위: 지표에 적은 diff_unit, 없으면 kind로 (명 / %p / 포인트)"""
    return ind.get("diff_unit") or DIFF_UNIT[ind["kind"]]


def _round(v, kind, digits=2):
    if v is None:
        return None
    return int(v) if kind == "count" else round(v, digits)


def _operand(v):
    """분자 · 분모 표시값: 사람 · 세대 수는 정수, 면적은 소수 둘째 자리"""
    if v is None:
        return None
    return int(v) if float(v).is_integer() else round(v, 2)


def _r(v, ind):
    """지표 정의에 맞춘 반올림 (사람 수는 정수, 인구밀도는 소수 첫째 자리, 그 밖은 둘째 자리)"""
    return _round(v, ind["kind"], ind.get("digits", 2))


def _data_years():
    r = _rows("select min(year) as lo, max(year) as hi from population")[0]
    return r["lo"], r["hi"]


def spec_tables():
    """지표 사전이 쓰는 통계표 ID (서버 시작 시 적재 안 된 표를 적재 대기열에 넣을 때 씀)
    years가 있는 과거 연도용 표는 빼고, 질문이 그 연도를 물을 때 받는다 (table_cache.py)"""
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
    """지표 사전 (config/catalog_notes.toml의 [[indicators]]) → {지표 이름: 정의}
    서버가 DB를 다시 만들지 않아도 지표·별칭 추가가 바로 반영되도록 파일에서 직접 읽는다"""
    with open(NOTES_PATH, "rb") as f:
        return {ind["name"]: ind for ind in tomllib.load(f)["indicators"]}


def spec(indicator):
    """지표 이름·별칭 → 지표 사전의 정의. 모르면 None"""
    name = canonical_name(indicator or "")
    return specs().get(name) if name else None


# 조사마다 답에 붙이는 기준 (다른 조사의 공표값으로 답할 때)
# 사용자가 무엇이 다른지 알 수 있게 쓴다: 누구를(외국인) · 어디서(주소지 · 사는 곳) · 언제 셌는지
ALT_NOTES = {
    "인구총조사": "인구총조사 기준입니다. 실제로 사는 곳에서 외국인까지 센 인구(11월 1일)라, "
    "주소를 둔 곳에서 내국인만 센 주민등록인구와 값이 다릅니다",
    "장래인구추계": "장래인구추계(중위 추계) 기준입니다. 인구총조사를 바탕으로 7월 1일 인구를 "
    "추정한 값(외국인 포함)이라, 주소를 둔 곳에서 내국인만 센 주민등록인구와 값이 다릅니다",
}


def alt_spec(sp, survey):
    """지표의 다른 조사 공표값 정의 (지표 사전의 alt_sources) → 조회 지표 정의. 없으면 None
    예) "인구총조사 기준 영도구 중위연령": 주민등록 1세별 계산 대신 DT_1IN2030의 중위연령"""
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
        # 비율이 공표되지 않아 그 조사의 연령별 인구로 나눴다 (인구총조사 고령인구 비율 등)
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
    """지표가 평소에 쓰는 표의 조사 이름들 (예: 합계출산율 → ["인구동향조사"])"""
    names = []
    for s in sp.get("sources") or []:
        try:
            name = _survey_of(s["table"])
        except Exception:  # 표 목록을 읽지 못해도 답은 한다 (안내만 빠진다)
            name = None
        if name and name not in names:
            names.append(name)
    return names


def alt_surveys(sp):
    """지표에 공표값이 따로 있는 조사 이름들"""
    return list(dict.fromkeys(a["survey"] for a in sp.get("alt_sources") or []))


def _alias_map():
    """지표 이름 → 별칭 목록 (계산·조회 지표 모두)"""
    return {name: ind.get("aliases", []) for name, ind in specs().items()}


def _exclusions():
    """지표 이름 → 제외 표현 목록 (지표 사전의 not_in). 이 표현 안에 든 별칭은 그 지표가 아니다
    예) "등록외국인" 안의 "외국인 수"는 외국인주민 수가 아니다 (귀화자·자녀를 포함하는 다른 통계)"""
    return {name: ind["not_in"] for name, ind in specs().items() if ind.get("not_in")}


def excluded_phrase(name, text):
    """문장에 지표의 제외 표현이 있으면 그 표현 (공백을 뺀 형태), 없으면 None"""
    s = _norm(text)
    return next((n for n in map(_norm, _exclusions().get(name, [])) if n in s), None)


def canonical_name(indicator):
    """지표 이름·별칭 → 지표 사전의 이름 (조회 지표 포함). 모르면 None"""
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
    if lo == hi:  # 한 살 ('1990년생' → 35세)
        return f"{lo}세"
    return f"{lo}~{hi}세"


def _get_indicator(indicator, ages=None):
    """지표 이름·별칭 → (지표 이름, 정의). 계산 지표만, 공백은 무시한다.
    ages(연령 구간 목록)가 있고 지표가 총인구면 그 연령 구간의 인구 수 (예: 6~11세 인구)"""
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
    """문장에 들어 있는 지표 이름·별칭 → [(시작, 끝, 지표 이름)] (공백을 뺀 문장 기준 위치)

    짧은 별칭이 더 긴 별칭 안에 있으면 긴 쪽만 남긴다. 제외 표현(not_in)과 겹치면 세지 않는다.
    예) "고령인구비율" 안의 "인구"(총인구), "인구순유입"(순이동) 안의 "인구"는 세지 않는다.
    """
    s = _norm(text)
    spans = []
    for name, names in _alias_map().items():
        for n in {_norm(x) for x in [name, *names]}:
            spans += [(m.start(), m.end(), name) for m in re.finditer(re.escape(n), s)]
    # 제외 표현 안에 든 별칭은 세지 않는다 (예: "등록외국인수" 안의 "외국인수")
    blocked = [
        (m.start(), m.end(), name)
        for name, phrases in _exclusions().items()
        for n in map(_norm, phrases)
        for m in re.finditer(re.escape(n), s)
    ]
    spans = [
        a for a in spans if not any(b[2] == a[2] and b[0] < a[1] and a[0] < b[1] for b in blocked)
    ]
    return [
        a
        for a in spans
        if not any(b[0] <= a[0] and a[1] <= b[1] and b[1] - b[0] > a[1] - a[0] for b in spans)
    ]


def kosis_url(tbl_id):
    """KOSIS 통계표 화면 주소. 기관 ID는 수집 메타데이터(catalog 테이블)에서 읽는다
    서비스 기본 20개 표 밖의 표(질문 처리 중 바로 받은 표 등)는 표 목록 2,364개(table_meta)에서"""
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
    if ind.get("households"):  # 세대당 인구: 분모인 세대수 표도 출처로 밝힌다
        out["extra_sources"] = [
            {
                "source": HH_SOURCE,
                "basis": HH_BASIS,
                "tbl_id": HH_TABLE,
                "url": kosis_url(HH_TABLE),
            }
        ]
    if ind.get("foreigners"):  # 등록외국인 비율: 분자인 등록외국인 표도 출처로 밝힌다
        out["extra_sources"] = [
            {
                "source": FOREIGN_SOURCE,
                "basis": FOREIGN_BASIS,
                "tbl_id": FOREIGN_TABLE,
                "url": KOSIS_URL.format(org_id=FOREIGN_ORG, tbl_id=FOREIGN_TABLE),
            }
        ]
    if ind.get("area"):  # 인구밀도: 분모인 면적 표도 출처로 밝힌다 (qa.render·sources)
        out["extra_sources"] = [
            {
                "source": AREA_SOURCE,
                "basis": AREA_BASIS,
                "tbl_id": AREA_TABLE,
                "url": KOSIS_URL.format(org_id=AREA_ORG, tbl_id=AREA_TABLE),
            }
        ]
    out.update(extra)
    # 지표 사전에 적은 주의 문구(note)도 답에 붙인다 (조회 지표는 lookup이 붙인다. 11/18 전에는
    # 계산 지표만 빠져 중위연령 · 등록외국인 비율의 기준 안내가 나가지 않았다)
    if (note := (specs().get(name) or {}).get("note")) and status in ("ok", "out_of_range"):
        notes = list(out.get("notes") or [])
        if note not in notes:
            out["notes"] = [*notes, note]
    return out


def _display(reg):
    """답·되묻기에 보이는 지역 이름: "경상남도 창원시", "경기도 부천시 원미구"
    regions.path는 "경기도 > 원미구"처럼 시가 빠져 있어 쓰지 않는다
    (path는 표 지역 잇기에 쓰이므로 그대로 둔다)"""
    name = _base_name(reg["region_name"])
    if reg["kind"] in ("전국", "시도") or not reg.get("sido_name"):
        return name
    if reg["kind"] == "일반구":
        # 일반구 코드 앞 4자리 = 시 코드 앞 4자리 (부천시 41190 → 원미구 41192)
        city = _rows(
            "select region_name from regions where kind = '시군구' and region_code = ?",
            [reg["region_code"][:4] + "0"],
        )
        if city:
            return f"{reg['sido_name']} {_base_name(city[0]['region_name'])} {name}"
    return f"{reg['sido_name']} {name}"


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
    except StatsTableMissing:
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


def _out_of_range_reason(lo, hi, ind=None):
    if (ind or {}).get("foreigners"):  # 기간을 정한 것은 붙인 등록외국인 표다
        return f"등록외국인(법무부) 자료가 {lo}~{hi}년(연간)만 있어 이 기간만 계산할 수 있습니다."
    reason = f"수집된 주민등록인구 자료는 {lo}~{hi}년(연간)만 있습니다."
    if m := population_latest.components("00"):  # 받아 온 최신 달 (설정의 적어 둔 값보다 정확)
        return reason + f" KOSIS 원표에는 {m['year']}년 {m['month']}월(월별)까지 자료가 있습니다."
    rows = _catalog_rows("select latest_available from table_notes where tbl_id = ?", [POP_TABLE])
    if rows and rows[0]["latest_available"]:
        reason += f" KOSIS 원표에는 {rows[0]['latest_available']}까지 자료가 있습니다."
    return reason


def _components(codes, y0, y1, sex, band=None, area=False, ind=None):
    """연도·지역별 총인구, 0~14세, 15~64세, 65세 이상 (100세 이상 포함)
    band=(시작 연령, 끝 연령 또는 None)이면 그 구간 인구(band)도 더한다
    area=True면 면적(area, ㎢)도 붙인다 (인구밀도)
    ind(지표 정의)에 sexes·median·households가 있으면 남녀 인구·중위연령·세대수도 붙인다"""
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
    """1세별 인구 {나이: 인구} → 중위연령. 가운데 사람이 든 나이 안에서 고르게 퍼져 있다고 보고 보간
    예) 0~39세 누적이 48%, 40세가 4%면 중위연령 = 40 + (50 - 48) / 4 = 40.5세"""
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
    """성비: 남자(male)·여자(female) 총인구를 붙인다. 최신 달 값은 KOSIS 월별 자료에서"""
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
    """중위연령(median_age): 1세별 인구로 계산. 최신 달 값은 KOSIS 월별 자료의 1세별 인구로"""
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
    """세대당 인구: 그해 주민등록 세대수(households)를 붙인다 (DT_1B040B3, 조회 지표와 같은 방식).
    세대수 표가 적재되지 않았거나 그해 값이 없으면 붙이지 않는다 (값 없음으로 답한다)"""
    from app.services import lookup  # lookup이 indicators를 불러서 함수 안에서 부른다

    try:
        got = lookup._fetch({"table": HH_TABLE, "item_id": "T1"}, list(codes), y0, y1)
    except StatsTableMissing:
        return
    for key, comp in comps.items():
        comp["households"] = got.get(key)


def _attach_foreigners(comps, codes, y0, y1, sex):
    """등록외국인 비율: 그해 등록외국인 수(foreigners)를 붙인다 (DT_1B040A9C, 성별마다 칸이 다름).
    표가 적재되지 않았거나 그해 값이 없으면 붙이지 않는다 (값 없음으로 답한다)"""
    from app.services import lookup  # lookup이 indicators를 불러서 함수 안에서 부른다

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
    """지표가 답할 수 있는 연도 (lo, hi, 최신 달 허용). 다른 표를 붙이는 지표는 그 표의 연도까지"""
    lo, hi = _data_years()
    if ind.get("foreigners"):
        from app.services import lookup

        try:
            flo, fhi = lookup._years({"table": FOREIGN_TABLE, "item_id": "H001"})
        except StatsTableMissing:
            return lo, hi, False
        return max(lo, flo), min(hi, fhi), False  # 등록외국인은 연간 자료만 (최신 달 없음)
    return lo, hi, True


def _attach_area(comps, codes, y1):
    """인구 값에 그해 면적(㎢)을 붙인다. 면적 자료가 아직 없는 해(연간 자료 다음 해의 최신 달 등)는
    그 전 가장 최근 해의 면적을 쓰고 area_year에 그 해를 적는다"""
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
    """인구밀도인데 면적 자료(mart.area)가 아직 없으면 안내용 dict, 있으면 None"""
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
    """면적이 그해 것이 아니면 안내 문장"""
    for (y, _), c in comps.items():
        if y == year and c.get("area_year") and c["area_year"] != year:
            return f"{year}년 면적 자료가 아직 없어 {c['area_year']}년 말 면적으로 계산했습니다"
    return None


def _month_values(codes, sex, year):
    """연간 자료에 없는 해(year)의 월별 값 {지역 코드: 값}.
    올해면 최신 달, 최신 달이 이미 다음 해로 넘어갔으면 그해 12월 (연간 자료와 같은 12월 말 기준.
    수집본(mart.population)은 전처리를 다시 돌려야 새 해가 들어가서, 그 전까지 이 값으로 답한다)"""
    latest = population_latest.components_many(codes, sex)
    out = {c: m for c, m in latest.items() if m["year"] == year}
    later = [c for c, m in latest.items() if m["year"] > year]
    if later:
        december = population_latest.components_many(later, sex, f"{year}12")
        out |= {c: m for c, m in december.items() if m["year"] == year}
    return out


def _monthly(code, sex, year):
    """연간 자료에 없는 해(year)의 월별 값 하나. KOSIS에 아직 없거나 연간 자료가 있는 해면 None"""
    if year <= _data_years()[1]:  # 연간 값으로 답하는 해: 월별 기준 안내를 붙이지 않는다
        return None
    return _month_values([code], sex, year).get(code)


def latest_year(region_names, sex="계"):
    """최근·현재를 물은 질문의 연도.
    첫 지역의 최신 달이 연간 자료보다 새로우면 그 해, 아니면 연간 마지막 해"""
    hi = _data_years()[1]
    code = "00"  # 지역 없이 묻는 순위는 전국의 최신 달로 본다
    if region_names:
        reg = resolve_region(region_names[0], None, sigungu=True)
        if reg.get("status") != "ok":
            return hi
        code = reg["region_code"]
    m = population_latest.components(code, sex)
    return m["year"] if m and m["year"] > hi else hi


def _mark_monthly(out, code, sex, year):
    """최신 달 값으로 답한 결과에 기준 시점을 적는다 (연간 값은 12월 말, 이 값은 그 달 말)"""
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


# 새로 생긴 지역의 출범 · 설치 연도. 그 전의 자료는 어느 표에도 없다.
# regions.valid_from은 쓰지 않는다: 통합(창원시) · 이름 바꿈(당진군 → 당진시, 남구 → 미추홀구) ·
# 출장소 코드처럼 그 전에도 같은 곳의 자료가 있는 경우가 섞여 있다
FOUNDED = {"세종특별자치시": 2012, "계룡시": 2003, "증평군": 2003}


def founded_year(reg) -> int | None:
    """그 지역이 새로 생긴 해 (FOUNDED에 없으면 None)"""
    return FOUNDED.get(reg.get("region_name"))


def not_yet_founded(reg, year, indicator) -> dict | None:
    """그 지역이 생기기 전의 해를 물었으면 '그해에 없던 지역' 안내 (조회 지표용, lookup._resolve)"""
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
    """물은 기간이 모두 그 지역이 생기기 전이면 '그해에 없던 지역' 안내, 아니면 None.
    수록 기간보다 먼저 본다: "1980년 세종시 인구"는 자료가 2008년부터라서가 아니라 세종시가
    2012년에 생겨서 없는 것이고, 어느 표로 가도 같다 (final: 다른 통계표를 찾아보지 않는다)"""
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
    """수록 기간·지역 유효 기간 확인. 문제가 있으면 안내용 dict, 없으면 None"""
    if (out := _not_yet(name, ind, reg, y1)) is not None:
        return out
    lo, hi, month_ok = _years_of(ind)
    # 연간 자료 다음 해는 같은 표의 최신 달 값으로 답한다 (_components). 기간은 맞는 것으로 보되
    # 아래 지역 유효 기간 확인은 그대로 한다 (예: 2023년에 생긴 군위군을 2020년부터 물으면 안내)
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
            elif proj.get("available_years"):  # 추계 범위(~2052)도 넘으면 추계 마지막 연도를 안내
                out["reason"] += " " + proj["reason"]
                out["available_years"] = proj["available_years"]
        elif y0 > hi and reg.get("sido_code") and "projection_c3" in ind:
            # 시군구의 미래 값: 장래인구추계는 시도까지만 있다 → 예측 질문으로 답하지 않고
            # 속한 시도의 추계값을 대신 보여 준다 (예: 2040년 해운대구 → 2040년 부산광역시)
            proj = projection(name, reg["sido_code"], y1)
            if proj["status"] == "ok":
                # sub_future: 연도를 콕 집어 물었으면 '없는 연도', "~될까"처럼 짐작을 물었으면
                # '예측 질문'이다. 질문의 말투는 여기서 모르므로 qa.compute가 가른다
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
    """지표·지역 해석. (지표 이름, 지표 정의, 지역 목록, 오류 dict)"""
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
        if reg["status"] != "ok":
            return name, ind, None, {**reg, "indicator": name}
        regions.append(in_year(reg, year))
    return name, ind, regions, None


def in_year(reg, year):
    """그 연도에 없던 지역이면, 그 해에 있던 같은 이름의 지역으로 바꾸고 note를 단다
    예) 2020년 대구 군위군 → 경상북도 군위군 (2023년 대구 편입 전)"""
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
        # 같은 이름이 여러 곳이면(예: 2026년 광주 동구 → 부산·대구·대전·울산 동구와 12210)
        # 같은 행정구역 변경 기록(region_changes)에 함께 적힌 지역만 남긴다
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


# ---- 공개 함수 ------------------------------------------------------------------


@lru_cache
def _region_names():
    """질문 속 지역 찾기용 이름 목록 (별칭 + 시도·시군구 이름, 긴 것부터)"""
    names = {r["alias"] for r in _rows("select alias from region_aliases")}
    names |= {r["region_name"] for r in _rows("select region_name from regions")}
    names |= set(AMBIGUOUS_NAMES)
    names -= {"합계", "총계", "전국", "계"}  # 표의 합계 행 이름 (지역이 아님)
    return sorted((_norm(n) for n in names if len(_norm(n)) >= 2), key=len, reverse=True)


def find_regions(text):
    """문장에 나온 지역 이름들 (나온 순서). HCX가 지역을 빠뜨렸을 때 쓴다
    더 긴 이름 안에 든 짧은 이름(예: "광주광역시" 안의 "광주")은 세지 않는다"""
    s = _norm(text)
    taken, found = [], []
    for name in _region_names():
        for m in re.finditer(re.escape(name), s):
            if m.start() and s[m.start() - 1].isdigit():
                continue  # "30대 전체"의 '대전': 앞에 숫자가 붙은 것은 연령대 · 수량이다
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append((m.start(), name))
    return [name for _, name in sorted(found)]


def _base_name(name):
    """비교용 지역 이름: "창원시(통합)" → "창원시", "포항시(계)" → "포항시"
    (load_area.SUFFIX와 같은 꼬리)"""
    return re.sub(r"\((?:계|통합)\)$", "", name or "")


def _change_groups():
    """행정구역 개편 기록마다 함께 적힌 코드 묶음"""
    return [
        set(r["region_codes"].split(",")) for r in _rows("select region_codes from region_changes")
    ]


def _same_place(a, b):
    """옛·새 코드가 이어지는 같은 지역인가.
    이름이 같고(꼬리 무시) 같은 시도이거나 같은 개편 기록에 함께 적힌 코드
    예) 원미구 41195·41192, 군위군 47720·27720, 광주 동구 29110·12210.
    인천 동구 28140과 부산 동구 26170은 이름만 같은 다른 지역"""
    if _base_name(a["region_name"]) != _base_name(b["region_name"]):
        return False
    if a["region_code"] == b["region_code"]:
        return True
    if a.get("sido_code") and a.get("sido_code") == b.get("sido_code"):
        return True
    pair = {a["region_code"], b["region_code"]}
    return any(pair <= g for g in _change_groups())


def resolve_region(name, year=None, sigungu=False):
    """지역 이름·별칭·코드 → 지역 정보

    "서울", "서울특별시", "11" → 서울특별시
    "중구" → ambiguous_region (후보 목록)
    "서울 중구" → 서울특별시 중구 (시도 + 시군구)
    같은 이름에 코드가 여럿이면(예: 부천 원미구) year로 그 해에 있던 코드를 고른다.
    sigungu=True(시군구 자료가 있는 지표)면 "광주"처럼 시도 약칭이 시군구 이름(경기 광주시)과
    겹칠 때 되묻는다. "충청도"처럼 여러 시도를 가리키는 이름도 되묻는다.
    """
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
    # "안산" → 안산시, "고성" → 고성군, "강남" → 강남구 (같은 이름이 여럿이면 아래에서 되묻는다)
    for suffix in ("시", "군", "구"):
        if not found and not key.endswith(("시", "군", "구", "도")):
            found = _rows(sql.format(extra=""), [key + suffix, key + suffix])

    parts = str(name).split()
    if not found and len(parts) >= 2:  # "서울 중구"
        sido = resolve_region(parts[0])
        if sido["status"] == "ok" and sido["kind"] == "시도":
            rest = _norm("".join(parts[1:]))
            found = _rows(
                sql.format(extra="and r.sido_code = ?"), [rest, rest, sido["region_code"]]
            )
            # "경기 광주" → 경기도 광주시, "서울 강남" → 서울특별시 강남구
            for suffix in ("시", "군", "구"):
                if not found and not rest.endswith(("시", "군", "구")):
                    found = _rows(
                        sql.format(extra="and r.sido_code = ?"),
                        [rest + suffix, rest + suffix, sido["region_code"]],
                    )

    # "부천 원미구", "수원시 영통구", "경기 부천 원미구": 앞말이 시면 그 시 안의 일반구에서 찾는다.
    # 일반구 코드는 앞 4자리가 시 코드와 같다
    # (수원시 41110 → 영통구 41117, 부천시 41190 → 원미구 41192)
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
                # "제주"(제주특별자치도 / 그 안의 제주시): 같은 시도 안의 시라 시도로 답하고 알린다.
                # "광주"(광주광역시 / 경기 광주시)처럼 다른
                # 시도의 시와 겹치면 아래처럼 되묻는다 (11/18)
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
        # 같은 지역의 옛 코드만 빠지면(부천 원미구) 그 해 코드로 고른다.
        # 다른 지역이 그 해에 없으면(2026년 광주: 광주광역시 2025년까지) 남은 지역(경기 광주시)으로
        # 말없이 답하지 않고 되묻는다. 없던 지역을 고르면 region_not_in_year 안내로 간다
        # 그 해에 없는 코드는 이어지는 지금 코드가 후보에 있을 때만 하나씩 뺀다
        # (2026년 동구: 광주 동구 29110은 전남광주 동구 12210이 있어 빼고, 인천 동구는 남김)
        found = [r for r in found if r in alive or not any(_same_place(r, a) for a in alive)]

    # 연도를 말하지 않았고 같은 지역의 옛·새 코드만 남았으면 지금 코드
    # ("창원시" → 창원시(통합) 48120, "원미구" → 2024년에 다시 생긴 41192). 다른 지역끼리면 되묻는다
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
    """단순 조회: 한 지역·한 연도의 지표 값"""
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
        # 두 값을 나눈 지표: 나눈 두 값을 함께 보여 준다 (예: 등록외국인 ○명 ÷ 주민등록인구 ○명)
        out["operands"] = [
            {"label": label, "value": _operand(comp.get(key)), "unit": unit}
            for key, label, unit in ind["operands"]
        ]
    return _mark_monthly(out, reg["region_code"], sex, year)


def trend(indicator, region, start_year, end_year, sex="계", ages=None):
    """추이 분석: 연도별 값 + 처음 대비 마지막 변화 (총인구면 증감률 %)"""
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
    # 변화량 · 증감률은 반올림 전 값으로 (반올림한 값끼리 빼면 끝자리가 0.01 틀린다. 11/18)
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
        # 비율 지표: %p 변화에 더해 비율 자체가 몇 % 커졌는지 (증감률). %p와 헷갈리지 않게
        # rate가 아닌 따로 된 칸으로 두고 "비율 자체는 ~% 커짐"으로 보여 준다 (qa._body)
        change["relative_rate"] = round(analysis.growth_rate(first, last), 1)
    years = series[-1]["year"] - series[0]["year"]
    if "rate" in change and years and first and last and first > 0 and last > 0:
        # 연평균 증감률도 반올림 전 값으로 (qa._add_trend_facts는 이미 있으면 그대로 둔다)
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
    """지역 비교: 여러 지역의 같은 연도 값, 높은 순 정렬 + 최고·최저 차이"""
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
    # 차이는 반올림 전 값으로 (trend와 같게. 표시값끼리 빼면 끝자리가 0.01 틀린다)
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
    """구성비: 한 지역·한 연도 인구의 연령대별(by="age") 또는 성별(by="sex") 비중"""
    name, ind, regs, err = _prepare("총인구", [region], year)
    if err:
        return err
    reg = regs[0]
    if bad := _check(name, ind, reg, year, year):
        return bad
    code = reg["region_code"]
    if by == "sex":
        sex = "계"  # 성별 구성은 남녀 전체가 대상
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
    shares = analysis.composition(counts, total)  # 분모는 '계' 값 (연령 미상 등이 있어도 맞게)
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
    """순위 대상 지역 조건 (regions 별칭 r). level: 시도 / 시군구 / 시(시군구 중 '시'만)
    결과: (조건 SQL, 인자, 안내용 범위 이름) 또는 sido를 못 찾으면 오류 dict"""
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
    """순위: 시도 전체 또는 시군구(기초자치단체) 전체 순위. sido를 주면 그 시도 안의 시군구만.
    target: "부산은 몇 위?"처럼 한 지역의 순위를 물었으면 그 지역 (lookup.rank와 같다)"""
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
    # 연간 자료 다음 해: 최신 달 값으로 순위
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
        # 최신 달 순위는 그해에 있는 지역으로 (연간 마지막 해의 지역으로 하면 그 뒤 생기거나
        # 합쳐진 지역(2026 전남광주 등)이 최신 달 자료와 코드가 달라 조용히 빠진다)
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
    items = analysis.rank_rows(items, descending=not ascending)  # 같은 값은 같은 순위
    notes = _optional(_area_note(comps, year))
    if missing:  # 빠진 지역을 알리지 않으면 "150곳 중 1위"처럼 전체인 것처럼 보인다
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
    # "실제 통계가 아닌 추계값입니다."는 답변 문장(qa._projection_text)에 있어 여기서는 뺀다
    notes = [
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
