"""사전에 없는 지표를 통계표에서 바로 계산한다 (동적 지표 계산 1단계: 규칙만, LLM 추가 호출 없음)

지표 사전(config/catalog_notes.toml)에 없는 지표는 통계표 찾기가 표를 고르고 표 안내로 끝났다.
여기서는 그 표의 칸(항목·분류) 목록과 질문 해석 결과(지역·연도·성별·연령·질문 종류)로
지표 사전의 sources와 같은 모양의 정의(계산 계획)를 만든다. 계산은 조회 지표와 똑같이
lookup.py(값·비교·추이·순위·구성비)가 한다. 그래서 숫자는 항상 raw.stat_value에서 나온다.

  칸 고르기 (표의 분류마다 하나)
    지역         lookup이 질문마다 고른다 (region_code_map)
    성별         질문의 성별 → 남자·여자 칸. 성별 칸은 항목에 있기도 하다 (예: 1인가구·남자·여자)
    연령         질문의 연령 구간 → lookup의 age_dim (말하지 않으면 합계)
    그 밖의 분류  칸 이름이 질문에 그대로 있으면 그 칸 (공백·기호 무시, 긴 이름 우선)
                 없으면 합계 칸. 합계 칸도 없고 칸이 여럿이면 고르지 못한다
  연산
    값·비교·추이·순위  고른 칸의 값
    비율               "~ 중 ~ 비율": 질문에서 고른 칸 하나 ÷ 같은 분류의 합계 칸 × 100
    구성비             질문에 이름이 나온 분류(예: 연령별)의 칸마다 ÷ 합계
  검증 (하나라도 걸리면 None → 지금처럼 통계표 안내)
    - 장래추계 표인데 실적 자료가 있는 연도를 물음 (추계 표는 미래 연도에만)
    - 질문의 말과 맞은 칸이 하나도 없음 (표의 다른 값을 답하지 않도록)
    - 한 분류에서 칸이 둘 이상 맞음, 합계 칸이 없는데 고를 수 없음
    - 비율의 분자가 정해지지 않음 (나눌 부분이 없거나 여럿)
    - 계산은 됐지만 값이 하나도 없음 (비공개 *·결측) → 다음 후보 표

답에는 계산식·쓴 표·칸과 "지표 사전에 없는 값을 표에서 바로 계산했다"는 주의를 붙인다.
"""

import logging
import re
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from app.core.stats_db import StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import indicators, load_queue, lookup, plan_cache, qa, search, table_cache

logger = logging.getLogger(__name__)

MAX_TABLES = 3  # 통계표 찾기 후보 중 이만큼까지 시도한다
NOTE = "지표 사전에 없는 값을 통계표에서 바로 계산했습니다. 공식 지표 정의와 다를 수 있습니다"
PROJECTION_NOTE = "장래추계 표의 추계값입니다(실적 아님). 다음 추계 때 바뀔 수 있습니다"
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총", "전국"}
SEX_NAMES = {"남": {"남자", "남", "남성"}, "여": {"여자", "여", "여성"}}
COUNT_UNITS = {"명", "가구", "건", "개", "세대", "호", "곳", "개소", "천명", "천가구"}
PCT_ASK = re.compile(r"%|퍼센트|비율|비중|몇\s*프로")
NORM = re.compile(r"[\s·,.()\[\]{}<>\"'‘’“”_/-]")


def _norm(text: str) -> str:
    return NORM.sub("", str(text))


