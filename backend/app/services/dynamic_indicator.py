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
                 없으면 분류별 기본값 정책 (SAFE_TOTAL):
                   합계가 '전부'라는 뜻인 분류(성별·연령·국적 등) → 합계
                   그 밖의 분류 → 합계로 하되 답에 알린다 (말하지 않은 구분은 합계로 계산)
                   합계가 없음 → 추계 시나리오·가정은 중위, 나머지는 되묻거나(8개 이하) 표 안내
  연산
    값·비교·추이·순위  고른 칸의 값
    비율               "~ 중 ~ 비율": 질문에서 고른 칸 하나 ÷ 같은 분류의 합계 칸 × 100
    구성비             질문에 이름이 나온 분류(예: 연령별)의 칸마다 ÷ 합계
  검증 (하나라도 걸리면 None → 지금처럼 통계표 안내)
    - 장래추계 표인데 실적 자료가 있는 연도를 물음 (추계 표는 미래 연도에만)
    - 질문의 말과 맞은 칸이 하나도 없음 (표의 다른 값을 답하지 않도록)
    - 한 분류에서 칸이 둘 이상 맞음, 합계 칸이 없는데 고를 수 없음
    - 비율의 분자가 정해지지 않음 (나눌 부분이 없거나 여럿)
    - 분자가 분모의 부분이 아님: 표의 모든 지역·연도에서 분자 ≤ 분모인지 본다 (part_within_whole)
      비율, 연령 구간을 더한 값, 구성비(부분의 합 ≤ 합계)에 쓴다.
      겹치는 묶음 칸을 두 번 세는 것도 막는다
    - 계산은 됐지만 값이 하나도 없음 (비공개 *·결측) → 표 안내