@dataclass
class Dim:
    no: int  # 0 = 항목, 1~5 = c1~c5
    name: str
    codes: dict[str, str]  # KOSIS 코드 → 이름

    @property
    def col(self) -> str:
        return "item_id" if self.no == 0 else f"c{self.no}_id"

    def total(self) -> str | None:
        found = [c for c, n in self.codes.items() if _norm(n) in TOTAL_NAMES]
        return found[0] if len(found) == 1 else None

    def default(self) -> str | None:
        """말하지 않으면 쓸 칸 (합계가 없는 분류). 장래추계 시나리오는 중위 (지표 사전과 같음)"""
        names = {c: _norm(n) for c, n in self.codes.items()}
        if any(n.startswith(("고위", "저위")) for n in names.values()):
            middle = [c for c, n in names.items() if n.startswith("중위")]
            if len(middle) == 1:
                return middle[0]
        return None

    def sex_codes(self) -> dict[str, str]:
        """{"남": 코드, "여": 코드} (둘 다 있을 때만)"""
        out = {}
        for sex, names in SEX_NAMES.items():
            hits = [c for c, n in self.codes.items() if _norm(n) in names]
            if len(hits) == 1:
                out[sex] = hits[0]
        return out if len(out) == 2 else {}

    def is_age(self) -> bool:
        ages = [n for n in self.codes.values() if lookup.parse_age(n)]
        return len(ages) >= 3 and len(ages) * 2 >= len(self.codes)


@dataclass
class Plan:
    """고른 칸. 분류마다 코드 하나 (연령은 age_dim으로 lookup이 고른다)"""

    pick: dict[int, str] = field(default_factory=dict)
    matched: dict[int, str] = field(default_factory=dict)  # 질문의 말로 고른 칸
    defaults: list[str] = field(default_factory=list)  # 말하지 않아 기본값으로 고른 칸 (답에 알림)
    sex_dim: Dim | None = None
    age_dim: Dim | None = None


# ---- 표의 칸 목록 ----------------------------------------------------------------


def schema(tbl_id: str) -> tuple[list[Dim], int | None, str]:
    """(분류 목록, 지역 분류 번호, 지역 수준). 적재 안 된 표면 StatsTableMissing
    칸 이름·지역 칸은 lookup의 표 정보 캐시를 쓴다 (표를 다시 적재하면 함께 비워진다)"""
    key = lookup._tbl_key(tbl_id)
    names = {
        r["dim_no"]: r["name"]
        for r in _rows("select dim_no, name from stat_dimension where tbl_key = ?", [key])
    }
    dim_nos = [
        r["dim_no"]
        for r in _rows(
            "select distinct dim_no from stat_code where tbl_key = ? order by dim_no", [key]
        )
    ]
    dims = [
        Dim(n, names.get(n, "항목" if n == 0 else f"분류{n}"), dict(_labels(tbl_id, n)))
        for n in dim_nos
    ]
    region_dim = lookup._region_dim(tbl_id)
    longest = _rows(
        "select max(length(region_code)) as n, count(distinct region_code) as k"
        " from region_code_map where tbl_id = ?",
        [tbl_id],
    )[0]
    if region_dim is None or not longest["n"]:
        level = "전국"
    elif longest["n"] >= 5:
        level = "시군구"
    elif longest["k"] > 1:
        level = "시도"
    else:
        level = "전국"
    return dims, region_dim, level


def is_projection(tbl_id: str) -> bool:
    """장래추계 표인가 (서비스 기본 표는 catalog의 표시, 나머지는 조사·표 이름)"""
    if lookup._projection(tbl_id):
        return True
    r = _rows("select survey_name, table_name from table_meta where tbl_id = ?", [tbl_id])
    return bool(r and search.PROJECTION_TABLE.search(f"{r[0]['survey_name']} {r[0]['table_name']}"))


def _asks_future(q: dict, after: int) -> bool:
    """물은 연도가 모두 실적 자료의 마지막 해 뒤인가. 연도를 말하지 않았으면 아니다
    (말하지 않으면 추계 표의 마지막 해(예: 2072년)로 답하게 된다)"""
    years = [y for y in (q.get("year"), q.get("start_year"), q.get("end_year")) if y]
    return bool(years) and min(years) > after


def _labels(tbl_id: str, dim_no: int) -> list[tuple[str, str]]:
    return sorted((code, name) for code, (_, name) in lookup._codes(tbl_id, dim_no).items())


def _unit(tbl_id: str, item: str) -> tuple[str | None, bool]:
    """(단위, 값이 모두 정수인가)
    단위가 명이어도 평균 가구원 수(2.3명)처럼 소수면 사람 수가 아니다"""
    cond, keys = lookup._in("v.item_key", tbl_id, 0, [item])
    r = _rows(
        "select mode() within group (order by u.name) as u,"
        " bool_and(v.value = trunc(v.value)) as whole"
        " from stat_value v join stat_unit u using (unit_key)"
        f" where v.tbl_key = ? and v.{lookup.YEARLY} and {cond}",
        [lookup._tbl_key(tbl_id), *keys],
    )
    return (r[0]["u"], bool(r[0]["whole"])) if r else (None, False)


# ---- 칸 고르기 -------------------------------------------------------------------


def _text_hits(question: str, dims: list[Dim], skip: set[int]) -> dict[int, list[str]]:
    """질문에 이름이 그대로 나온 칸 {분류 번호: [코드...]}. 더 긴 이름 안에 든 짧은 이름은 뺀다"""
    q = _norm(question)
    spans = []
    for d in dims:
        if d.no in skip:
            continue
        for code, name in d.codes.items():
            n = _norm(name)
            if len(n) < 2 or n in TOTAL_NAMES:
                continue
            spans += [(m.start(), m.end(), d.no, code) for m in re.finditer(re.escape(n), q)]
    kept = [
        a
        for a in spans
        if not any(b[0] <= a[0] and a[1] <= b[1] and b[1] - b[0] > a[1] - a[0] for b in spans)
    ]
    hits: dict[int, list[str]] = {}
    for *_, no, code in kept:
        hits.setdefault(no, [])
        if code not in hits[no]:
            hits[no].append(code)
    return hits


@dataclass
class Ask:
    """칸을 하나로 정하지 못해 사용자에게 물을 것 (한 분류의 후보 칸들)"""

    dim_no: int
    dim_name: str
    options: list[tuple[str, str]]  # (코드, 이름)

    def __str__(self) -> str:
        return f"{self.dim_name}에서 칸을 고르지 못함 (후보 {len(self.options)}개)"


MAX_ASK_OPTIONS = 8  # 이보다 칸이 많은 분류는 되묻지 않고 표 안내 (버튼이 너무 많아진다)


def choose(
    question: str,
    parsed: dict,
    dims: list[Dim],
    region_dim: int | None,
    forced: dict[int, str] | None = None,
):
    """(Plan, None) 또는 (None, 고르지 못한 이유: 문자열 또는 Ask)
    forced: 사용자가 되묻기에서 고른 칸 {분류 번호: 코드}"""
    forced = forced or {}
    plan = Plan()
    sex = parsed.get("sex") or "계"
    for d in dims:
        if d.no != region_dim and d.sex_codes():
            plan.sex_dim = d
        elif d.no not in (0, region_dim) and d.is_age() and d.total():
            plan.age_dim = d
    if plan.sex_dim is None and sex in ("남", "여"):
        return None, "표에 성별 구분이 없음"
    if plan.age_dim is None and parsed.get("ages"):
        return None, "표에 연령 구분이 없음"
    skip = {region_dim} | ({plan.age_dim.no} if plan.age_dim else set())
    hits = _text_hits(question, dims, skip)
    asks: list[Ask] = []
    for d in dims:
        if d.no in skip:
            continue
        if d.no in forced and forced[d.no] in d.codes:
            plan.pick[d.no] = plan.matched[d.no] = forced[d.no]
            continue
        codes = hits.get(d.no, [])
        if d is plan.sex_dim:
            # 성별 칸 말고 다른 칸(예: 항목의 1인가구)이 질문에 나왔으면 그것이 '계'
            sex_set = set(d.sex_codes().values())
            others = [c for c in codes if c not in sex_set]
            base = (others[:1] or [d.total()] or [None])[0]
            if base is None:
                rest = [c for c in d.codes if c not in sex_set]
                base = rest[0] if len(rest) == 1 else None
            if len(others) > 1:
                asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in others]))
            elif base is None:
                return None, f"{d.name}에서 칸을 고르지 못함"
            else:
                plan.pick[d.no] = base
                if others:
                    plan.matched[d.no] = base
            continue
        if len(codes) > 1:
            asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in codes]))
        elif codes:
            plan.pick[d.no] = plan.matched[d.no] = codes[0]
        elif d.total():
            plan.pick[d.no] = d.total()
        elif len(d.codes) == 1:
            plan.pick[d.no] = next(iter(d.codes))
        elif (default := d.default()) is not None:
            plan.pick[d.no] = default
            plan.defaults.append(f"{d.name}: {d.codes[default]}")
        elif len(d.codes) <= MAX_ASK_OPTIONS:
            asks.append(Ask(d.no, d.name, list(d.codes.items())))
        else:
            return None, f"{d.name}에서 칸을 고르지 못함"
    if not plan.matched and not (plan.sex_dim and sex in ("남", "여")):
        return None, "질문의 말과 맞는 칸이 없음"  # 맞는 말이 없는 표는 되묻지도 않는다
    if asks:
        return None, asks[0]
    return plan, None