답에는 계산식·쓴 표·칸과 "지표 사전에 없는 값을 표에서 바로 계산했다"는 주의를 붙인다.
"""

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from app.core.stats_db import StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import (
    alternative,
    cell_pick,
    indicators,
    load_queue,
    lookup,
    plan_cache,
    qa,
    search,
    table_cache,
)
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

# 말하지 않으면 알리지 않고 합계로 두는 분류 (합계가 '전부'라는 뜻이라 정의가 바뀌지 않는다).
# 여기 없는 분류(예: 특성별처럼 여러 기준이 섞인 분류)도 합계로 두지만 답에 그 사실을 알린다.
# 적재된 표 750개의 분류 이름으로 정했다 (2026-11-17). 바꾸면 팀과 맞춘다
SAFE_TOTAL = re.compile(
    r"(?<!특)성별|연령|각세|나이|국적|교육정도|혼인상태|가구원수|세대구성|거처|점유형태|사용방수|직업|산업|"
    r"경제활동|종사상지위|체류자격|체류기간|출산순위|자녀|전입지|전출지|현거주지|통근통학지|"
    r"거주외국인|외국인(아내|남편)"
)
NOTE = "지표 사전에 없는 값을 통계표에서 바로 계산했습니다. 공식 지표 정의와 다를 수 있습니다"
OTHER_TABLE_NOTE = "지표 사전의 표에 없는 값이라 같은 통계의 다른 통계표에서 바로 꺼냈습니다"
PROJECTION_NOTE = "장래추계 표의 추계값입니다(실적 아님). 다음 추계 때 바뀔 수 있습니다"
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총", "전국"}
SEX_NAMES = {"남": {"남자", "남", "남성"}, "여": {"여자", "여", "여성"}}
COUNT_UNITS = {"명", "가구", "건", "개", "세대", "호", "곳", "개소", "천명", "천가구"}
# 표가 쓰는 말과 같은 뜻의 일상어 (글자 맞추기 전에 바꾼다). 뜻이 하나로 정해진 말만 둔다
SAME_WORDS = {"출퇴근": "통근", "출근": "통근"}
TOTAL_SUFFIX = re.compile(r"(계|합계|소계|전체)$")  # 공백·기호를 뺀 이름 끝의 합계 표시
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
    # 한 분류 안의 여러 칸으로 하는 연산 (분류 번호, 코드들, "sum"·"compare"·"rank")
    multi: tuple[int, list[str], str] | None = None
    multi_dim: Dim | None = None  # 순위의 분류 (항목 안의 분류면 표 이름의 말로 부른다)


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
    for word, same in SAME_WORDS.items():  # 표가 쓰는 말로 ("출퇴근" → "통근")
        q = q.replace(word, same)
    spans = []
    for d in dims:
        if d.no in skip:
            continue
        for code, name in d.codes.items():
            n = _norm(name)
            if len(n) < 2 or n in TOTAL_NAMES:
                continue
            # "통학-계"·"통근·통학-계"처럼 뒤에 합계 표시가 붙은 이름은 떼고도 맞춘다 ("통학 인구")
            bare = TOTAL_SUFFIX.sub("", n)
            for word in {n, bare} if len(bare) >= 2 else {n}:
                spans += [(m.start(), m.end(), d.no, code) for m in re.finditer(re.escape(word), q)]
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
    forced: dict[int, str] = field(default_factory=dict)  # 이미 정한 다른 분류 (칸 고르기 LLM)

    def __str__(self) -> str:
        return f"{self.dim_name}에서 칸을 고르지 못함 (후보 {len(self.options)}개)"


MAX_ASK_OPTIONS = 8  # 이보다 칸이 많은 분류는 되묻지 않고 표 안내 (버튼이 너무 많아진다)


# 한 분류 안의 여러 칸으로 하는 연산을 고르는 말
SUM_WORDS = re.compile(r"합|합계|합친|더한|더하면|모두|전부|또는|이거나|(?<=[가-힣])(이)?나\s")
COMPARE_WORDS = re.compile(r"비교|차이|몇\s*배|보다|대비|vs", re.I)
RANK_PARTS = re.compile(r"top\s*\d|상위\s*\d|순위|(가장|제일)\s*(많|적|높|낮)|1위|몇\s*위", re.I)
REGION_RANK = re.compile(r"시도|시군구|지역|곳|어디|구별|도시|광역시")
SUBTOTAL = re.compile(r"(계|합계|소계)$")


def _multi_op(question: str) -> str | None:
    if COMPARE_WORDS.search(question):
        return "compare"
    if SUM_WORDS.search(question):
        return "sum"
    return None


def _leaf_codes(d: Dim) -> list[str]:
    """합계·소계("-계")를 뺀 세부 칸들"""
    return [c for c, n in d.codes.items() if c != d.total() and not SUBTOTAL.search(_norm(n))]


def _rank_dim(question: str, dims: list[Dim], region_dim: int | None, tbl_nm: str) -> Dim | None:
    """분류 안의 순위를 묻는가: 그 분류. 지역 순위("가장 많은 시도")는 아니다"""
    if not RANK_PARTS.search(question) or REGION_RANK.search(question):
        return None
    q = _norm(question)
    for d in dims:
        stem = _norm(re.sub(r"(별|구분)$", "", d.name))
        if d.no not in (0, region_dim) and len(stem) >= 2 and stem in q:
            return d
    # 표 이름의 "이용 교통수단별"처럼 분류 이름에 없는 '~별'이면 그 분류는 항목에 들어 있다
    names = [_norm(d.name) for d in dims]
    # "교통 수단별"처럼 띄어 쓴 두 낱말도 이어 붙여 본다 (긴 말부터)
    words = re.findall(r"([가-힣]+\s[가-힣]+)별", tbl_nm) + re.findall(r"([가-힣]{2,})별", tbl_nm)
    for word in words:
        w = _norm(word)
        if w in q and not any(w in n for n in names):
            item = next((d for d in dims if d.no == 0), None)
            # 항목 안의 분류는 표 이름의 말로 부른다 ("항목" 대신 "교통수단")
            return Dim(item.no, w, item.codes) if item else None
    return None


def choose(
    question: str,
    parsed: dict,
    dims: list[Dim],
    region_dim: int | None,
    forced: dict[int, str] | None = None,
    tbl_nm: str = "",
):
    """(Plan, None) 또는 (None, 고르지 못한 이유: 문자열 또는 Ask)
    forced: 사용자가 되묻기에서 고른 칸 {분류 번호: 코드}
    tbl_nm: 표 이름 (분류 안의 순위에서 '교통수단별'처럼 표 이름에만 있는 분류를 알아본다)"""
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
    ranked = _rank_dim(question, dims, region_dim, tbl_nm)
    if ranked is not None and len(leaves := _leaf_codes(ranked)) >= 2:
        # "교통수단 top3": 이 분류의 세부 칸끼리 순위 (지역 순위가 아니다)
        plan.multi, plan.multi_dim = (ranked.no, leaves, "rank"), ranked
        plan.pick[ranked.no] = plan.matched[ranked.no] = leaves[0]
    for d in dims:
        if d.no in skip or (plan.multi and d.no == plan.multi[0]):
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
        if len(codes) > 1 and d.total() and set(codes) == set(_leaf_codes(d)):
            # 세부 칸을 모두 말함 ("출퇴근·통학" = 통근 + 통학) → 그 분류의 합계
            plan.pick[d.no] = plan.matched[d.no] = d.total()
            continue
        op = _multi_op(question) if len(codes) > 1 and not plan.multi else None
        if op:
            # "걸어서나 자전거로"(합), "A와 B 비교"(비교): 되묻지 않고 그 칸들로 계산한다
            plan.multi = (d.no, codes, op)
            plan.pick[d.no] = plan.matched[d.no] = codes[0]
        elif len(codes) > 1:
            asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in codes]))
        elif codes:
            plan.pick[d.no] = plan.matched[d.no] = codes[0]
        elif d.total():
            plan.pick[d.no] = d.total()
            if not SAFE_TOTAL.search(_norm(d.name)):  # 합계의 뜻이 분명하지 않은 분류: 알린다
                plan.defaults.append(f"{d.name}: {d.codes[d.total()]}")
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


def llm_groups(question: str, dims: list[Dim], region_dim: int | None) -> list[cell_pick.Group]:
    """칸 고르기 LLM에 보낼 분류: 지역 · 연령 · 성별만 있는 분류는 코드가 고르므로 뺀다 (choose와
    같은 기준)"""
    out = []
    for d in dims:
        if d.no == region_dim:
            continue
        if d.no != 0 and d.is_age() and d.total():
            continue
        sex = set(d.sex_codes().values())
        if sex and not [c for c in d.codes if c not in sex and c != d.total()]:
            continue
        cells = list(d.codes.items())
        out.append(cell_pick.Group(d.no, d.name, cell_pick.narrow(question, cells), len(cells)))
    return out


# ---- 계산 계획 (지표 사전의 sources와 같은 모양) --------------------------------------


@dataclass
class BandMiss:
    """질문의 조건(예: 체중 3.2kg)이 표의 구간 분류(예: 2.5~3.0kg, 3.0~3.5kg)와 맞지 않음
    → 숫자를 내지 않고 '맞는 구간 없음 + 볼 수 있는 구간'으로 답한다 (band_answer)"""

    item: str
    dim_name: str
    labels: list[str]
    dim_no: int = -1

    def __str__(self) -> str:
        return f"'{self.item}'에 맞는 {self.dim_name} 구간 없음"


# 숫자+단위 조건 (3.2kg, 1년 반, 18개월). 연도(2025년)는 빼려고 숫자를 세 자리까지만 본다
MEASURE_UNIT = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d+)?\s*(kg|cm|개월|년(?:\s*반)?|g)(?!도)", re.I)


def _unit_norm(text: str) -> str:
    """ "1.5㎏미만" → "1.5kg미만" (표의 단위가 한 글자 기호일 때가 있다)"""
    # _norm은 소수점도 지우므로(3.2kg → 32kg) 여기서는 공백만 지운다
    return unicodedata.normalize("NFKC", re.sub(r"\s+", "", str(text))).lower()


def _band_miss(texts: list[str], dims: list[Dim], region_dim: int | None) -> BandMiss | None:
    """질문의 숫자+단위 조건이 같은 단위의 구간 분류가 있는 표에 들어왔는가 (그 구간은 칸이 아님)"""
    for text in texts:
        for m in MEASURE_UNIT.finditer(_unit_norm(text)):
            unit = re.sub(r"\s*반$", "", m[1].lower())
            for d in dims:
                if d.no == region_dim or d.is_age():
                    continue
                named = [n for c, n in d.codes.items() if c != d.total() and unit in _unit_norm(n)]
                if len(named) >= 2:
                    return BandMiss(m[0], d.name, named, d.no)
    return None


def uncovered(
    slots: dict | None,
    plan: Plan,
    dims: list[Dim],
    parsed: dict,
    region_dim: int | None = None,
) -> "str | BandMiss | None":
    """통계표 찾기가 질문에서 뽑은 조건 중 고른 칸에 반영되지 않은 것 (있으면 계산하지 않는다)

    칸 하나만 질문과 맞아도 나머지를 합계로 두면, 질문이 좁혀 물은 값 대신 넓은 값을 답하게 된다
    (예: "미혼 1인 가구"에서 미혼이 표에 없으면 전체 1인 가구). 그래서
      items    찾는 값(예: 미혼, 65세 이상)은 고른 칸 이름이나 연령·성별로 반영돼야 한다
      exclude  빼 달라는 대상이 있으면 합계 칸에 그 대상이 들어 있어 계산하지 않는다
      classes  나눠 보기(~별)를 물었는데 구성비가 아니면 합계 하나로 답하게 되어 계산하지 않는다
    """
    if not slots:
        return None
    cond = slots.get("conditions") or {}
    names = [_norm(d.codes[c]) for d in dims for no, c in plan.pick.items() if no == d.no]
    if plan.multi:  # 합·비교·순위하는 칸들도 반영된 것
        md = plan.multi_dim or next(d for d in dims if d.no == plan.multi[0])
        names += [_norm(md.codes[c]) for c in plan.multi[1]]
    # 칸 고르기 LLM이 고른 칸의 말로 바꿔 읽은 질문 낱말
    # (예: 결혼 → 혼인). 칸 이름에 반영된 것으로 본다
    read = [_norm(a) for a, _ in slots.get("read_as") or [] if len(_norm(a)) >= 2]
    for item in cond.get("items") or []:
        word = _norm(item)
        if not word:
            continue
        if any(word in n or n in word for n in names if len(n) >= 2):
            continue
        if any(word in r or r in word for r in read):
            continue
        is_age = lookup.parse_age(item) or qa.extract_ages(item)  # "62살 이상"·"67살 이하"도
        if is_age and parsed.get("ages") and plan.age_dim is not None:
            continue  # 연령 구간은 lookup이 연령 칸으로 고른다 (안 맞으면 band_mismatch)
        if any(word in w for w in ("남자", "여자", "남성", "여성")) and plan.sex_dim is not None:
            continue
        if miss := _band_miss([item], dims, region_dim):
            return miss
        return f"질문의 조건 '{item}'을 표의 칸에 반영할 수 없음"
    if slots.get("exclude"):
        return f"빼 달라는 대상({', '.join(slots['exclude'])})이 합계 칸에 들어 있음"
    if slots.get("check_target") and (target := _norm(slots.get("target") or "")):
        # 대상("유학생", "결혼이민자")을 물었는데 표 · 고른 칸 어디에도 없으면 전체 값을 답하게 된다
        # (넘기기 꾸러미로 계산할 때만 본다. slots["table_words"]: 표 이름 · 표의 대상)
        words = [*names, *(_norm(w) for w in slots.get("table_words") or [] if w), *read]
        if not any(target in w or (len(w) >= 2 and w in target) for w in words):
            return f"질문의 대상 '{slots['target']}'이 표나 고른 칸에 없음"
    # 성별, 그리고 질문에 연령 구간이 있을 때의 '연령'은 나눠 보기가 아니라 조건이다
    skip = {"성별"} | ({"연령", "나이"} if parsed.get("ages") else set())
    classes = [c for c in cond.get("classes") or [] if _norm(c) not in skip]
    if classes and parsed.get("type") not in ("composition", "rank", "trend"):
        return f"나눠 보기({', '.join(classes)}별)는 값 하나로 답할 수 없음"
    return None


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


def _without_age_words(term: str) -> str:
    """'30대 미혼 인구' → '미혼 인구' (연령은 lookup이 이름 앞에 붙인다: '30대 미혼 인구')"""
    for start, end, _ in sorted(qa.age_spans(term), reverse=True):
        # "62살에서 67살 사이"의 '사이', "33살부터 38살까지"의 '까지'도 연령 표현이다
        rest = re.sub(r"^\s*(사이|까지|의)", "", term[end:])
        term = term[:start] + rest
    return " ".join(term.split())


def _name(parsed: dict, dims: list[Dim], plan: Plan, counted: bool, strip_age: bool = False) -> str:
    """지표 이름: 질문 해석의 지표 표현이 고른 칸 이름을 모두 담고 있으면 그 표현, 아니면 칸 이름
    (HCX가 지표 칸에 "2020년 서울"처럼 지표가 아닌 말을 넣는 경우가 있다)
    strip_age: 연령을 lookup이 연령 칸으로 고르면 이름에서 연령 표현을 뺀다 (두 번 붙지 않게)"""
    words = [d.codes[plan.matched[d.no]] for d in dims if d.no in plan.matched]
    words = words or [dims[0].codes[plan.pick[0]]]
    # "통학-계"처럼 이름 끝의 합계 표시는 떼고 쓴다 ("통학 인구"와 맞추고, 답에도 "통학")
    words = [re.sub(r"[\s\-·]*(계|합계|소계)$", "", w) or w for w in words]
    words = [
        w for w in words if w not in TOTAL_NAMES
    ] or words  # 합계 칸("계")은 이름에 쓰지 않는다
    for term in (parsed.get("term"), parsed.get("indicator")):
        # 사전 지표 이름도 고른 칸 이름을 모두 담으면 쓴다 (옛 연도: 이혼 ⊂ 이혼 건수).
        # 근거 없이 고른 사전 지표(총인구 ⊅ 1인가구)는 아래 담김 검사에서 걸러진다
        if not term or re.search(r"\d{4}", str(term)):
            continue
        if indicators.find_regions(str(term)):  # "전국 평균 가구원 수"처럼 지역까지 담은 표현
            continue
        if all(_norm(w) in _norm(term) for w in words):
            return _without_age_words(str(term)) if strip_age else str(term)
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
    tbl_id: str,
    question: str,
    parsed: dict,
    forced: dict[int, str] | None = None,
    slots: dict | None = None,
) -> tuple[dict | None, "str | Ask | None"]:
    """(지표 정의, None) 또는 (None, 이유 또는 Ask). 적재 안 된 표면 StatsTableMissing
    forced: 되묻기에서 고른 칸 {분류 번호: 코드}
    slots: 통계표 찾기가 질문에서 뽑은 조건 (found["parsed"]). 칸에 모두 반영됐는지 본다"""
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
    try:
        tbl_nm = lookup._table(tbl_id)["tbl_nm"]
    except Exception:  # 표 이름은 분류 안 순위에서만 쓴다
        tbl_nm = ""
    plan, why = choose(question, parsed, dims, region_dim, forced, tbl_nm)
    if plan is None:
        # 칸을 고르지 못했어도 질문의 숫자 조건(체중 3.2kg 등)이 이 표의 구간 분류에 해당하면
        # 그 조건에 맞는 구간이 없다고 답한다 (표 안내보다 구체적)
        items = list(((slots or {}).get("conditions") or {}).get("items") or [])
        if not isinstance(why, Ask) and (miss := _band_miss([*items, question], dims, region_dim)):
            return None, miss
        return None, why
    if ignore_ages:
        # 연령 말이 고른 칸 이름 안에 있을 때만 지표 이름의 일부로 본다 (노년 ⊂ 노년부양비).
        # 아니면 질문의 연령 조건을 버리게 된다 ("노인 1인 가구" → 전 연령 1인 가구) → 계산하지 않음
        expr = _norm((parsed.get("age_group") or {}).get("expr") or "")
        names = [_norm(d.codes[plan.matched[d.no]]) for d in dims if d.no in plan.matched]
        if not expr or not any(expr in n for n in names):
            return None, "질문의 연령 조건을 표의 칸에 반영할 수 없음"
    if why := uncovered(slots, plan, dims, parsed, region_dim):
        return None, why
    # 통계표 찾기가 조건으로 뽑지 않았어도, 질문의 숫자 조건(3.2kg 등)이 이 표의 구간 분류에
    # 해당하는데 그 분류를 질문의 말로 고르지 못했으면(합계로 둠) 조건을 버리게 된다
    # → 맞는 구간 없음
    miss = _band_miss([question], dims, region_dim)
    if miss and miss.dim_no not in plan.matched:
        return None, miss
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
    strip_age = bool(
        plan.age_dim
        and parsed.get("ages")
        and parsed.get("type") != "composition"
        and not PCT_ASK.search(question)
    )
    name = _name(parsed, dims, plan, counted, strip_age)
    spec = {
        "name": name,
        "unit": unit,
        "kind": "count" if counted else "value",
        "method": "lookup",
        "dynamic": True,
        # 사전 지표 이름으로 답하면 사전 표에 없는 연도 등을 다른 표에서 꺼낸 것이다 (옛 연도)
        "note": OTHER_TABLE_NOTE if indicators.canonical_name(name) else NOTE,
        "sources": [src],
    }
    kind = parsed.get("type")
    if plan.multi:
        return _parts_spec(spec, src, plan, dims, tbl_nm)
    if kind == "composition" or PCT_ASK.search(question):
        made = _composition(spec, src, plan, dims, question) if kind == "composition" else None
        if made is None:
            made, why = _ratio(spec, src, plan, dims, parsed, tbl_nm)
            if made is None:
                return None, why
        spec = made
    else:
        if plan.sex_dim is not None:
            src["by_sex"] = {s: _sex_override(plan, s) for s in ("계", "남", "여")}
        if plan.age_dim is not None and parsed.get("ages") and not ignore_ages:
            # 연령 구간을 더해 답한다: 더한 값이 연령 합계를 넘지 않는지 (겹치는 묶음 칸)
            try:
                codes = lookup._age_codes(src, tuple(parsed["ages"][0]))
            except lookup.AgeMismatch:
                codes = None  # lookup이 계산할 때 '그 연령 구간 자료 없음'으로 답한다
            if codes and (
                why := part_within_whole(src, src["age_dim"] + "_id", codes, src["age_total"])
            ):
                return None, why
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
        spec["note"] += (
            f". 말하지 않은 구분은 합계·기본값으로 계산했습니다 ({', '.join(plan.defaults)})"
        )
    if ignore_ages:
        spec["ignore_ages"] = True  # qa.compute가 질문의 연령을 넘기지 않는다
    if not spec.get("formula") and (cells := _cells(plan, dims)):
        spec["formula"] = f"KOSIS 공표 값 ({cells})"  # 칸이 하나뿐이면 lookup이 표 이름을 쓴다
    spec["cells"] = _cells(plan, dims)
    return spec, None


def _parts_spec(spec: dict, src: dict, plan: Plan, dims: list[Dim], tbl_nm: str):
    """한 분류 안의 칸들로 합·비교·순위 (parts_answer가 계산한다)
    → (정의, None) 또는 (None, 이유)"""
    dim_no, codes, op = plan.multi
    d = plan.multi_dim or next(x for x in dims if x.no == dim_no)
    if op == "sum" and d.total() and (why := part_within_whole(src, d.col, codes, d.total())):
        return None, why  # 겹치는 칸(묶음과 세부)을 더하면 두 번 센다
    if plan.sex_dim is not None and d is not plan.sex_dim:
        src["by_sex"] = {s: _sex_override(plan, s) for s in ("계", "남", "여")}
    # 지표 이름: 연산하는 분류 말고 질문에서 고른 칸, 없으면 표 이름의 주제 ("통근 통학 인구")
    words = [
        x.codes[plan.matched[x.no]]
        for x in dims
        if x.no in plan.matched and x.no != dim_no and plan.matched[x.no] != x.total()
    ]
    subject = " ".join(words) or re.sub(r"\(.*$|-[^-]*$", "", tbl_nm.split("별")[-1]).strip()
    if not words and any(_norm(subject) in _norm(d.codes[c]) for c in codes):
        # 표 이름에서 뗀 말이 연산하는 칸의 일부면 주제가 아니다 ("상주(야간) · 주간 인구"의 "상주")
        subject = ""
    spec.update(
        name=subject or spec["name"],
        parts_op={"op": op, "col": d.col, "dim": d.name, "codes": codes, "subject": subject,
                  "labels": [d.codes[c] for c in codes]},
        fixed={"type": "parts"},
        cells=f"{d.name} {', '.join(d.codes[c] for c in codes[:5])}",
    )  # fmt: skip
    return spec, None


def _item_whole(d: Dim, tbl_nm: str) -> str | None:
    """합계 칸이 없는 항목에서 전체를 뜻하는 칸: 이름이 표 이름에 그대로 든 첫 칸
    ("통근·통학 인구(12세 이상)" ⊂ "… 이용 교통수단별 통근 통학 인구(12세 이상)").
    분자 ≤ 분모 검증(part_within_whole)은 그대로 거친다"""
    if d.no != 0 or not tbl_nm:
        return None
    # 첫 칸만 본다 (KOSIS는 전체를 맨 앞에 둔다). "상주(야간) · 주간 인구" 표의 "주간 인구"처럼
    # 표 이름에 든 세부 칸을 전체로 보지 않게
    code, name = next(iter(d.codes.items()), (None, ""))
    return code if len(_norm(name)) >= 4 and _norm(name) in _norm(tbl_nm) else None


def _ratio(spec: dict, src: dict, plan: Plan, dims: list[Dim], parsed: dict, tbl_nm: str = ""):
    """'~ 중 ~ 비율': 나눌 부분 하나 ÷ 같은 분류의 합계 × 100 → (정의, None) 또는 (None, 이유)"""
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
            return None, "질문한 연령 구간을 표의 연령 칸으로 만들 수 없음"
        parts.append((d.col, codes, d.total(), indicators.age_label(band)))
    for d in dims:
        whole = d.total() or _item_whole(d, tbl_nm)
        if d.no in plan.matched and whole and plan.matched[d.no] != whole:
            if d is not plan.sex_dim:
                code = plan.matched[d.no]
                if indicators.find_regions(d.codes[code]):
                    # 질문의 지역 이름이 지역 분류가 아닌 칸에 걸렸다
                    # (예: 통근통학지별 '서울특별시'). 그 칸 ÷ 합계(전국)는 질문이 묻는 비율이
                    # 아니다 ("1인 가구 중 통근 비율" → 서울로 통근 ÷ 전국 88%가 나왔다)
                    continue
                parts.append((d.col, [code], whole, d.codes[code]))
    if len(parts) != 1:
        return None, "비율의 분자·분모를 정하지 못함"
    col, num, den, part = parts[0]
    if why := part_within_whole(src, col, num, den):
        return None, why
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
    return out, None


CHECKS = MemoryCache(
    "dynamic_checks", maxsize=1024, ttl=86400
)  # 표가 다시 적재돼도 하루면 다시 본다


def part_within_whole(src: dict, col: str, num: list[str], den: str) -> str | None:
    """분자 칸(들)이 분모 칸의 부분인가: 표의 모든 지역·연도에서 분자 합 ≤ 분모.
    맞으면 None, 아니면 이유

    같은 표·같은 분류라도 칸이 서로 포함 관계가 아닐 수 있다 (예: 묶음 칸과 세부 칸이 함께 있는
    분류를 더하면 두 번 센다, 분모로 고른 칸이 합계가 아니다). 그러면 비율이 100%를 넘거나
    틀린 값이 된다.
    나머지 분류는 정의와 같이 고정한다 (연령을 따로 고르면 연령 합계)"""
    fixed = {k: v for k, v in (src.get("filters") or {}).items() if k != col}
    if src.get("age_dim") and f"{src['age_dim']}_id" != col:
        fixed[f"{src['age_dim']}_id"] = src["age_total"]
    item = None if col == "item_id" else src["item_id"]
    key = (src["table"], col, tuple(num), den, tuple(sorted(fixed.items(), key=str)), str(item))
    cached = CHECKS.get(key)
    if cached is not MISSING:
        return cached
    pairs = [
        (n, d)
        for n, d in _part_rows(src["table"], col, num, den, fixed, item)
        if n is not None and d is not None
    ]
    if not pairs:
        why = "분자·분모 값이 함께 있는 칸이 없음"
    else:
        bad = sum(1 for n, d in pairs if n > d + max(0.5, abs(d) * 1e-6))
        why = (
            f"분자가 분모보다 큰 지역·연도가 {bad}곳 (분자·분모 관계가 맞지 않음)" if bad else None
        )
    CHECKS.set(key, why)
    return why


def _part_rows(tbl_id, col, num, den, fixed, item) -> list[tuple[float | None, float | None]]:
    """지역·연도마다 (분자 합, 분모). 지역 분류가 없는 표는 연도마다"""
    dim_no = lookup._dim_no(col)
    column = "v.item_key" if dim_no == 0 else f"v.c{dim_no}"
    known = lookup._codes(tbl_id, dim_no)
    num_keys = [known[c][0] for c in num if c in known]
    if not num_keys or den not in known:
        return []
    conds, params = [], []
    if item is not None:
        cond, keys = lookup._in("v.item_key", tbl_id, 0, [item])
        conds.append(cond)
        params += keys
    for fcol, val in fixed.items():
        n = lookup._dim_no(fcol)
        cond, keys = lookup._in(
            "v.item_key" if n == 0 else f"v.c{n}",
            tbl_id,
            n,
            val if isinstance(val, list) else [val],
        )
        conds.append(cond)
        params += keys
    region = lookup._region_dim(tbl_id)
    group = f"v.period, v.c{region}" if region else "v.period"
    marks = ", ".join("?" for _ in num_keys)
    rows = _rows(
        f"select sum(v.value) filter (where {column} in ({marks}))::float8 as num,"
        f" sum(v.value) filter (where {column} = ?)::float8 as den"
        f" from stat_value v where v.tbl_key = ? and v.{lookup.YEARLY}"
        + "".join(f" and {c}" for c in conds)
        + f" group by {group}",
        [*num_keys, known[den][0], lookup._tbl_key(tbl_id), *params],
    )
    return [(r["num"], r["den"]) for r in rows]


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
            rest = [c for c in d.codes if c != d.total()]
            if part_within_whole(src, d.col, rest, d.total()):
                return None  # 칸 안에 묶음(소계)이 섞여 부분의 합이 합계를 넘는다
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


MAX_TABLES = 3  # 통계표 찾기 후보 중 이만큼까지 (순위대로) 시도한다


async def answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    """통계표 찾기가 고른 표로 바로 계산한 답 (qa.answer와 같은 모양). 못 하면 None → 표 안내

    통계표 찾기가 조건을 충족한 표를 찾았을 때만(status ok, 함께 볼 표 없음) 계산한다.
      - 후보는 순위대로 MAX_TABLES개까지 보되, 질문 조건(연도·지역 수준·성별·분류 등)을 하나라도
        충족하지 못한(✘) 표는 건너뛴다. 정답 표가 1순위가 아닐 수 있어 2·3순위도 보지만,
        통계표 찾기가 덜 맞다고 본 표로 숫자를 내지는 않는다
      - 일부 조건만 맞는 경우(partial_match)나 한 통계가 여러 표로 나뉜 경우(together)는 표 안내
      - 통계표 찾기의 주의(판 메모·기준)는 답의 주의에 붙인다
    어느 표로도 계산하지 못했는데 앞 표에서 칸 하나만 정하지 못했으면(예: 분류에 합계가 없음)
    그 칸을 되묻는다 (status clarify_question).
    적재 안 된 작은 표는 바로 받아 넣는다 (table_cache.fetch_now).
    시도한 표와 결과는 trace["dynamic"]에 남긴다 (요청 기록)."""
    recs = (found.get("recommendations") or [])[:MAX_TABLES]
    if found.get("together"):
        return None  # 한 통계가 여러 표로 나뉜 경우는 통계표 찾기 답을 그대로 쓴다
    if found.get("status") != "ok":
        # 일부 일치(partial_match)도 어긋난 조건이 연령뿐이면 계산해 본다 (_age_check).
        # 그 밖은 표 안내
        misses = [[k for k, ok in (c.get("checks") or {}).items() if not ok] for c in recs]
        if found.get("status") != "partial_match" or not any(
            m and all(_age_check(k, parsed) for k in m) for m in misses
        ):
            return None
    steps = trace.setdefault("dynamic", [])
    slots, first_ask = found.get("parsed") or {}, None
    for card in recs:
        tbl_id = card.get("tbl_id")
        if not tbl_id:
            continue
        checks = (card.get("checks") or {}).items()
        failed = [k for k, ok in checks if not ok and not _age_check(k, parsed)]
        if failed:
            steps.append(
                {"tbl_id": tbl_id, "skip": f"통계표 찾기 조건 불충족 ({', '.join(failed)})"}
            )
            continue
        res, ask = await _try(
            tbl_id, card.get("org_id"), question, parsed, trace, {}, slots, found.get("notice")
        )
        if res is not None:
            await _remember(parsed, tbl_id, {}, res)
            return res
        if ask is not None and first_ask is None:
            first_ask = (card, ask)
    if first_ask is None:
        return None
    card, ask = first_ask
    return ask_answer(question, parsed, card["tbl_id"], card.get("tbl_nm") or card["tbl_id"], ask)


def _age_check(key: str, parsed: dict) -> bool:
    """통계표 찾기의 조건이 연령 구간인가 (질문에 연령 구간이 있을 때만).
    통계표 찾기는 "62살에서 67살 사이"를 '62살 이상'·'67살 이하' 칸이나 '연령별'로 찾다가 없으면
    ✘로 둔다. 연령 구간은 lookup이 표의 연령 칸으로 직접 더하고, 맞는 칸이 없으면
    맞는 구간 없음(band_mismatch)으로 볼 수 있는 구간을 안내하므로 바로 계산이 맡는다"""
    if not parsed.get("ages"):
        return False
    word = key.split(":", 1)[1] if ":" in key else key
    return "연령" in word or "나이" in word or bool(qa.extract_ages(word) or lookup.parse_age(word))


# ---- 넘기기 꾸러미로 계산 (계약_초안 5-2 ~ 5-5,
# 2026-11-17) ------------------------------
# 통계표 찾기가 넘긴 꾸러미(found["handoff"])의 후보를 동시에 계산해 보고 우선순위로 고른다.
#   시도 목록  고른 표 → (물은 연도가 없으면) 그 표의 다른 판 → 나머지 후보. 최대 5개
# 건너뜀     ✘가 있는 표 / 고른 표가 아닌데 확인한 조건이 없음 / 검토가 고른 표를 '모두 충족'으로 봄
#   계산 조건  탐색 해석(Gemma)이 먼저, 빈 칸은 수치 해석(HCX). 질문 종류는 수치 해석
#   고르기     계산된 표 중 목록 순서가 가장 앞인 것 (동시에 계산해도 차례로 할 때와 같은 결과)
# 꾸러미가 없거나 지표가 여럿이면 예전 answer()로 처리한다.

MAX_ATTEMPTS = 5
MAX_FETCH_AT_ONCE = 3  # 적재 안 된 표를 KOSIS에서 동시에 받는 수
HANDOFF_STATUSES = ("ok", "partial_match")
WHOLE_COUNTRY = {"전국", "우리나라", "한국", "대한민국", "국내"}


def to_parsed(understanding: dict, parsed: dict) -> tuple[dict, dict]:
    """탐색 해석 → 계산용 질문 해석 (qa.understand와 같은 모양). 탐색 해석에 없는 칸은 수치 해석.
    → (새 해석, 둘이 달랐던 칸 {칸: [수치 해석, 탐색 해석]}).
    지역 이름을 DB로 풀므로 스레드에서 부른다"""
    cond = understanding.get("conditions") or {}
    q = dict(parsed)
    diff: dict = {}

    def put(key, value):
        before = parsed.get(key)
        if before not in (None, [], "") and before != value:
            diff[key] = [before, value]
        q[key] = value

    kind = parsed.get("type")
    if kind in (None, "other", "parse_error"):
        # 수치 해석이 질문 종류를 못 정했다 (예전에는 이 경우 계산이 늘 '지원 안 함'으로 끝났다)
        kind = q["type"] = "trend" if cond.get("year_end") else "value"
    region = cond.get("region")
    if region and len(parsed.get("regions") or []) <= 1:  # 탐색은 지역을 하나만 담는다
        if _norm(region) in WHOLE_COUNTRY:
            put("regions", ["전국"])
        else:
            found = indicators.resolve_region(region)
            if found.get("status") == "ok":
                put("regions", [found["display"]])
    year, year_end = cond.get("year"), cond.get("year_end")
    if year is not None:
        if kind == "trend":
            if year_end:
                put("start_year", year)
                put("end_year", year_end)
            elif (understanding.get("time_ref") or {}).get("open_end"):
                put("start_year", year)  # '최근 5년', '2020년 이후'
        else:
            put("year", year_end or year)
    if cond.get("sex") in ("남", "여"):
        put("sex", cond["sex"])
    age = understanding.get("age")
    if age and age.get("lo") is not None:
        put("ages", [[age["lo"], age.get("hi")]])
        if age.get("basis"):
            q["age_group"] = {
                "expr": age.get("expr"),
                "range": indicators.age_label((age["lo"], age.get("hi"))),
                "basis": age["basis"],
            }
    if not parsed.get("level") and cond.get("region_level") in ("시도", "시군구"):
        q["level"] = cond["region_level"]
    return q, diff


def without_regions(words: list[str]) -> list[str]:
    """제외 대상에서 지역 이름을 뺀다 ("서울이랑 경기 말고 시도별": 지역은 지역 칸이 고른다.
    합계 칸에 그 대상이 섞였다는 검사(uncovered)에 넣으면 맞는 표를 막는다). DB를 읽어 스레드에서"""
    return [w for w in words if not indicators.find_regions(w)]


def attempt_list(target: dict, q: dict) -> list[dict]:
    """시도할 표 목록 (순서 = 우선순위). 항목마다 kind: picked / edition / other"""
    cands = target.get("candidates") or []
    picked = next((c for c in cands if c.get("picked")), None)
    years = [y for y in (q.get("year"), q.get("start_year"), q.get("end_year")) if y]
    out: list[dict] = []

    def add(entry: dict) -> None:
        if entry["tbl_id"] not in {e["tbl_id"] for e in out}:
            out.append(entry)

    def covers(e: dict) -> bool:
        lo, hi = e.get("start_year"), e.get("end_year")
        return lo is not None and hi is not None and lo <= min(years) and max(years) <= hi

    if picked:
        add({**picked, "kind": "picked"})
        if years and not covers(picked):
            # 고른 표에 물은 연도가 없으면 같은 대상 · 성별의
            # 다른 판으로. 연도 조건만 다르므로 연도 ✘는 뺀다
            checks = {k: v for k, v in picked["checks"].items() if k not in ("year", "period")}
            for e in picked.get("editions") or []:
                if covers(e):
                    add({**picked, **e, "checks": checks, "editions": [], "kind": "edition"})
    for c in cands:
        if c is not picked:
            add({**c, "kind": "other"})
    return out[:MAX_ATTEMPTS]


def skip_reason(
    entry: dict, picked_exact: bool, parsed: dict | None = None, multi: bool = False
) -> str | None:
    """바로 계산하지 않는 이유 (계약 5-2의 3번). None이면 계산한다.
    연령 구간의 ✘는 건너뛰지 않는다 (_age_check: lookup이 연령 칸으로 고르거나
    맞는 구간 없음으로 안내한다).
    multi: 지표가 여럿인 질문의 지표 하나. 지표마다 검토(review)를 하지 않아 picked가 거의 없고
    확인한 조건도 비어 있을 수 있어 ✘ 후보 건너뛰기만 쓴다 (후보를 다 계산해도 결과는 같다)"""
    failed = [
        k
        for k, ok in (entry.get("checks") or {}).items()
        if not ok and not _age_check(k, parsed or {})
    ]
    if failed:
        return f"통계표 찾기 조건 불충족 ({', '.join(failed)})"
    if multi:
        return None
    if entry["kind"] == "other":
        if not entry.get("checks"):
            return "고른 표가 아닌데 확인한 조건이 없음"
        if picked_exact:
            return "검토가 고른 표를 '모두 충족'으로 봄"
    return None


def handoff_notice(entry: dict, understanding: dict, combine: dict, found_notice) -> str | None:
    """답의 주의에 붙일 것: 표의 주석, 연령 이름의 기준, 답하지 못한 범위 밖 지표"""
    lines = [found_notice] if entry["kind"] == "picked" and found_notice else []
    lines += list(entry.get("notes") or [])
    age = understanding.get("age") or {}
    if age.get("basis") and age.get("lo") is not None:
        label = indicators.age_label((age["lo"], age.get("hi")))
        lines.append(f"'{age.get('expr')}'은(는) {label}으로 계산했습니다 ({age['basis']})")
    if oos := combine.get("out_of_scope"):
        lines.append(f"{', '.join(oos)}은(는) 인구 통계가 아니라 답하지 못했습니다")
    return ". ".join(line.rstrip(". ") for line in lines if line) or None


async def compute_from_handoff(
    question: str, parsed: dict, found: dict, trace: dict
) -> dict | None:
    """넘기기 꾸러미의 후보로 바로 계산한 답 (answer와 같은 자리, 같은 모양).
    못 하면 None → 표 안내"""
    h = found.get("handoff")
    if not h:
        return await answer(question, parsed, found, trace)
    trace.setdefault("dynamic", [])
    if h.get("status") not in HANDOFF_STATUSES or not h.get("targets"):
        return None
    if len(h["targets"]) > 1:
        return await multi_answer(question, parsed, found, trace)  # 지표가 여럿 (구현 5)
    res = await _compute_target(question, parsed, found, h["targets"][0], trace)
    from app.services import multi_indicator

    if res and multi_indicator.TOP_REGION_OF.search(question):
        # "1인 가구가 가장 많은 시도의 통근 인구"를 통계표 찾기가 지표 하나로 읽었다.
        # 거르는 지표 없이 계산하면 1위 지역이 아니라 전국 값이 나온다 → 숫자 대신 표 안내
        if "value" in (res.get("result") or {}) and "ranking" not in (res.get("result") or {}):
            trace["dynamic"].append({"skip": "'가장 많은 지역의 값'인데 거르는 지표가 없음"})
            return None
    return res


async def multi_answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    """지표가 여럿인 질문 (구현 5): 지표마다 계산해 combine.op대로 합친다 (multi_indicator.py)"""
    from app.services import multi_indicator

    return await multi_indicator.answer(question, parsed, found, trace)


def _target_understanding(u: dict, target: dict) -> dict:
    """지표 하나의 해석: 질문 전체 해석에 계획의 지표별 조건
    (연도 · 지역 수준 · 성별 · 연령)을 덮는다.
    항목 · 분류는 계획에 없어 비운다 (질문 전체의 항목이 다른 지표 칸 검사를 막지 않게)"""
    cond = dict(u.get("conditions") or {})
    own = target.get("conditions") or {}
    for key in ("region", "region_level", "year", "year_end", "sex"):
        if own.get(key) not in (None, "", []):
            cond[key] = own[key]
    cond["items"], cond["classes"] = [], []
    out = {**u, "conditions": cond}
    if text := own.get("age_text"):
        bands = qa.extract_ages(str(text))
        if bands:
            lo, hi = bands[0]
            out["age"] = {"expr": str(text), "lo": lo, "hi": hi, "basis": None}
    return out


async def _compute_target(
    question: str, parsed: dict, found: dict, target: dict, trace: dict, multi: bool = False
) -> dict | None:
    """꾸러미의 지표 하나(target)를 후보 표로 계산한 답. 못 하면 None.
    multi: 지표가 여럿인 질문의 지표 하나 (지표별 질문 · 조건으로 계산하고,
    대안 제안 LLM은 부르지 않는다)"""
    h = found["handoff"]
    trace.setdefault("dynamic", [])
    if target.get("together"):
        trace["dynamic"].append({"skip": "한 통계가 여러 표로 나뉨 (함께 볼 표)"})
        return None
    u = h.get("understanding") or {}
    if multi:
        u = _target_understanding(u, target)
    try:
        q, diff = await run_in_threadpool(to_parsed, u, parsed)
    except Exception:
        logger.exception("탐색 해석 바꿔 담기 실패 → 수치 해석으로 계산")
        q, diff = dict(parsed), {}
    trace["handoff"] = {"parsed_from": "search", "mismatch": diff}
    try:
        exclude = await run_in_threadpool(without_regions, list(u.get("exclude") or []))
    except Exception:
        logger.exception("제외 대상의 지역 이름 확인 실패 → 제외 대상 그대로")
        exclude = list(u.get("exclude") or [])
    entries = attempt_list(target, q)
    picked = next((e for e in entries if e["kind"] == "picked"), None)
    picked_exact = bool(picked and (picked.get("review") or {}).get("match") == "exact")
    sem = asyncio.Semaphore(MAX_FETCH_AT_ONCE)

    async def compute(entry: dict, q_used: dict, sub: dict):
        """표 하나를 해석 q_used로 계산 → (답, 되물을 것). 기록은 sub에"""
        slots = {
            "conditions": u.get("conditions") or {},
            "exclude": exclude,
            "target": u.get("target"),
            # 대상 낱말 검사: 검토(LLM)가 이 표를 '모두 충족'으로 봤으면 하지 않는다 (가, 11/18).
            # 탐색 쪽 trust_exact(48a7e37)와 같은 기준. 질문은 "해외 유학생", 표는 "유학생 현황"처럼
            # 낱말이 글자로 겹치지 않아도 뜻으로 맞는 표를 막았다
            "check_target": (entry.get("review") or {}).get("match") != "exact",
            "table_words": [entry.get("tbl_nm"), entry.get("target")],
        }
        notice = handoff_notice(entry, u, h.get("combine") or {}, found.get("notice"))
        res, ask = await _try(
            entry["tbl_id"], entry.get("org_id"), question, q_used, sub, {}, slots, notice, sem
        )
        if res is None and ask is None and sub["dynamic"]:
            if cell_pick.needs_llm(sub["dynamic"][-1].get("skip")):
                res, ask = await with_cell_llm(entry, q_used, sub, slots, notice)
        if sub["dynamic"]:
            sub["dynamic"][0]["kind"] = entry["kind"]
        return res, ask

    async def with_cell_llm(entry: dict, q_used: dict, sub: dict, slots: dict, notice):
        """규칙이 칸을 못 고른 표: LLM이 고른 칸(forced)으로 다시 계산한다 (cell_pick.py)"""
        tbl_id = entry["tbl_id"]
        try:
            dims, region_dim, _ = await run_in_threadpool(schema, tbl_id)
            groups = llm_groups(question, dims, region_dim)
        except Exception:
            logger.exception("칸 고르기 LLM에 보낼 칸 만들기 실패: %s", tbl_id)
            return None, None
        picked = await cell_pick.choose(
            question, u, entry.get("tbl_nm"), groups, sub["dynamic"][-1]
        )
        if picked is None:
            return None, None
        if picked.unsure:
            g = picked.unsure[0]
            codes = next((d.codes for d in dims if d.no == g.no), {})
            if len(picked.unsure) == 1 and 1 < len(codes) <= MAX_ASK_OPTIONS:
                # 질문만으로 정할 수 없는 분류 하나: 그 분류만
                # 사용자에게 묻는다 (다른 분류는 LLM이 정한 칸)
                return None, Ask(g.no, g.name, list(codes.items()), dict(picked.forced))
            return None, None
        # 칸 고르기 LLM이 "이 표가 질문의 값(대상 포함)을
        # 담고 있다"고 답했으므로 대상 낱말 검사는 하지 않는다
        # (나, 11/18. 프롬프트: "질문이 묻는 대상이나 값이 표에 없으면 아니오")
        slots_llm = {**slots, "read_as": picked.read_as, "check_target": False}
        lines = [n for n in (notice, picked.notice()) if n]
        res, ask = await _try(
            tbl_id,
            entry.get("org_id"),
            question,
            q_used,
            sub,
            picked.forced,
            slots_llm,
            ". ".join(lines) or None,
            sem,
        )
        sub["dynamic"][-1]["by"] = "cell_llm"
        sub["dynamic"][-1]["read_as"] = [list(p) for p in picked.read_as]  # 캐시에 근거로 남긴다
        return res, ask

    async def attempt(entry: dict):
        sub: dict = {"dynamic": []}
        tbl_id = entry["tbl_id"]
        if why := skip_reason(entry, picked_exact, q, multi):
            sub["dynamic"].append({"tbl_id": tbl_id, "kind": entry["kind"], "skip": why})
            return None, None, sub
        if u.get("projection"):
            try:
                projection = await run_in_threadpool(is_projection, tbl_id)
            except Exception:
                projection = True  # 모르면 막지 않는다 (build가 연도로 다시 본다)
            if not projection:
                sub["dynamic"].append(
                    {"tbl_id": tbl_id, "skip": "장래 추계를 물었는데 추계 표가 아님"}
                )
                return None, None, sub
        res, ask = await compute(entry, q, sub)
        return res, ask, sub

    def merge(sub: dict) -> None:  # 표마다 따로 남긴 기록을 순서대로 합친다
        trace["dynamic"].extend(sub["dynamic"])
        if sub.get("fetched"):
            trace.setdefault("fetched", []).extend(sub["fetched"])

    outcomes = await asyncio.gather(*(attempt(e) for e in entries))
    for _, _, sub in outcomes:
        merge(sub)
    trace["handoff"]["tried"] = len(entries)
    for entry, (res, _, sub) in zip(entries, outcomes, strict=True):
        if res is not None and res.get("status") == "ok":
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
            # 칸 고르기 LLM이 고른 칸은 사람의 선택이 아니라 표의 칸 풀이라 캐시에 함께 남긴다
            # (다음에 같은 질문은 LLM 없이 그 칸으로 계산)
            last = sub["dynamic"][-1] if sub["dynamic"] else {}
            by_llm = last.get("by") == "cell_llm"
            forced = (last.get("forced") or {}) if by_llm else {}
            words = (last.get("read_as") or []) if by_llm else []
            await _remember(parsed, entry["tbl_id"], forced, res, words)
            return res
    for entry, (_, ask, _) in zip(entries, outcomes, strict=True):
        if ask is not None:  # 맞는 표에서 칸 하나만 못 정함: 그 칸을 묻는다
            name = entry.get("tbl_nm") or entry["tbl_id"]
            return ask_answer(question, q, entry["tbl_id"], name, ask)

    if multi:
        # 지표가 여럿이면 지표마다 대안 LLM을 부르지 않는다 (HCX · Gemma 호출이 지표 수만큼 는다)
        for entry, (res, _, _) in zip(entries, outcomes, strict=True):
            if res is not None:
                trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
                return res
        return None

    # 모두 계산하지 못함: 가까운 연도 · 다른 표로 계산한 대안을 LLM이 골라 값과 함께 묻는다
    # (alternative.py)
    async def run(entry: dict, q_alt: dict) -> dict | None:
        sub: dict = {"dynamic": []}
        res, _ = await compute(entry, q_alt, sub)
        for step in sub["dynamic"]:
            step["alternative"] = True
        merge(sub)
        return res

    attempts = [
        {"entry": e, "res": res, "skip": (sub["dynamic"] or [{}])[0].get("skip")}
        for e, (res, _, sub) in zip(entries, outcomes, strict=True)
    ]
    try:
        offered = await alternative.offer(
            question, q, attempts, (picked or {}).get("tbl_nm"), run, trace
        )
    except Exception:
        logger.exception("대안 만들기 실패 → 표 안내")
        offered = None
    if offered is not None:
        return offered
    for entry, (res, _, _) in zip(entries, outcomes, strict=True):
        if res is not None:  # 수록 기간 밖: 그 표의 기간을 안내하는 답 (예전과 같음)
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
            return res
    return None


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
        # 고른 칸은 이 사람의 선택이다. 표만 남겨 다음 사람에게는 다시 묻는다
        # (모두의 기본값이 되지 않게)
        await _remember(parsed, tbl_id, {}, res)
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
        saved = {int(k): v for k, v in (plan.get("forced") or {}).items()}
        forced = saved
        if saved:
            try:
                dims, region_dim, _ = await run_in_threadpool(schema, plan["tbl_id"])
            except Exception:
                logger.exception("캐시한 표의 칸 목록 읽기 실패: %s", plan["tbl_id"])
                continue
            forced = reusable_forced(question, saved, plan.get("words") or [], dims, region_dim)
        res, ask = await _try(plan["tbl_id"], None, question, parsed, trace, forced)
        if dropped := {k: v for k, v in saved.items() if k not in forced}:
            # 저장한 칸 중 새 질문에 맞지 않아 쓰지 않은 칸 (요청 기록)
            trace["dynamic"][-1]["cache_dropped"] = dropped
        if res is not None and res.get("status") != "ok":
            # 저장한 표에 그 연도·지역 자료가 없다 (예: 수록 기간 밖). 다른 표가 있을 수 있어
            # 여기서 답하지 않고 통계표 찾기로 넘긴다
            steps = trace["dynamic"] or [{}]
            steps[-1]["skip"] = f"캐시한 표로 답하지 못함 ({res.get('status')})"
            continue
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


def reusable_forced(
    question: str, forced: dict[int, str], words, dims: list[Dim], region_dim: int | None
) -> dict[int, str]:
    """캐시에 저장한 칸(forced) 중 새 질문에도 쓸 수 있는 칸만.

    캐시의 키는 지표 표현뿐이라, 칸 고르기 LLM이 앞 질문의 말 때문에 고른 칸이 지표 표현만 같은
    다른 질문에도 쓰일 수 있다. 저장한 칸은 질문의 말보다 먼저 정해지므로(choose) 그대로 쓰면
    새 질문이 말한 칸 대신 예전 칸으로 계산한다. 그래서 분류마다
      - 새 질문이 그 분류의 다른 칸을 글자로 말했다          → 쓰지 않는다 (새 질문의 칸)
      - 합계·기본 칸이거나 새 질문이 그 칸을 글자로 말했다     → 쓴다
      - 그 칸을 고른 근거 낱말(words)이 새 질문에도 있다      → 쓴다 (예: "결혼" → 혼인)
      - 그 밖 (근거를 새 질문에서 찾을 수 없음)               → 쓰지 않는다
    쓰지 않은 분류는 규칙이 새 질문으로 다시 고른다 (못 고르면 되묻기나 통계표 찾기)"""
    by_no = {d.no: d for d in dims}
    hits = _text_hits(question, dims, {region_dim})
    q = _norm(question)
    out = {}
    for no, code in forced.items():
        d = by_no.get(no)
        if d is None or code not in d.codes:
            continue  # 표가 다시 적재돼 칸이 없어졌다
        said = hits.get(no) or []
        if said and code not in said:
            continue
        name = _norm(d.codes[code])
        grounded = any(
            len(_norm(a)) >= 2 and _norm(a) in q and _norm(b) in name for a, b in words or []
        )
        if code in (d.total(), d.default()) or code in said or grounded:
            out[no] = code
    return out


async def _remember(parsed: dict, tbl_id: str, forced: dict, res: dict, words=()) -> None:
    """바로 계산에 성공한 계획을 캐시에 남긴다. 실패해도 답은 그대로 준다
    words: 칸 고르기 LLM이 forced의 칸을 고른 근거 [(질문 낱말, 표 낱말)]"""
    spec = (res.get("parsed") or {}).get("dynamic_spec")
    if res.get("status") != "ok" or not spec:
        return
    try:
        found = await run_in_threadpool(plan_cache.key_of, parsed)
        if found:
            await plan_cache.save(found[0], found[1], tbl_id, forced, spec, words)
    except Exception:
        logger.exception("계산 방법 캐시 저장 실패")


async def _try(
    tbl_id, org_id, question, parsed, trace, forced, slots=None, notice=None, fetch_sem=None
):
    """표 하나로 계산해 본다 → (답 또는 None, 되물을 것 또는 None)
    slots: 통계표 찾기의 조건 (칸 반영 검사), notice: 통계표 찾기의 주의 (답에 붙인다)
    fetch_sem: 적재 안 된 표를 KOSIS에서 동시에 받는 수 제한 (꾸러미로 여러 표를 계산할 때)"""
    step = {"tbl_id": tbl_id, **({"forced": forced} if forced else {})}
    trace["dynamic"].append(step)
    extra = {"fetch_sem": fetch_sem} if fetch_sem is not None else {}
    spec, why = await _build(tbl_id, org_id, question, parsed, step, forced, slots, **extra)
    if isinstance(why, BandMiss):
        step["skip"] = str(why)
        return await run_in_threadpool(band_answer, parsed, tbl_id, why), None
    if spec is None:
        step["skip"] = str(why)
        return None, why if isinstance(why, Ask) else None
    step["cells"] = spec["cells"]
    if notice:
        spec["note"] = f"{spec['note']}. {notice}"
    if spec.get("parts_op"):  # 한 분류 안의 칸들로 합·비교·순위
        res = await run_in_threadpool(parts_answer, parsed, spec, question)
        step["status"] = res["status"]
        if res["status"] == "ok":
            return res, None
        step["skip"] = res.get("answer", "")[:60]
        return None, None
    res = await qa.answer(_with_spec(parsed, spec), trace, question)
    step["status"] = res["status"]
    if res["status"] == "ok" and not has_values(res.get("result") or {}):
        step["skip"] = "값이 없음 (비공개·결측)"
        return None, None
    if res["status"] in ("ok", "out_of_range", "band_mismatch"):
        # 기간 밖·구간 불일치도 맞는 표를 찾은 뒤의 안내라 답으로 쓴다 (볼 수 있는 기간·구간을 알림)
        return res, None  # result["method"] = "dynamic"은 qa.compute가 단다
    return None, None


TOP_N = re.compile(r"(?:top|상위)\s*(\d+)|(\d+)\s*(?:위|개|가지)까지", re.I)


def _set_code(spec: dict, col: str, code: str) -> dict:
    """정의의 한 분류를 그 칸으로 바꾼 사본"""
    src = dict(spec["sources"][0])
    if col == "item_id":
        src["item_id"] = code
        # 성별이 항목에 들어 있는 표(1인가구·남자·여자)만 성별 바꾸기를 뺀다. 성별이 따로 분류인
        # 표에서 빼면 계·남·여를 모두 더해 두 배가 된다
        if any("item_id" in over for over in (src.get("by_sex") or {}).values()):
            src.pop("by_sex")
    else:
        src["filters"] = {**(src.get("filters") or {}), col: code}
    return {**spec, "sources": [src]}


def parts_answer(parsed: dict, spec: dict, question: str) -> dict:
    """한 분류 안의 칸들로 합·비교·순위를 계산해 답한다 (qa.answer와 같은 모양).
    칸마다 lookup.value로 DB 값을 꺼내 더하거나 비교·정렬한다 (숫자는 모두 DB 값)"""
    op = spec["parts_op"]
    region = (parsed.get("regions") or ["전국"])[0]
    year = parsed.get("year") or lookup._years(spec["sources"][0])[1]
    sex = parsed.get("sex") or "계"
    rows, first = [], None
    for code, label in zip(op["codes"], op["labels"], strict=True):
        r = lookup.value(_set_code(spec, op["col"], code), region, year, sex)
        if r.get("status") != "ok":
            return {"status": r.get("status"), "answer": r.get("reason") or "", "result": r}
        first = first or r
        rows.append({"label": label, "value": r.get("value")})
    unit, name = first.get("unit") or "", spec["name"]
    where = f"{year}년 {first.get('region') or region}"
    valid = [x for x in rows if x["value"] is not None]
    if not valid:
        return {"status": "missing_data", "answer": "", "result": {"status": "missing_data"}}

    def val(v):
        return qa._val(v, unit)

    lines = []
    if op["op"] == "sum":
        total = sum(x["value"] for x in valid)
        names = "·".join(x["label"] for x in rows)
        text = f"{where}의 {names} {qa._josa(name, '은는')} 합해서 {val(total)}입니다."
        lines = [f"- {x['label']}: {val(x['value'])}" for x in rows] + [f"- 합계: {val(total)}"]
        value = total
    elif op["op"] == "compare":
        hi, lo = (
            sorted(valid, key=lambda x: -x["value"])[0],
            sorted(valid, key=lambda x: x["value"])[0],
        )
        diff = hi["value"] - lo["value"]
        times = f" ({round(hi['value'] / lo['value'], 2)}배)" if lo["value"] else ""
        # 칸이 곧 지표인 비교(주간 인구 vs 상주인구)는 주제를 붙이지 않는다
        about = f"{qa._josa(name, '은는')} " if op.get("subject", name) else ""
        text = (
            f"{where}의 {about}{qa._josa(hi['label'], '이가')} "
            f"{lo['label']}보다 {val(diff)} 많습니다{times}."
        )
        lines = [f"- {x['label']}: {val(x['value'])}" for x in rows]
        value = diff
    else:
        m = TOP_N.search(question)
        n = int(m[1] or m[2]) if m else (parsed.get("top") or 3)
        # 이름으로 합계를 알아보지 못한 첫 칸("통근·통학 인구(12세 이상)")은 값이 나머지의 합과
        # 같으면 합계로 보고 뺀다
        rest = sum(x["value"] for x in valid[1:])
        if len(valid) > 2 and valid[0] is rows[0] and rest and valid[0]["value"] >= rest * 0.95:
            valid = valid[1:]
        top = sorted(valid, key=lambda x: -x["value"])[:n]
        listed = ", ".join(f"{x['label']}({val(x['value'])})" for x in top)
        text = (
            f"{where}의 {op['dim']} 가운데 {qa._josa(name, '이가')} "
            f"가장 많은 {len(top)}개는 {listed}입니다."
        )
        lines = [f"{i}. {x['label']}: {val(x['value'])}" for i, x in enumerate(top, 1)]
        value = top[0]["value"]
    result = {
        "status": "ok",
        "method": "dynamic",
        "indicator": name,
        "unit": unit,
        "year": year,
        "region": first.get("region"),
        "value": value,
        "categories": rows,
        **{k: first.get(k) for k in ("source", "basis", "tbl_id", "url")},
    }
    notes = [spec.get("note")] if spec.get("note") else []
    tail = [f"※ {n}" for n in notes]
    tail += [f"출처: {result['source']} ({result['basis']})", f"통계표: {result['url']}"]
    return {
        "status": "ok",
        "answer": "\n".join([text, "", *lines, *tail]),
        "parsed": {**parsed, "dynamic_spec": spec, "indicator": name},
        "result": result,
        "sources": qa.sources(result),
        "trace": {},
    }


def band_answer(parsed: dict, tbl_id: str, miss: BandMiss) -> dict:
    """맞는 구간 없음 응답 (qa.answer와 같은 모양): 숫자 없이 표에 있는 구간과 그 표를 안내한다"""
    tbl = lookup._table(tbl_id)
    labels = miss.labels[:10]
    result = {
        "status": "band_mismatch",
        "indicator": tbl["tbl_nm"],
        "reason": f"'{tbl['tbl_nm']}' 표의 {miss.dim_name} 구간 중에 질문한 '{miss.item}' 조건과 "
        "맞는 구간이 없어 그 값을 낼 수 없습니다.",
        "available_bands": labels,
        "alternative": f"대신 볼 수 있는 구간: {', '.join(labels)}",
        "source": f"{tbl['org_nm']}, {tbl['tbl_nm']} ({tbl_id})",
        "basis": tbl["basis"] or "KOSIS 공표 값",
        "tbl_id": tbl_id,
        "url": indicators.kosis_url(tbl_id),
        "method": "dynamic",
    }
    return {
        "status": "band_mismatch",
        "answer": f"{qa.summary(result)}\n\n{qa.render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": qa.sources(result),
        "trace": {},
    }


def ask_answer(question, parsed, tbl_id, tbl_nm, ask: Ask, forced=None) -> dict:
    """칸 되묻기 응답 (ask_session의 되묻기와 같은 모양). context는 대화 상태에만 남고
    API 응답(QuestionClarification)에는 나가지 않는다"""
    names = [name for _, name in ask.options]
    forced = forced or ask.forced
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


async def _build(tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None):
    """fetch_sem: 후보 여러 개를 동시에 계산할 때 KOSIS에서 받는 일을 동시에 몇 개까지 할지"""
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
    except StatsTableMissing:
        pass
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"
    # 적재 안 된 표: 작은 표는 지금 받아 넣는다. 못 받으면 대기열에 넣고 이번에는 표 안내
    try:
        org_id = org_id or (await load_queue.table_info(tbl_id))["org_id"]
        if fetch_sem is None:
            outcome = await table_cache.fetch_now(tbl_id, org_id, step)
        else:
            async with fetch_sem:
                outcome = await table_cache.fetch_now(tbl_id, org_id, step)
        if outcome != "loaded":
            await load_queue.enqueue(tbl_id, "question")
            return None, f"적재 안 됨 ({outcome}, 대기열에 넣음)"
    except Exception:
        logger.exception("동적 지표 표 받기 실패: %s", tbl_id)
        return None, "표를 받지 못함"
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"


def _with_spec(parsed: dict, spec: dict) -> dict:
    """질문 해석 결과에 지표 정의를 넣는다. 연도를 말하지 않았으면 그 표의 마지막 해"""
    q = {**parsed, "indicator": spec["name"], "unsupported": False, "dynamic_spec": spec}
    if spec["kind"] == "ratio" and q.get("type") == "composition":
        q["type"] = "value"  # "~ 중 ~ 비율"은 구성비가 아니라 비율 값 하나
    if q.get("type") in (None, "other"):
        q["type"] = "value"  # 질문 종류를 정하지 못했어도 칸으로 정의를 만들었으면 그 값 하나
    if not q.get("year") and q.get("type") in LAST_YEAR_KINDS:
        try:
            q["year"] = lookup._years(spec["sources"][0])[1]
        except Exception:
            logger.exception("동적 지표 수록 기간 확인 실패")
    return q