# ---- 계산 계획 (지표 사전의 sources와 같은 모양) --------------------------------------


def _has_age_dim(dims: list[Dim], region_dim: int | None) -> bool:
    return any(d.no not in (0, region_dim) and d.is_age() and d.total() for d in dims)


def _filters(plan: Plan, dims: list[Dim], region_dim: int | None) -> dict:
    return {
        d.col: plan.pick[d.no] for d in dims if d.no not in (0, region_dim) and d.no in plan.pick
    }


def _sex_override(plan: Plan, sex: str) -> dict:
    d = plan.sex_dim
    code = plan.pick[d.no] if sex == "계" else d.sex_codes()[sex]
    return {"item_id": code} if d.no == 0 else {"filters": {d.col: code}}


def _name(parsed: dict, dims: list[Dim], plan: Plan, counted: bool) -> str:
    """지표 이름: 질문 해석의 지표 표현이 고른 칸 이름을 모두 담고 있으면 그 표현, 아니면 칸 이름
    (HCX가 지표 칸에 "2020년 서울"처럼 지표가 아닌 말을 넣는 경우가 있다)"""
    words = [d.codes[plan.matched[d.no]] for d in dims if d.no in plan.matched]
    words = words or [dims[0].codes[plan.pick[0]]]
    for term in (parsed.get("term"), parsed.get("indicator")):
        if not term or indicators.canonical_name(term) or re.search(r"\d{4}", str(term)):
            continue
        if indicators.find_regions(str(term)):  # "전국 평균 가구원 수"처럼 지역까지 담은 표현
            continue
        if all(_norm(w) in _norm(term) for w in words):
            return str(term)
    name = " ".join(words)
    return f"{name} 수" if counted and not name.endswith("수") else name


def _cells(plan: Plan, dims: list[Dim]) -> str:
    """답에 보여 줄 고른 칸 (예: 항목 1인가구, 연령별 합계)"""
    return ", ".join(
        f"{d.name} {d.codes[plan.pick[d.no]]}"
        for d in dims
        if d.no in plan.pick and len(d.codes) > 1
    )


def build(
    tbl_id: str, question: str, parsed: dict, forced: dict[int, str] | None = None
) -> tuple[dict | None, "str | Ask | None"]:
    """(지표 정의, None) 또는 (None, 이유 또는 Ask). 적재 안 된 표면 StatsTableMissing
    forced: 되묻기에서 고른 칸 {분류 번호: 코드}"""
    dims, region_dim, level = schema(tbl_id)
    if not dims or dims[0].no != 0:
        return None, "항목 정보가 없음"
    future_after = None
    if is_projection(tbl_id):
        future_after = indicators._data_years()[1]  # 실적(주민등록인구) 자료의 마지막 해
        if not _asks_future(parsed, future_after):
            return None, "장래추계 표는 미래 연도를 물을 때만 씀"
    ignore_ages = False
    if parsed.get("ages") and parsed.get("age_group") and not _has_age_dim(dims, region_dim):
        # "노년부양비"의 노년처럼 말에서 짐작한 연령(qa.correct의 age_group)은 지표 이름의 일부일 수
        # 있다. 표에 연령 분류가 없으면 연령 조건 없이 고른다 ("65세 이상"처럼 숫자로 말하면 지킨다)
        parsed, ignore_ages = {**parsed, "ages": None}, True
    plan, why = choose(question, parsed, dims, region_dim, forced)
    if plan is None:
        return None, why
    src = {"table": tbl_id, "level": level, "item_id": plan.pick[0]}
    if level == "전국":
        src["national_only"] = True
    filters = _filters(plan, dims, region_dim)
    if plan.sex_dim is not None:
        filters.pop(plan.sex_dim.col, None)
    if filters:
        src["filters"] = filters
    if plan.age_dim is not None:
        d = plan.age_dim
        bands = [lookup.parse_age(n) for n in d.codes.values()]
        src.update(
            age_dim=f"c{d.no}",
            age_total=d.total(),
            age_min=min(b[0] for b in bands if b),
        )
    unit, whole = _unit(tbl_id, plan.pick[0])
    unit = unit or ""
    counted = unit in COUNT_UNITS and whole
    name = _name(parsed, dims, plan, counted)
    spec = {
        "name": name,
        "unit": unit,
        "kind": "count" if counted else "value",
        "method": "lookup",
        "dynamic": True,
        "note": NOTE,
        "sources": [src],
    }
    kind = parsed.get("type")
    if kind == "composition" or PCT_ASK.search(question):
        made = _composition(spec, src, plan, dims, question) if kind == "composition" else None
        made = made or _ratio(spec, src, plan, dims, parsed)
        if made is None:
            return None, "비율의 분자·분모를 정하지 못함"
        spec = made
    elif plan.sex_dim is not None:
        src["by_sex"] = {s: _sex_override(plan, s) for s in ("계", "남", "여")}
    # 이 정의를 만들 때 고정한 조건. 다음 턴에서 바뀌면 qa.compute가 쓰지 않고 다시 만들게 한다
    # (예: 여자 비율로 만든 정의를 "남자는?"에 쓰지 않음, 구성비 정의를 값 질문에 쓰지 않음)
    spec["fixed"] = {"sex": parsed.get("sex") or "계"} if "by_sex" not in src else {}
    if spec["kind"] == "ratio":
        spec["fixed"]["ages"] = parsed.get("ages") or None
    if spec["kind"] == "composition":
        spec["fixed"]["type"] = "composition"
    if future_after is not None:  # 다음 턴에서 과거 연도로 바꾸면 이 정의를 쓰지 않는다
        spec["future_after"] = future_after
        spec["note"] = f"{NOTE}. {PROJECTION_NOTE}"
    if plan.defaults:
        spec["note"] += f". 말하지 않은 구분은 기본값으로 계산했습니다 ({', '.join(plan.defaults)})"
    if ignore_ages:
        spec["ignore_ages"] = True  # qa.compute가 질문의 연령을 넘기지 않는다
    spec["formula"] = spec.get("formula") or f"KOSIS 공표 값 ({_cells(plan, dims)})"
    spec["cells"] = _cells(plan, dims)
    return spec, None


def _ratio(spec: dict, src: dict, plan: Plan, dims: list[Dim], parsed: dict) -> dict | None:
    """'~ 중 ~ 비율': 나눌 부분 하나 ÷ 같은 분류의 합계 × 100"""
    sex = parsed.get("sex") or "계"
    parts = []  # (분류 열, 분자 코드들, 분모 코드, 이름)
    if plan.sex_dim is not None and sex in ("남", "여"):
        d = plan.sex_dim
        word = "남자" if sex == "남" else "여자"
        parts.append((d.col, [d.sex_codes()[sex]], plan.pick[d.no], word))
    if plan.age_dim is not None and parsed.get("ages"):
        d = plan.age_dim
        band = tuple(parsed["ages"][0])
        try:
            codes = lookup._age_codes({**src, "table": src["table"]}, band)
        except lookup.AgeMismatch:
            return None
        parts.append((d.col, codes, d.total(), indicators.age_label(band)))
    for d in dims:
        if d.no in plan.matched and d.total() and plan.matched[d.no] != d.total():
            if d is not plan.sex_dim:
                code = plan.matched[d.no]
                parts.append((d.col, [code], d.total(), d.codes[code]))
    if len(parts) != 1:
        return None
    col, num, den, part = parts[0]
    num_src = _without_age(src) if plan.age_dim and col == plan.age_dim.col else dict(src)
    filters = dict(src.get("filters", {}))
    if col == "item_id":
        num_src["item_id"] = num if len(num) > 1 else num[0]
        num_src["den_item_id"] = den
        num_src["den_filters"] = filters
    else:
        num_src["filters"] = {**filters, col: num if len(num) > 1 else num[0]}
        num_src["den_item_id"] = src["item_id"]
        num_src["den_filters"] = {**filters, col: den}
    whole = next(d.codes for d in dims if d.col == col)
    out = {
        **spec,
        "unit": "%",
        "kind": "ratio",
        "formula": f"{part} ÷ {whole.get(den, '합계')} × 100",
        "sources": [num_src],
    }
    if plan.age_dim is not None and col == plan.age_dim.col:
        # 연령 구간을 분자 칸에 넣었다 (qa.compute가 연령을 다시 넘기지 않음)
        out["ages_in_filters"] = True
    return out


def _without_age(src: dict) -> dict:
    """연령을 lookup의 age_dim 대신 직접 거를 때 (연령이 비율의 분자이거나 구성비로 나눌 분류)"""
    return {k: v for k, v in src.items() if k not in ("age_dim", "age_total", "age_min")}


def _composition(spec: dict, src: dict, plan: Plan, dims: list[Dim], question: str) -> dict | None:
    """질문에 이름이 나온 분류(예: 연령별, 혼인상태별)의 칸마다 ÷ 합계"""
    q = _norm(question)
    for d in dims:
        if d.no == 0 or not d.total():
            continue
        stem = _norm(re.sub(r"(별|구분)$", "", d.name))
        if len(stem) >= 2 and stem in q:
            out = _without_age(src) if d is plan.age_dim else dict(src)
            out["filters"] = {k: v for k, v in src.get("filters", {}).items() if k != d.col}
            out.update(parts_dim=f"c{d.no}", parts_total=d.total())
            return {
                **spec,
                "unit": "%",
                "kind": "composition",
                "formula": "항목별 값 ÷ 합계 × 100",
                "sources": [out],
            }
    return None


# ---- 질문 처리 -------------------------------------------------------------------

LAST_YEAR_KINDS = ("value", "compare", "rank", "composition")


async def answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    """통계표 찾기가 고른 표로 바로 계산한 답 (qa.answer와 같은 모양). 못 하면 None → 표 안내

    후보 표를 순서대로 시도해 처음 계산된 표를 쓴다. 어느 표로도 계산하지 못했는데 앞 표에서
    칸 하나만 정하지 못했으면(예: 분류에 합계가 없음) 그 칸을 되묻는다 (status clarify_question).
    적재 안 된 작은 표는 바로 받아 넣는다 (table_cache.fetch_now).
    시도한 표와 결과는 trace["dynamic"]에 남긴다 (요청 기록)."""
    if found.get("status") not in ("ok", "partial_match"):
        return None  # 되묻기·범위 밖은 통계표 찾기 답을 그대로 쓴다
    trace.setdefault("dynamic", [])
    first_ask = None
    for card in (found.get("recommendations") or [])[:MAX_TABLES]:
        tbl_id = card.get("tbl_id")
        if not tbl_id:
            continue
        res, ask = await _try(tbl_id, card.get("org_id"), question, parsed, trace, {})
        if res is not None:
            await _remember(parsed, tbl_id, {}, res)
            return res
        if ask is not None and first_ask is None:
            first_ask = (card, ask)
    if first_ask is None:
        return None
    card, ask = first_ask
    return ask_answer(question, parsed, card["tbl_id"], card.get("tbl_nm") or card["tbl_id"], ask)


async def answer_choice(question: str, context: dict, code: str, trace: dict) -> dict | None:
    """되묻기에서 고른 칸으로 같은 표를 다시 계산한다 (질문 해석 HCX는 다시 부르지 않는다).
    다른 분류도 정하지 못했으면 그것을 이어서 묻는다. 못 하면 None"""
    forced = {**{int(k): v for k, v in (context.get("forced") or {}).items()}}
    forced[int(context["dim_no"])] = code
    parsed = context.get("parsed") or {}
    tbl_id = context["tbl_id"]
    trace.setdefault("dynamic", [])
    res, ask = await _try(tbl_id, None, question, parsed, trace, forced)
    if res is None and ask is not None:
        return ask_answer(question, parsed, tbl_id, context.get("tbl_nm") or tbl_id, ask, forced)
    if res is not None:
        await _remember(parsed, tbl_id, forced, res)
    return res


async def cached_answer(question: str, parsed: dict, trace: dict) -> dict | None:
    """계산 방법 캐시에 이 지표가 있으면 통계표 찾기 없이 그 표로 바로 계산한다 (plan_cache.py).
    칸 고르기·검증은 다시 하므로 그 표로 더 이상 계산되지 않으면 None → 지금처럼 통계표 찾기"""
    try:
        found = await run_in_threadpool(plan_cache.key_of, parsed)
        plans = await plan_cache.find(found[0]) if found else []
    except Exception:
        logger.exception("계산 방법 캐시 읽기 실패")
        return None
    trace["plan_cache"] = "miss" if found else "skip"
    trace.setdefault("dynamic", [])
    for plan in plans:
        forced = {int(k): v for k, v in (plan.get("forced") or {}).items()}
        res, ask = await _try(plan["tbl_id"], None, question, parsed, trace, forced)
        if res is None and ask is not None:
            res = ask_answer(question, parsed, plan["tbl_id"], plan["tbl_id"], ask, forced)
        if res is not None:
            trace["plan_cache"] = {"hit": plan["id"], "tbl_id": plan["tbl_id"]}
            if res.get("status") == "ok":
                try:
                    await plan_cache.used(plan["id"])
                except Exception:
                    logger.exception("계산 방법 캐시 기록 실패")
            return res
    return None


async def _remember(parsed: dict, tbl_id: str, forced: dict, res: dict) -> None:
    """바로 계산에 성공한 계획을 캐시에 남긴다. 실패해도 답은 그대로 준다"""
    spec = (res.get("parsed") or {}).get("dynamic_spec")
    if res.get("status") != "ok" or not spec:
        return
    try:
        found = await run_in_threadpool(plan_cache.key_of, parsed)
        if found:
            await plan_cache.save(found[0], found[1], tbl_id, forced, spec)
    except Exception:
        logger.exception("계산 방법 캐시 저장 실패")


async def _try(tbl_id, org_id, question, parsed, trace, forced):
    """표 하나로 계산해 본다 → (답 또는 None, 되물을 것 또는 None)"""
    step = {"tbl_id": tbl_id, **({"forced": forced} if forced else {})}
    trace["dynamic"].append(step)
    spec, why = await _build(tbl_id, org_id, question, parsed, step, forced)
    if spec is None:
        step["skip"] = str(why)
        return None, why if isinstance(why, Ask) else None
    step["cells"] = spec["cells"]
    res = await qa.answer(_with_spec(parsed, spec), trace, question)
    step["status"] = res["status"]
    if res["status"] == "ok" and not has_values(res.get("result") or {}):
        step["skip"] = "값이 없음 (비공개·결측)"
        return None, None
    if res["status"] in ("ok", "out_of_range"):
        return res, None  # result["method"] = "dynamic"은 qa.compute가 단다
    return None, None


def ask_answer(question, parsed, tbl_id, tbl_nm, ask: Ask, forced=None) -> dict:
    """칸 되묻기 응답 (ask_session의 되묻기와 같은 모양). context는 대화 상태에만 남고
    API 응답(QuestionClarification)에는 나가지 않는다"""
    names = [name for _, name in ask.options]
    prompt = f"'{tbl_nm}' 표에서 {ask.dim_name} 중 어느 것을 볼까요?"
    return {
        "status": "clarify_question",
        "answer": prompt,
        "parsed": parsed,
        "result": {"status": "clarify_question"},
        "sources": [],
        "chart": None,
        "trace": {},
        "question": question,
        "clarification": {
            "kind": "table_cell",
            "prompt": prompt,
            "options": names,
            "context": {
                "tbl_id": tbl_id,
                "tbl_nm": tbl_nm,
                "dim_no": ask.dim_no,
                "codes": {name: code for code, name in ask.options},
                "forced": {str(k): v for k, v in (forced or {}).items()},
                "parsed": parsed,
            },
        },
    }


ORDINAL = re.compile(r"^\s*(\d+)\s*번?\s*$")


def pick_option(reply: str, clarification: dict) -> str | None:
    """되묻기에 대한 답 → 고른 칸의 코드. 선택지 이름(공백 무시)이나 번호("2번")로 고른다"""
    codes = (clarification.get("context") or {}).get("codes") or {}
    names = clarification.get("options") or []
    if m := ORDINAL.match(reply):
        i = int(m[1]) - 1
        return codes.get(names[i]) if 0 <= i < len(names) else None
    key = _norm(reply)
    exact = [n for n in names if _norm(n) == key]
    if exact:
        return codes.get(exact[0])
    inside = [n for n in names if _norm(n) and _norm(n) in key]
    return codes.get(inside[0]) if len(inside) == 1 else None


def has_values(result: dict) -> bool:
    """계산 결과에 실제 값이 하나라도 있는가 (비공개 *·결측이면 값 칸이 비어 있다)"""
    for r in qa._leaves(result):
        if r.get("value") is not None or r.get("parts"):
            return True
        rows = [*(r.get("series") or []), *(r.get("values") or []), *(r.get("ranking") or [])]
        if any(x.get("value") is not None for x in rows):
            return True
    return False


async def _build(tbl_id, org_id, question, parsed, step, forced=None):
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced)
    except StatsTableMissing:
        pass
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"
    # 적재 안 된 표: 작은 표는 지금 받아 넣는다. 못 받으면 대기열에 넣고 이번에는 표 안내
    try:
        org_id = org_id or (await load_queue.table_info(tbl_id))["org_id"]
        outcome = await table_cache.fetch_now(tbl_id, org_id, step)
        if outcome != "loaded":
            await load_queue.enqueue(tbl_id, "question")
            return None, f"적재 안 됨 ({outcome}, 대기열에 넣음)"
    except Exception:
        logger.exception("동적 지표 표 받기 실패: %s", tbl_id)
        return None, "표를 받지 못함"
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced)
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"


def _with_spec(parsed: dict, spec: dict) -> dict:
    """질문 해석 결과에 지표 정의를 넣는다. 연도를 말하지 않았으면 그 표의 마지막 해"""
    q = {**parsed, "indicator": spec["name"], "unsupported": False, "dynamic_spec": spec}
    if spec["kind"] == "ratio" and q.get("type") == "composition":
        q["type"] = "value"  # "~ 중 ~ 비율"은 구성비가 아니라 비율 값 하나
    if not q.get("year") and q.get("type") in LAST_YEAR_KINDS:
        try:
            q["year"] = lookup._years(spec["sources"][0])[1]
        except Exception:
            logger.exception("동적 지표 수록 기간 확인 실패")
    return q
