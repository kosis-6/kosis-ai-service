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
import contextlib
import contextvars
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from starlette.concurrency import run_in_threadpool

from app.core.stats_db import StatsTableMissing
from app.core.stats_db import rows as _rows
from app.services import (
    alternative,
    cell_pick,
    flow,
    indicators,
    load_queue,
    lookup,
    partial_table,
    plan_cache,
    qa,
    search,
    search_answer,
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
# 추계 표로 답할 때 첫 문장 바로 뒤에 붙인다 (※ 주의만으로는 확정 수치처럼 읽혔다, 11/23)
PROJECTION_ANSWER = "장래추계에 따른 예상값으로, 실제 통계가 아닙니다."
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총", "전국"}
# 혼인 · 이혼 표는 성별을 남편 · 아내로 적는다 ("이혼하는 남자 평균 나이" → 남편)
SEX_NAMES = {"남": {"남자", "남", "남성", "남편"}, "여": {"여자", "여", "여성", "아내"}}
COUNT_UNITS = {"명", "가구", "건", "개", "세대", "호", "곳", "개소", "천명", "천가구"}
# 표가 쓰는 말과 같은 뜻의 일상어 (글자 맞추기 전에 바꾼다). 뜻이 하나로 정해진 말만 둔다
SAME_WORDS = {"출퇴근": "통근", "출근": "통근"}
UNIT_TAIL = re.compile(r"\s*\([^()]*\)\s*$")
# "학교 가는 · 다니는"은 통학일 수도 재학일 수도 있어 위 목록에 넣지 않는다. 이동을 말할 때만
# 통학으로 바꾼다 ("출근하거나 학교 가는 사람 수" → 통근 + 통학). "학교 다니는 학생 수"(재학)와
# "고등학교 다니는"(교육정도의 고등학교 칸)은 그대로 둔다 (11/19). 목록 밖 말("등굣길")은
# 에이전트화 때 실패 분석 · 쿼리 재작성이 맡는다
SCHOOL_GOING = re.compile(r"(?:^|(?<=\s))학교\s*(?:에\s*)?(?:가는|가고|간|다니는|다니고|다닌)")
MOVE_CONTEXT = re.compile(
    r"출근|출퇴근|통근|지하철|전철|버스|자가용|승용차|자전거|타고|걸어서|교통"
)
TOTAL_SUFFIX = re.compile(r"(계|합계|소계|전체)$")  # 공백·기호를 뺀 이름 끝의 합계 표시
PCT_ASK = re.compile(r"%|퍼센트|비율|비중|몇\s*프로")
WITHIN = re.compile(r"\s+(?:중(?:에?서|에)?|가운데)\s+")
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
        # "계(쌍)"처럼 이름 끝 괄호의 단위는 떼고 본다
        found = [
            c for c, n in self.codes.items()
            if _norm(n) in TOTAL_NAMES or _norm(UNIT_TAIL.sub("", n)) in TOTAL_NAMES
        ]  # fmt: skip
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
    couple: bool = False  # 부부를 한 쪽씩 센 표의 성별 칸으로 답함 (남녀를 더하면 두 번 센다)
    # 성별 합계 칸이 없고 한 칸으로 답할 수 없어 남 · 여를 따로 보여 준다
    # {"남": 이름표, "여": 이름표}. 이때 pick의 성별 칸은 자리만 채운 남자 칸이다
    # (_build가 '계' 없는 by_sex를 만든다)
    split_sex: dict[str, str] | None = None


# ---- 표의 칸 목록 ----------------------------------------------------------------


def schema(tbl_id: str) -> tuple[list[Dim], int | None, str]:
    """(분류 목록, 지역 분류 번호, 지역 수준). 적재 안 된 표면 StatsTableMissing
    칸 이름·지역 칸은 lookup의 표 정보 캐시를 쓴다 (표를 다시 적재하면 함께 비워진다)
    이 질문에서 칸만 받은 표면 그 메타의 칸 목록 (partial_table.py)"""
    if (part := partial_table.active(tbl_id)) is not None:
        dims = [
            Dim(n, name, {c: nm for c, (_, nm) in sorted(codes.items())})
            for n, (name, codes) in sorted(part.dims.items())
        ]
        return dims, part.region_dim, partial_table.level(part)
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
    level = "전국"
    if region_dim is not None:
        # 부분 조회와 같이 전국(00)을 제외한 코드의 길이로 지역 수준을 정한다.
        longest = _rows(
            "select max(length(region_code)) as n from region_code_map"
            " where tbl_id = ? and src_dim = ? and region_code <> '00'",
            [tbl_id, region_dim],
        )[0]["n"]
        if longest:
            level = "시군구" if longest >= 5 else "시도"
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


def _unit(
    tbl_id: str, item: str, cells: dict[int, list[str]] | None = None
) -> tuple[str | None, bool]:
    """(단위, 값이 모두 정수인가)
    단위가 명이어도 평균 가구원 수(2.3명)처럼 소수면 사람 수가 아니다.
    cells: 항목 밖에서 고른 칸 {분류 번호: 코드들}. 단위가 항목이 아니라 분류 칸마다 다른 표가
    있다 (인구동향 DT_1B8000G: 항목은 '출생사망혼인이혼' 하나, 종류별 칸이 출생아수(명) ·
    조출생률(천명당) · 이혼건수(건) …). 항목만 보면 행이 가장 많은 '천명당'이 붙었다
    ("11,798천명당", 11/20).
    고른 칸으로 좁힌 행이 없으면 항목만으로 본다"""
    if (part := partial_table.active(tbl_id)) is not None:
        return partial_table.unit_of(part, item, cells)
    conds, params = [], []
    for n, codes in {0: [item], **(cells or {})}.items():
        cond, keys = lookup._in("v.item_key" if n == 0 else f"v.c{n}", tbl_id, n, codes)
        conds.append(cond)
        params += keys
    r = _rows(
        "select mode() within group (order by u.name) as u,"
        " bool_and(v.value = trunc(v.value)) as whole"
        " from stat_value v join stat_unit u using (unit_key)"
        f" where v.tbl_key = ? and v.{lookup.YEARLY}" + "".join(f" and {c}" for c in conds),
        [lookup._tbl_key(tbl_id), *params],
    )
    if cells and (not r or r[0]["u"] is None):
        return _unit(tbl_id, item)
    return (r[0]["u"], bool(r[0]["whole"])) if r else (None, False)


def _unit_cells(plan: Plan, dims: list[Dim], region_dim: int | None) -> dict[int, list[str]]:
    """단위를 볼 때 좁힐 칸: 항목 · 지역 · 연령 밖에서 고른 칸 (여러 칸 연산이면 그 칸들)"""
    skip = {0, region_dim, plan.age_dim.no if plan.age_dim else None}
    cells = {d.no: [plan.pick[d.no]] for d in dims if d.no not in skip and d.no in plan.pick}
    if plan.multi and plan.multi[0] not in skip:
        cells[plan.multi[0]] = list(plan.multi[1])
    return cells


# ---- 칸 고르기 -------------------------------------------------------------------


def table_words(question: str) -> str:
    """칸 이름과 글자를 맞추기 전에 질문의 일상어를 표가 쓰는 말로 바꾼 글자 (공백 · 기호 뺌)"""
    if MOVE_CONTEXT.search(question):  # 띄어쓰기를 지우기 전에 봐야 "고등학교"를 피한다
        question = SCHOOL_GOING.sub("통학", question)
    q = _norm(question)
    for word, same in SAME_WORDS.items():  # "출퇴근" → "통근"
        q = q.replace(word, same)
    return q


def _text_hits(question: str, dims: list[Dim], skip: set[int]) -> dict[int, list[str]]:
    """질문에 이름이 그대로 나온 칸 {분류 번호: [코드...]}. 더 긴 이름 안에 든 짧은 이름은 뺀다"""
    q = table_words(question)
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
    for d in dims:
        if d.no in skip or d.no in hits:
            continue
        if found := _word_hits(question, d):
            hits[d.no] = found
    return hits


MAX_WORD_CELLS = 2  # 낱말 하나로 고를 수 있는 칸 수 ("외국인" → 외국인 아내 · 외국인 남편)
# 칸 이름 일부로 맞추기에서 쓰지 않는 낱말 (칸마다 흔히 붙는 말)
GENERIC_WORDS = {
    "인구", "이상", "이하", "미만", "전체", "합계", "소계", "기타", "가구", "건수", "사람",
}  # fmt: skip
JOSA_TAIL = re.compile(
    r"(이랑|랑|하고|와|과|이나|나|으로|로|에서|에|의|은|는|이|가|을|를|도|만|중에|중|들)$"
)


def _word_hits(question: str, d: Dim) -> list[str]:
    """칸 이름이 질문에 글자 그대로 없을 때: 질문 낱말이 칸 이름 안에 든 칸.
    "지하철" ⊂ "전철 지하철", "미성년 자녀" ⊂ "미성년자녀-없음",
    "외국인" ⊂ "한국인 남편 + 외국인 아내". "시내버스"처럼 이름 사이에 다른 말이 낀 경우는
    낱말의 앞 두 글자와 뒤 두 글자가 모두 이름에 있으면.
    합계 칸은 맞추지 않는다 (세부 칸을 고르는 것이므로)"""
    words = []
    for token in question.split():
        w = _norm(JOSA_TAIL.sub("", token))
        if len(w) >= 2 and w not in GENERIC_WORDS and not w.isdigit():
            words.append(w)
    whole = {d.total()} | {c for c, n in d.codes.items() if TOTAL_SUFFIX.search(_norm(n))}
    cells = [(c, _norm(n)) for c, n in d.codes.items() if c not in whole]
    out: list[str] = []
    for w in words:
        found = [c for c, n in cells if w in n or (len(w) >= 4 and w[:2] in n and w[-2:] in n)]
        # 낱말 하나가 칸 셋 이상에 걸리면 그 낱말로는 고르지 않는다 ("버스" → 버스 칸 셋)
        if len(found) <= MAX_WORD_CELLS:
            out += [c for c in found if c not in out]
    return out


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
COMPARE_WORDS = re.compile(
    r"비교|차이|몇\s*배|보다|대비|vs|더\s*(많|적|높|낮)|어느\s*(게|쪽|것)|뭐가\s*더|뭘\s*더", re.I
)
RANK_PARTS = re.compile(
    r"top\s*\d|상위\s*\d|순위|(가장|제일)\s*(많|적|높|낮)|1위|몇\s*위|(많|높|적|낮)은\s*(순서|순)",
    re.I,
)
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
        # "국적(지역)별"의 괄호는 떼고 본다 ("국적별로 많은 순서")
        stem = _norm(re.sub(r"(별|구분)$", "", re.sub(r"\(.*?\)", "", d.name)))
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
    couple: bool = False,
):
    """(Plan, None) 또는 (None, 고르지 못한 이유: 문자열 또는 Ask)
    forced: 사용자가 되묻기에서 고른 칸 {분류 번호: 코드}
    tbl_nm: 표 이름 (분류 안의 순위에서 '교통수단별'처럼 표 이름에만 있는 분류를 알아본다)
    couple: 남자 · 여자 칸으로 부부를 한 쪽씩 센 표 (couple_counted).
    성별을 말하지 않으면 남자 칸"""
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
            if (
                not plan.multi
                and not others
                and sex_set <= set(codes)
                and _multi_op(question) == "compare"
            ):
                # "남자애가 더 많이 태어났어 여자애가 더 많이?": 남 · 여 칸끼리 비교
                both = [d.sex_codes()["남"], d.sex_codes()["여"]]
                plan.multi, plan.multi_dim = (d.no, both, "compare"), d
                plan.pick[d.no] = plan.matched[d.no] = both[0]
                continue
            base = (others[:1] or [d.total()] or [None])[0]
            if base is None:
                rest = [c for c in d.codes if c not in sex_set]
                base = rest[0] if len(rest) == 1 else None
            if base is None and sex in ("남", "여"):
                # 합계 칸이 없는 성별 분류 (남편 · 아내): 말한 성별의 칸 ("이혼하는 남자" → 남편)
                plan.pick[d.no] = plan.matched[d.no] = d.sex_codes()[sex]
                plan.couple = couple or _couple_code(d) is not None
                continue
            if (
                base is None
                and sex not in ("남", "여")
                and (husband := _couple_code(d) or (d.sex_codes()["남"] if couple else None))
            ):
                if parsed.get("ages"):
                    # 연령별이면 남편 · 아내 나이가 달라 한 쪽 칸으로 답할 수 없고, 더하면 같은
                    # 부부를 두 번 센다 ("1975년 서울 30대 혼인": 남편 13,257 · 아내 3,367)
                    # → 남편 쪽 · 아내 쪽을 따로 보여 준다
                    plan.pick[d.no] = husband
                    plan.split_sex = {"남": "남편", "여": "아내"}
                    plan.defaults.append(
                        f"{d.name}: 남편 · 아내 따로"
                        " (부부 단위 건수라 연령은 남편 · 아내 나이가 다름)"
                    )
                    plan.couple = True
                    continue
                # 남편 · 아내만 있는 분류, 또는 남자 · 여자로 부부를 한 쪽씩 센 표 (couple_counted):
                # 부부 단위 건수라 연령을 나누지 않으면 남편 쪽 칸이 전체 건수다
                plan.pick[d.no] = husband
                plan.defaults.append(f"{d.name}: {d.codes[husband]} {couple_note(region_dim)}")
                plan.couple = True
                continue
            if len(others) > 1:
                asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in others]))
            elif base is None:
                # 성별 합계 칸이 없는 사람 수 표 ("서울 기혼자 수", 항목이 남자 · 여자뿐):
                # 남 · 여를 따로 보여 준다 (지표 사전의 needs_split과 같다)
                male, female = d.sex_codes()["남"], d.sex_codes()["여"]
                plan.pick[d.no] = male
                plan.split_sex = {"남": d.codes[male], "여": d.codes[female]}
                plan.defaults.append(f"{d.name}: {d.codes[male]} · {d.codes[female]} 따로")
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
        elif (husband := _couple_code(d)) and not parsed.get("ages"):
            # 남편 · 아내만 있는 분류 (혼인 · 이혼 표): 부부 단위 건수라 연령을 나누지 않으면 어느
            # 쪽으로 세도 같다 ("이혼 부부 중 미성년 자녀 없는 비율"을 성별로 되묻고 있었다)
            plan.pick[d.no] = husband
            plan.defaults.append(f"{d.name}: {d.codes[husband]} {couple_note(region_dim)}")
            plan.couple = True
        elif len(d.codes) <= MAX_ASK_OPTIONS:
            asks.append(Ask(d.no, d.name, list(d.codes.items())))
        else:
            return None, f"{d.name}에서 칸을 고르지 못함"
    if not plan.matched and not (plan.sex_dim and sex in ("남", "여")):
        return None, "질문의 말과 맞는 칸이 없음"  # 맞는 말이 없는 표는 되묻지도 않는다
    if asks:
        return None, asks[0]
    return plan, None


def _couple_code(d: Dim) -> str | None:
    """분류가 남편 · 아내 두 칸뿐이면 남편 칸 코드"""
    names = {_norm(n): c for c, n in d.codes.items()}
    if set(names) == {"남편", "아내"}:
        return names["남편"]
    return None


# 부부 단위 건수 표에서 남편 쪽 칸으로 답할 때의 안내.
# 전국 값은 두 쪽이 같고, 지역 값은 남편 쪽이 공표된 시도별 건수와 같다 (남편 주소지 기준).
# 혼인 DT_1B80023 남편 칸 = 시도별 혼인 DT_1B80022 (1980 서울 90,497 · 아내 칸 76,675),
# 이혼 DT_1B80032 남편 칸 = 시도별 이혼 DT_1B80030 (1980 서울 7,935 · 아내 칸 8,020) (11/22 확인)
COUPLE_NOTE = "기준 (부부 단위 건수. 지역별 값은 남편 주소지 기준으로, 공표된 시도별 건수와 같음)"
# 지역 구분이 없는 표 (다문화 유형별 이혼 DT_1BB0003): 지역 값 이야기는 맞지 않는다
COUPLE_NOTE_NATIONAL = "기준 (부부 단위 건수)"


def couple_note(region_dim: int | None) -> str:
    return COUPLE_NOTE if region_dim is not None else COUPLE_NOTE_NATIONAL


def table_notice(notice: str | None, couple: bool) -> str | None:
    """답에 붙일 통계표 찾기의 주의. 부부를 한 쪽씩 센 표의 성별 칸으로 답했으면(성별을 말했든
    아니든) "남녀 값을 더해 보라"는 표 안내를 뺀다 (더하면 두 번 센다, V2-053 · V2-035)"""
    if not notice or not couple:
        return notice
    left = notice.replace(search_answer.NO_TOTAL_NOTE.rstrip("."), "")
    return re.sub(r"(?:\.\s*){2,}", ". ", left).strip(" .") or None


@lru_cache(maxsize=512)
def couple_counted(tbl_id: str) -> bool:
    """성별이 남자 · 여자 두 칸뿐(합계 없음)인 표가 부부를 한 쪽씩 센 건수인가 (혼인 · 이혼 표).
    낱말이 아니라 값으로 본다: 전국(지역 합계) 행의 남자 값 합과 여자 값 합이 같으면 그렇다.
    사람을 센 표(인구 · 기혼자 수 등)는 남 ≠ 여라 해당하지 않는다. 비공개 칸이 섞여 합이 어긋나면
    아니라고 본다 (지금처럼 칸을 고르지 못함)"""
    dims, region_dim, _ = schema(tbl_id)
    d = next((x for x in dims if x.no != region_dim and x.sex_codes()), None)
    if d is None or d.total() is not None or set(d.codes) != set(d.sex_codes().values()):
        return False
    conds, params = [], []
    if region_dim is not None:
        region = next((x for x in dims if x.no == region_dim), None)
        top = region.total() if region else None
        if top is None:
            return False
        cond, keys = lookup._in(f"v.c{region_dim}", tbl_id, region_dim, [top])
        conds.append(cond)
        params += keys
    known = lookup._codes(tbl_id, d.no)
    male, female = (known[d.sex_codes()[s]][0] for s in ("남", "여"))
    column = "v.item_key" if d.no == 0 else f"v.c{d.no}"
    row = _rows(
        f"select sum(v.value) filter (where {column} = ?)::float8 as m,"
        f" sum(v.value) filter (where {column} = ?)::float8 as f"
        f" from stat_value v where v.tbl_key = ? and v.{lookup.YEARLY}"
        + "".join(f" and {c}" for c in conds),
        [male, female, lookup._tbl_key(tbl_id), *params],
    )[0]
    return bool(row["m"]) and row["m"] == row["f"]


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
    # 표 이름 · 표의 대상 (넘기기 꾸러미로 계산할 때만 있다)
    titles = [_norm(w) for w in slots.get("table_words") or [] if w]
    # 칸 고르기 LLM이 고른 칸으로 계산할 때는 조건 낱말 검사를 하지 않는다. LLM이
    # "질문과 뜻이 다르거나 더 넓은 칸으로 대신하지 않는다"는 지시로 조건까지 보고 칸을 골랐다
    # (11/18, 대상 검사와 같은 원칙)
    for item in [] if slots.get("cells_by_llm") else cond.get("items") or []:
        # 표 자체가 그 조건으로 만든 표면(표 이름에 그 낱말이 있다) 표가 이미 반영했다.
        # 예: '재혼' → 시도별 평균재혼연령 표는 재혼 칸 없이 아내 · 남편 칸으로 답한다.
        # 통계표 찾기의 조건 확인(search.check: 칸 값이나 표 이름에 있으면 ✔)과 같은 기준.
        # 표 이름에 없는 조건('미혼 1인 가구'의 미혼 → 1인가구 표)은 지금처럼 칸에서 찾는다
        if len(word := _norm(item)) >= 2 and any(word in t for t in titles):
            continue
        if not _item_left(item, names, read, plan, parsed):
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


def _item_left(item: str, names: list[str], read: list[str], plan: Plan, parsed: dict) -> bool:
    """조건 낱말 하나가 고른 칸(이름 · 바꿔 읽은 말 · 연령 · 성별)으로 설명되지 않으면 True"""
    word = _norm(item)
    if not word:
        return False
    if any(word in n or n in word for n in names if len(n) >= 2):
        return False
    if any(word in r or r in word for r in read):
        return False
    is_age = lookup.parse_age(item) or qa.extract_ages(item)  # "62살 이상"·"67살 이하"도
    if is_age and parsed.get("ages") and plan.age_dim is not None:
        return False  # 연령 구간은 lookup이 연령 칸으로 고른다 (안 맞으면 band_mismatch)
    return not (
        any(word in w for w in ("남자", "여자", "남성", "여성")) and plan.sex_dim is not None
    )


def _report_cells(
    slots: dict | None, plan: Plan | None, dims: list[Dim], parsed: dict, region_dim: int | None
) -> None:
    """slots["report"]가 있으면 고른 칸 이름과 칸으로 설명되지 않은 조건 낱말을 적는다
    (같은 표 계산의 실패 기록: 에이전트가 이 기록을 보고 말을 표의 낱말로 다시 쓴다)"""
    report = (slots or {}).get("report")
    if report is None:
        return
    items = ((slots or {}).get("conditions") or {}).get("items") or []
    if plan is None:
        report.update(cells=[], leftover=list(items))
        return
    cells = [
        d.codes[plan.matched[d.no]] for d in dims if d.no in plan.matched and d.no != region_dim
    ]
    names = [_norm(d.codes[c]) for d in dims for no, c in plan.pick.items() if no == d.no]
    if plan.multi:
        md = plan.multi_dim or next(d for d in dims if d.no == plan.multi[0])
        names += [_norm(md.codes[c]) for c in plan.multi[1]]
    read = [_norm(a) for a, _ in (slots or {}).get("read_as") or [] if len(_norm(a)) >= 2]
    report.update(
        cells=cells, leftover=[i for i in items if _item_left(i, names, read, plan, parsed)]
    )


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


def _name(
    parsed: dict,
    dims: list[Dim],
    plan: Plan,
    counted: bool,
    strip_age: bool = False,
    unit: str = "",
) -> str:
    """지표 이름: 질문 해석의 지표 표현이 고른 칸 이름을 모두 담고 있으면 그 표현, 아니면 칸 이름
    (HCX가 지표 칸에 "2020년 서울"처럼 지표가 아닌 말을 넣는 경우가 있다)
    strip_age: 연령을 lookup이 연령 칸으로 고르면 이름에서 연령 표현을 뺀다 (두 번 붙지 않게)
    unit: 값의 단위. 칸 이름 끝의 같은 단위 괄호는 뗀다 ("이혼건수(건)" → "이혼건수")"""
    named = [d for d in dims if d.no in plan.matched]
    # 칸이 하나뿐인 분류(인구동향 표의 항목 '출생사망혼인이혼')는 고른 것이 아니라 표 전체라,
    # 다른 고른 칸이 있으면 이름에 쓰지 않는다 ("출생사망혼인이혼 이혼건수(건)", 11/20)
    named = [d for d in named if len(d.codes) > 1] or named
    words = [d.codes[plan.matched[d.no]] for d in named]
    words = words or [dims[0].codes[plan.pick[0]]]
    if unit:
        tail = re.compile(rf"\s*\(\s*{re.escape(unit)}\s*\)\s*$")
        words = [tail.sub("", w) or w for w in words]
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
    projection = {}
    if is_projection(tbl_id):
        future_after = indicators._data_years()[1]  # 실적(주민등록인구) 자료의 마지막 해
        if not _asks_future(parsed, future_after):
            return None, "장래추계 표는 미래 연도를 물을 때만 씀"
        projection = {
            "future_after": future_after,
            "note": f"{NOTE}. {PROJECTION_NOTE}",
            "answer_note": PROJECTION_ANSWER,
        }
    ignore_ages = False
    if parsed.get("ages") and parsed.get("age_group") and not _has_age_dim(dims, region_dim):
        # "노년부양비"의 노년처럼 말에서 짐작한 연령(qa.correct의 age_group)은 지표 이름의 일부일 수
        # 있다. 표에 연령 분류가 없으면 연령 조건 없이 고른다 ("65세 이상"처럼 숫자로 말하면 지킨다)
        parsed, ignore_ages = {**parsed, "ages": None}, True
    try:
        tbl_nm = lookup._table(tbl_id)["tbl_nm"]
    except Exception:  # 표 이름은 분류 안 순위에서만 쓴다
        tbl_nm = ""
    if PER_AVERAGE.search(question) and (avg := _per_average(tbl_id, dims, region_dim, level)):
        return {**avg, **projection}, None
    try:
        # 칸만 받은 표는 값 전체를 볼 수 없어 아니라고 둔다 (캐시에 남지 않게 부르지도 않는다)
        couple = partial_table.active(tbl_id) is None and couple_counted(tbl_id)
    except Exception:  # 확인하지 못하면 지금처럼 (성별 칸을 고르지 못함)
        logger.exception("부부 단위 표 확인 실패: %s", tbl_id)
        couple = False
    plan, why = choose(question, parsed, dims, region_dim, forced, tbl_nm, couple)
    _report_cells(slots, plan, dims, parsed, region_dim)
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
    unit, whole = _unit(tbl_id, plan.pick[0], _unit_cells(plan, dims, region_dim))
    unit = unit or ""
    counted = unit in COUNT_UNITS and whole
    strip_age = bool(
        plan.age_dim
        and parsed.get("ages")
        and parsed.get("type") != "composition"
        and not PCT_ASK.search(question)
    )
    name = _name(parsed, dims, plan, counted, strip_age, unit)
    spec = {
        "name": name,
        "unit": unit,
        "kind": "count" if counted else "value",
        "method": "lookup",
        "dynamic": True,
        # 사전 지표 이름으로 답하면 사전 표에 없는 연도 등을 다른 표에서 꺼낸 것이다 (옛 연도)
        "note": OTHER_TABLE_NOTE if indicators.canonical_name(name) else NOTE,
        "sources": [src],
        **projection,
        **({"couple": True} if plan.couple else {}),
    }
    kind = parsed.get("type")
    if plan.split_sex and (plan.multi or kind == "composition" or PCT_ASK.search(question)):
        # 한 분류 안의 연산 · 구성비 · 비율은 남녀를 나눠 계산하지 않는다: 자리만 채운 남자 칸
        # 값이 답이 되지 않게 지금처럼 고르지 못한 것으로 둔다
        return None, f"{plan.sex_dim.name}에서 칸을 고르지 못함"
    if plan.multi:
        return _parts_spec(spec, src, plan, dims, tbl_nm)
    if kind == "composition" or PCT_ASK.search(question):
        made = _composition(spec, src, plan, dims, question) if kind == "composition" else None
        if made is None:
            made, why = _ratio(spec, src, plan, dims, parsed, tbl_nm, question=question)
            if made is None:
                return None, why
        spec = made
    else:
        if plan.split_sex:
            # '계' 없이 남 · 여만 둔다 → qa.compute가 lookup.needs_split으로 둘 다 계산한다
            src["by_sex"] = {
                s: {**_sex_override(plan, s), "label": label} for s, label in plan.split_sex.items()
            }
        elif plan.sex_dim is not None:
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
    if plan.defaults:
        spec["note"] += (
            f". 말하지 않은 구분은 합계·기본값으로 계산했습니다 ({', '.join(plan.defaults)})"
        )
    if ignore_ages:
        spec["ignore_ages"] = True  # qa.compute가 질문의 연령을 넘기지 않는다
    if not spec.get("formula") and (cells := _cells(plan, dims)):
        spec["formula"] = f"KOSIS 공표 값 ({cells})"  # 칸이 하나뿐이면 lookup이 표 이름을 쓴다
    spec["cells"] = _cells(plan, dims)
    pair_unit = spec.get("unit") == "쌍" or "(쌍)" in spec["cells"]  # "쌍태아 (명)"은 사람 수
    if PAIRS_ASKED.search(question) and not pair_unit:
        # "쌍둥이 몇 쌍": 사람 수(쌍태아 14,963명)가 아니라 쌍 단위 값이어야 한다
        return None, "질문은 '쌍' 단위인데 표의 칸은 다른 단위"
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
    if len(_norm(name)) >= 4 and _norm(name) in _norm(tbl_nm):
        return code
    # 이름 끝에 합계 표시가 붙은 첫 칸 ("이혼건수-계"): 다른 칸 이름에는 합계 표시가 없을 때만
    marked = [c for c, n in d.codes.items() if TOTAL_SUFFIX.search(_norm(n)) and "-" in n]
    return code if marked == [code] else None


# "가구당 평균 가구원 수": 같은 표의 가구원 칸 ÷ 가구 칸
PER_AVERAGE = re.compile(
    r"(가구|세대)\s*당\s*(평균\s*)?(가구원|세대원)|평균\s*(가구원|세대원)\s*수"
)
MEMBER_CELL = re.compile(r"^(.*?)_?(가구원|세대원)(_?계)?$")


def _per_average(tbl_id: str, dims: list[Dim], region_dim: int | None, level: str) -> dict | None:
    """항목에 '가구원'과 같은 묶음의 '가구'가 함께 있으면 가구원 ÷ 가구 (평균 가구원 수).
    나머지 분류는 합계로 둔다. 합계가 없는 분류가 있거나 짝이 없으면 None"""
    names = {re.sub(r"\s", "", n): c for c, n in dims[0].codes.items()}  # 밑줄은 둔다 (묶음 구분)
    pair = None
    for name, code in names.items():
        m = MEMBER_CELL.match(name)
        if not m:
            continue
        prefix = m[1].rstrip("_")
        household = "가구" if m[2] == "가구원" else "세대"
        den = names.get(f"{prefix}_{household}" if prefix else household)
        if den is None and prefix.endswith(household):
            den = names.get(prefix)  # "일반가구" + "일반가구_가구원" 꼴
        if den and (pair is None or prefix.startswith("일반")):
            # 공식 평균 가구원 수는 일반가구 기준이라 일반가구 짝이 있으면 그쪽
            pair = (code, den, dims[0].codes[code], dims[0].codes[den])
    if pair is None:
        return None
    filters = {}
    for d in dims:
        if d.no in (0, region_dim):
            continue
        if not (code := d.total()):
            return None
        filters[d.col] = code
    num, den, num_nm, den_nm = pair
    num_nm, den_nm = num_nm.replace("_", " "), den_nm.replace("_", " ")
    src = {"table": tbl_id, "level": level, "item_id": num, "den_item_id": den, "den_scale": 1}
    if level == "전국":
        src["national_only"] = True
    if filters:
        src["filters"] = filters
        src["den_filters"] = filters
    member = "가구원" if "가구원" in num_nm else "세대원"
    return {
        "name": f"{'가구' if member == '가구원' else '세대'}당 평균 {member} 수",
        "unit": "명",
        "kind": "value",
        "method": "lookup",
        "dynamic": True,
        "note": NOTE,
        "formula": f"{num_nm} ÷ {den_nm}",
        "cells": f"항목 {num_nm} ÷ {den_nm}",
        "sources": [src],
        "fixed": {"sex": "계"},
    }


def _merged_age_bands(bands):
    """겹치거나 이어지는 연령 구간을 합쳐 같은 사람을 두 번 세지 않는다."""
    merged = []
    for lo, hi in sorted(bands, key=lambda band: band[0]):
        if not merged or (merged[-1][1] is not None and lo > merged[-1][1] + 1):
            merged.append((lo, hi))
        else:
            start, end = merged[-1]
            merged[-1] = (start, None if end is None or hi is None else max(end, hi))
    return merged


def _age_intersection(parts, population):
    bands = []
    for part_lo, part_hi in parts:
        for pop_lo, pop_hi in population:
            start = max(part_lo, pop_lo)
            ends = [hi for hi in (part_hi, pop_hi) if hi is not None]
            end = min(ends) if ends else None
            if end is None or start <= end:
                bands.append((start, end))
    return _merged_age_bands(bands)


def _ratio(spec, src, plan, dims, parsed, tbl_nm="", *, question=""):
    """'~ 중 ~ 비율': 나눌 부분 하나 ÷ 같은 분류의 합계 × 100 → (정의, None) 또는 (None, 이유)"""
    sex = parsed.get("sex") or "계"
    parts = []  # (분류 열, 분자 코드들, 분모 코드, 이름)
    scope = WITHIN.split(question, maxsplit=1)
    population_ages = qa.resolved_ages(scope[0]) if len(scope) == 2 else []
    ages = (parsed.get("ages") or [])[:1]
    age_dim = plan.age_dim
    population_label = ""
    if population_ages:
        if age_dim is None:
            return None, "표에 모집단을 제한할 연령 구분이 없음"
        population_ages = _merged_age_bands(population_ages)
        population_label = "·".join(map(indicators.age_label, population_ages))
        requested_ages = qa.resolved_ages(scope[1])
        ages = _age_intersection(requested_ages, population_ages)
        if requested_ages and not ages:
            return None, "분자의 연령이 모집단의 연령 범위에 포함되지 않음"
        try:
            population_codes = [
                code for band in population_ages for code in lookup._age_codes(src, band)
            ]
        except lookup.AgeMismatch:
            return None, "모집단의 연령 구간을 표의 연령 칸으로 만들 수 없음"
        if not ages:
            src = _without_age(src)
            src["filters"] = {**src.get("filters", {}), age_dim.col: population_codes}
    population_sex = False
    if plan.sex_dim is not None and sex in ("남", "여"):
        d = plan.sex_dim
        population_sex = (
            len(scope) == 2
            and bool(qa.GENDER_WORDS[sex].search(scope[0]))
            and not any(pattern.search(scope[1]) for pattern in qa.GENDER_WORDS.values())
        )
        if population_sex:
            # "여성 1인 가구 중 70세 이상 비율": 분자와 분모 모두 여성 조건으로 계산한다.
            override = _sex_override(plan, sex)
            src = {
                **src,
                **override,
                "filters": {**src.get("filters", {}), **override.get("filters", {})},
            }
        else:
            word = "남자" if sex == "남" else "여자"
            parts.append((d.col, [d.sex_codes()[sex]], plan.pick[d.no], word))
    if age_dim is not None and ages:
        try:
            codes = [code for band in ages for code in lookup._age_codes(src, tuple(band))]
        except lookup.AgeMismatch:
            return None, "질문한 연령 구간을 표의 연령 칸으로 만들 수 없음"
        den = population_codes if population_ages else age_dim.total()
        parts.append((age_dim.col, codes, den, "·".join(map(indicators.age_label, ages))))
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
    name = spec["name"]
    if population_label:
        name = f"{population_label} {_without_age_words(name)}"
    if population_sex and not qa.GENDER_WORDS[sex].search(name):
        name = f"{'남성' if sex == '남' else '여성'} {name}"
    whole_label = population_label if age_dim and col == age_dim.col and population_label else None
    out = {
        **spec,
        "name": _ratio_name(name, part),
        "unit": "%",
        "kind": "ratio",
        "formula": f"{part} ÷ {whole_label or whole.get(den, '합계')} × 100",
        "sources": [num_src],
    }
    if population_ages or (age_dim is not None and col == age_dim.col):
        # 연령 구간을 조회 조건에 넣었으므로 qa.compute가 다시 넘기지 않는다.
        out["ages_in_filters"] = True
    return out, None


def _ratio_name(name: str, part: str) -> str:
    """비율 지표 이름: "{대상} 중 {부분} 비율". 값 정의의 이름("1인가구(20세이상 인구) 수")을 그대로
    쓰면 "… 수는 13.27%"가 됐다 (11/23). 이름이 이미 비율이면 그대로 둔다.
    대상은 값 정의 이름에서 끝의 '수'와 부분 칸 이름을 뺀 것 ("1인가구 여자 수" → "1인가구")"""
    if re.search(r"비율|비중|%", name):
        return name
    subject = re.sub(r"\s*수$", "", name)
    subject = " ".join(w for w in subject.split() if _norm(w) != _norm(part)) or subject
    return f"{subject} 중 {part} 비율"


CHECKS = MemoryCache(
    "dynamic_checks", maxsize=1024, ttl=86400
)  # 표가 다시 적재돼도 하루면 다시 본다


def part_within_whole(src: dict, col: str, num: list[str], den: str | list[str]) -> str | None:
    """분자 칸(들)이 분모 칸의 부분인가: 표의 모든 지역·연도에서 분자 합 ≤ 분모.
    맞으면 None, 아니면 이유

    같은 표·같은 분류라도 칸이 서로 포함 관계가 아닐 수 있다 (예: 묶음 칸과 세부 칸이 함께 있는
    분류를 더하면 두 번 센다, 분모로 고른 칸이 합계가 아니다). 그러면 비율이 100%를 넘거나
    틀린 값이 된다.
    나머지 분류는 정의와 같이 고정한다 (연령을 따로 고르면 연령 합계)
    칸만 받은 표는 모든 지역·연도를 볼 수 없어 하지 않는다 (적재 뒤 계산, 캐시에 남기지 않음)"""
    if partial_table.active(src["table"]) is not None:
        return partial_table.CHECK_REASON
    fixed = {k: v for k, v in (src.get("filters") or {}).items() if k != col}
    if src.get("age_dim") and f"{src['age_dim']}_id" != col:
        fixed[f"{src['age_dim']}_id"] = src["age_total"]
    item = None if col == "item_id" else src["item_id"]
    fixed_key = tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in sorted(fixed.items()))
    den_key = (den,) if isinstance(den, str) else tuple(den)
    key = (src["table"], col, tuple(num), den_key, fixed_key, str(item))
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
    den_codes = [den] if isinstance(den, str) else den
    if not num_keys or not den_codes or any(code not in known for code in den_codes):
        return []
    den_keys = [known[code][0] for code in den_codes]
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
    den_marks = ", ".join("?" for _ in den_keys)
    rows = _rows(
        f"select sum(v.value) filter (where {column} in ({marks}))::float8 as num,"
        f" sum(v.value) filter (where {column} in ({den_marks}))::float8 as den"
        f" from stat_value v where v.tbl_key = ? and v.{lookup.YEARLY}"
        + "".join(f" and {c}" for c in conds)
        + f" group by {group}",
        [*num_keys, *den_keys, lookup._tbl_key(tbl_id), *params],
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
    적재 안 된 작은 표는 바로 받아 넣는다 (table_cache.fetch_now). 큰 표는 대기열에 넣고
    답에 필요한 칸만 받아 이 질문에만 쓴다 (_build_partial).
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
    population_mismatch = None
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
            tbl_id,
            card.get("org_id"),
            question,
            parsed,
            trace,
            {},
            {**slots, "table_name": card.get("tbl_nm")},
            found.get("notice"),
        )
        if res is not None:
            if (res.get("result") or {}).get("population_limit"):
                population_mismatch = population_mismatch or res
                continue
            await _remember(parsed, tbl_id, {}, res)
            return res
        if ask is not None and first_ask is None:
            first_ask = (card, ask)
    if first_ask is None:
        return population_mismatch
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
# 한 질문에서 칸 고르기 LLM을 부르는 횟수 상한 (11/18 세진). 지표가 여럿이면 지표끼리 나눠 쓴다.
# 예전에는 후보 5개를 동시에 계산하며 규칙이 실패한 표마다 불러 지표 하나에 최대 5회, 지표 여럿이면
# 15회였고, 순위가 뒤라 답으로 쓰이지 않을 표에도 불렀다 (개발셋 150: 117회 중 56회가 '표에 없음')
MAX_CELL_LLM = 3
_cell_llm_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "cell_llm_budget", default=None
)
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
        put("ages", age.get("bands") or [[age["lo"], age.get("hi")]])
        if age.get("basis"):
            q["age_group"] = {
                "expr": age.get("expr"),
                "range": " · ".join(
                    indicators.age_label(tuple(b))
                    for b in (age.get("bands") or [[age["lo"], age.get("hi")]])
                ),
                "basis": age["basis"],
            }
    if not parsed.get("level") and cond.get("region_level") in ("시도", "시군구"):
        q["level"] = cond["region_level"]
    return q, diff


def drop_guessed_year(question: str, understanding: dict, q: dict) -> bool:
    """탐색 해석에 연도가 없고 질문에도 연도 표현이 없으면 수치 해석의 연도를 지운다.
    지웠으면 True. 지우면 _with_spec이 그 표의 마지막 해로 계산한다.
    qa.correct는 질문 종류가 other면 HCX가 짐작해 넣은 연도(올해)를 지우지 않는다. 그 연도가
    남아 "지하철로 출근하거나 학교 가는 사람 수"가 2020년까지 있는 표에서
    out_of_range였다 (11/19)"""
    cond = understanding.get("conditions") or {}
    if cond.get("year") is not None or cond.get("year_end") is not None:
        return False
    if not q.get("year") or qa._says_year(question):
        return False
    q["year"] = None
    return True


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


# 쌍둥이를 몇 쌍인지 물음 ("이혼한 부부 몇 쌍"은 건수라 해당하지 않는다)
PAIRS_ASKED = re.compile(r"(쌍둥이|쌍태아)\S*\s*(\S+\s*){0,2}몇\s*쌍")
NOW_ASKED = re.compile(r"지금|현재|요즘|최근")
NOW_YEARS = 1  # "지금"을 물으면 자료 마지막 해에서 이만큼 안까지 있는 표
RECENT_YEARS = 5  # 연도를 말하지 않은 질문: 자료 마지막 해에서 이만큼 안의 자료가 있는 표만


def _recent_floor() -> int | None:
    """연도를 말하지 않은 질문에 쓸 수 있는 표의 마지막 해 하한 (주민등록인구 마지막 해 − 5)"""
    try:
        return indicators._data_years()[1] - RECENT_YEARS
    except Exception:
        return None


# 통계표 찾기가 '찾는 값이 표에 없다'고 본 조건 ("item:미성년 자녀 없는 경우").
# 표의 칸 이름과 글자가 달라 ✘가 된 경우가 있어 건너뛰지 않는다: 계산할 때 칸 반영 검사
# (uncovered)가 그 값이 고른 칸에 반영됐는지 다시 보므로, 반영되지 않으면 계산하지 않는다
ITEM_CHECK = "item:"


# 검토가 고른 표를 '모두 충족(exact)'으로 봐서 나머지 후보를 건너뛸 때의 이유. 고른 표가 계산되지
# 않으면 이 이유로 건너뛴 후보를 다시 계산한다 (compute_from_handoff,
# 11/20 D-020: 1위 표는 성별 칸을 못 골랐고 3위의 혼인 총계 표가 정답이었는데 건너뛰어
# 사전의 '수록 기간 밖' 안내로 끝났다)
EXACT_SKIP = "검토가 고른 표를 '모두 충족'으로 봄"


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
        if not ok and not _age_check(k, parsed or {}) and not k.startswith(ITEM_CHECK)
    ]
    if failed:
        return f"통계표 찾기 조건 불충족 ({', '.join(failed)})"
    if multi:
        return None
    if entry["kind"] == "other":
        if not entry.get("checks"):
            return "고른 표가 아닌데 확인한 조건이 없음"
        if picked_exact:
            return EXACT_SKIP
    return None


def handoff_notice(entry: dict, understanding: dict, combine: dict, found_notice) -> str | None:
    """답의 주의에 붙일 것: 표의 주석, 연령 이름의 기준, 답하지 못한 범위 밖 지표"""
    lines = [found_notice] if entry["kind"] == "picked" and found_notice else []
    lines += list(entry.get("notes") or [])
    age = understanding.get("age") or {}
    if age.get("basis") and age.get("lo") is not None:
        label = " · ".join(
            indicators.age_label(tuple(b))
            for b in (age.get("bands") or [[age["lo"], age.get("hi")]])
        )
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
    # 이 질문의 칸 고르기 LLM 남은 횟수 (지표가 여럿이어도 함께 쓴다)
    _cell_llm_budget.set([MAX_CELL_LLM])
    targets = h["targets"]
    target = targets[0]
    if len(targets) > 1:
        if not parsed.get("scoped_age_ratio"):
            return await multi_answer(question, parsed, found, trace)
        # 연령을 제한한 비율은 질문 전체를 한 표에 적용한다.
        target = {
            "candidates": [
                candidate
                for side in targets
                if not side.get("together")
                for candidate in side.get("candidates") or []
            ]
        }
    res = await _compute_target(question, parsed, found, target, trace)
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
    if target.get("unsaid_age") and not own.get("age_text"):
        out["age"] = None  # 비율의 전체 쪽: 질문의 연령은 부분 쪽 말의 것 (multi_indicator)
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
    if (guessed := q.get("year")) and drop_guessed_year(question, u, q):
        trace["handoff"]["year_dropped"] = guessed
    try:
        exclude = await run_in_threadpool(without_regions, list(u.get("exclude") or []))
    except Exception:
        logger.exception("제외 대상의 지역 이름 확인 실패 → 제외 대상 그대로")
        exclude = list(u.get("exclude") or [])
    entries = attempt_list(target, q)
    picked = next((e for e in entries if e["kind"] == "picked"), None)
    picked_exact = bool(picked and (picked.get("review") or {}).get("match") == "exact")
    sem = asyncio.Semaphore(MAX_FETCH_AT_ONCE)
    budget = _cell_llm_budget.get()
    if budget is None:
        budget = [MAX_CELL_LLM]
        _cell_llm_budget.set(budget)

    def context(entry: dict) -> tuple[dict, str | None]:
        """표 하나를 계산할 때 넘기는 조건 · 안내"""
        slots = {
            "conditions": u.get("conditions") or {},
            "exclude": exclude,
            "target": u.get("target"),
            # 대상 낱말 검사: 검토(LLM)가 이 표를 '모두 충족'으로 봤으면 하지 않는다 (가, 11/18).
            # 탐색 쪽 trust_exact(48a7e37)와 같은 기준. 질문은 "해외 유학생", 표는 "유학생 현황"처럼
            # 낱말이 글자로 겹치지 않아도 뜻으로 맞는 표를 막았다
            "check_target": (entry.get("review") or {}).get("match") != "exact",
            "table_name": entry.get("tbl_nm"),
            "table_words": [entry.get("tbl_nm"), entry.get("target")],
        }
        return slots, handoff_notice(entry, u, h.get("combine") or {}, found.get("notice"))

    def wants_llm(res, ask, sub: dict) -> bool:
        """규칙이 칸을 못 골라 칸 고르기 LLM이 도울 수 있는 표인가"""
        return (
            res is None
            and ask is None
            and bool(sub["dynamic"])
            and cell_pick.needs_llm(sub["dynamic"][-1].get("skip"))
        )

    async def compute(entry: dict, q_used: dict, sub: dict, llm: bool = True):
        """표 하나를 해석 q_used로 계산 → (답, 되물을 것). 기록은 sub에.
        llm=False면 규칙으로만 (칸 고르기 LLM은 뒤에서 순위대로 부른다)"""
        slots, notice = context(entry)
        res, ask = await _try(
            entry["tbl_id"], entry.get("org_id"), question, q_used, sub, {}, slots, notice, sem
        )
        if llm and wants_llm(res, ask, sub):
            res, ask = await with_cell_llm(entry, q_used, sub, slots, notice)
        if sub["dynamic"]:
            sub["dynamic"][0]["kind"] = entry["kind"]
        return res, ask

    async def with_cell_llm(entry: dict, q_used: dict, sub: dict, slots: dict, notice):
        """규칙이 칸을 못 고른 표: LLM이 고른 칸(forced)으로 다시 계산한다 (cell_pick.py).
        이 질문의 상한(MAX_CELL_LLM)을 다 썼으면 부르지 않는다"""
        if budget[0] <= 0:
            if sub["dynamic"]:
                sub["dynamic"][-1]["cell_llm_skip"] = f"질문당 칸 고르기 LLM 상한({MAX_CELL_LLM}회)"
            return None, None
        tbl_id = entry["tbl_id"]
        try:
            dims, region_dim, _ = await run_in_threadpool(schema, tbl_id)
            groups = llm_groups(question, dims, region_dim)
        except Exception:
            logger.exception("칸 고르기 LLM에 보낼 칸 만들기 실패: %s", tbl_id)
            return None, None
        from app.core.config import get_settings

        if groups and get_settings().handoff_cell_llm:
            budget[0] -= 1  # 실제로 LLM을 부르는 경우만 센다
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
        slots_llm = {
            **slots,
            "read_as": picked.read_as,
            "check_target": False,
            "cells_by_llm": True,
        }
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

    said_year = any(q.get(k) for k in ("year", "start_year", "end_year"))
    # "2015년 대비 지금": 시작 해는 말했어도 끝은 지금이다.
    # 최근 자료가 있는 후보가 있으면 옛 표는 뺀다
    now_asked = bool(NOW_ASKED.search(question)) and not q.get("year")
    recent = None if said_year and not now_asked else await run_in_threadpool(_recent_floor)
    if recent and now_asked:
        recent += RECENT_YEARS - NOW_YEARS  # "지금"은 마지막 해에 가까운 표만
    if recent and now_asked and not any((e.get("end_year") or 0) >= recent for e in entries):
        recent = None  # 최근 자료가 있는 후보가 없으면 있는 표로 답한다 (안내 대신)

    async def attempt(entry: dict, exact: bool):
        sub: dict = {"dynamic": []}
        tbl_id = entry["tbl_id"]
        if why := skip_reason(entry, exact, q, multi):
            sub["dynamic"].append({"tbl_id": tbl_id, "kind": entry["kind"], "skip": why})
            return None, None, sub
        if recent and entry.get("end_year") and entry["end_year"] < recent:
            # 연도를 말하지 않았는데 최근 자료가 없는 옛 표 ("이혼 몇 건이나 돼?" → 1980년 표의
            # 마지막 해로 답했다). 연도를 말하지 않으면 최신 연도를 묻는 것이다
            said = "지금을 물었는데" if now_asked else "연도를 말하지 않았는데"
            why = f"{said} {entry['end_year']}년까지인 표"
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
        res, ask = await compute(entry, q, sub, llm=False)
        return res, ask, sub

    def merge(sub: dict) -> None:  # 표마다 따로 남긴 기록을 순서대로 합친다
        trace["dynamic"].extend(sub["dynamic"])
        if sub.get("fetched"):
            trace.setdefault("fetched", []).extend(sub["fetched"])

    def is_ok(res) -> bool:
        return bool(res) and res.get("status") == "ok"

    async def run_round(idx: list[int], exact: bool) -> list:
        """entries[idx]를 계산한다.
        1. 후보를 규칙으로만 동시에 계산한다 (LLM 없음)
        2. 칸 고르기 LLM은 순위대로 하나씩. 답은 '계산된 표 중 순위가 가장 앞인 것'이라, 규칙으로
           계산된 첫 표보다 뒤의 표는 LLM으로 계산돼도 쓰이지 않는다
           → 그 앞의 표만, 계산되면 멈춘다"""
        with partial_table.shared_tables():
            outs = list(await asyncio.gather(*(attempt(entries[i], exact) for i in idx)))
            first_ok = next((k for k, (res, _, _) in enumerate(outs) if is_ok(res)), len(outs))
            for k in range(first_ok):
                res, ask, sub = outs[k]
                if not wants_llm(res, ask, sub):
                    continue
                slots, notice = context(entries[idx[k]])
                res, ask = await with_cell_llm(entries[idx[k]], q, sub, slots, notice)
                outs[k] = (res, ask, sub)
                if is_ok(res):
                    break
        return outs

    outcomes = await run_round(list(range(len(entries))), picked_exact)
    if picked_exact and not any(is_ok(res) or ask is not None for res, ask, _ in outcomes):
        # 검토가 '모두 충족'으로 고른 표가 계산되지 않았다 (칸을 못 고름 · 값 없음 등):
        # 그 이유로 건너뛴 후보를 다시 계산한다. ✘ · 확인한 조건 없음 등 다른 건너뜀은 그대로
        again = [
            i
            for i, (_, _, sub) in enumerate(outcomes)
            if (sub["dynamic"] or [{}])[0].get("skip") == EXACT_SKIP
        ]
        for i, out in zip(again, await run_round(again, False), strict=True):
            if out[2]["dynamic"]:
                out[2]["dynamic"][0]["retry"] = "고른 표가 계산되지 않아 다시 계산"
            outcomes[i] = out
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

    for res, _, _ in outcomes:
        if res and (res.get("result") or {}).get("population_limit"):
            return res  # 맞는 모집단이 없으면 다른 기준의 비율 대신 범위를 안내한다.

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


AGE_LIMIT = re.compile(r"(\d+)\s*세\s*이상")  # 표 이름의 집계 대상 연령 ("1인가구(20세 이상, …)")
WHOLE_POPULATION = re.compile(
    r"전\s*연령|모든\s*연령|연령\s*(?:제한|구분)\s*없이"
    r"|\b전체\b(?!\s*(?:기간|연도|시점|지역|시도|시군구))"
)


def _checks_population(question: str, kind: str | None) -> bool:
    return kind == "composition" or bool(
        PCT_ASK.search(question) or WHOLE_POPULATION.search(question)
    )


def age_limit(tbl_nm: str, question: str, kind: str | None = None) -> str | None:
    """비율·구성비 또는 전체 집계에 필요한 연령을 표가 모두 담지 못하면 그 제한."""
    if not _checks_population(question, kind):
        return None
    m = AGE_LIMIT.search(tbl_nm or "")
    if m is None:
        return None
    # "1인 가구 중 20세 이상 비율"의 20세는 분자의 조건이다. 분모의 범위만 본다.
    scope = WITHIN.split(question, maxsplit=1)
    population = scope[0] if len(scope) == 2 else "" if PCT_ASK.search(question) else question
    bands = qa.resolved_ages(population)
    return None if bands and all(lo >= int(m[1]) for lo, _ in bands) else m[0]


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
    if not plans:
        flow.add("계산 방법 캐시", "없음" if found else "건너뜀", (found or ["", ""])[1])
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
            steps[-1].setdefault("skip", f"캐시한 표로 답하지 못함 ({res.get('status')})")
            continue
        if res is None and ask is not None:
            res = ask_answer(question, parsed, plan["tbl_id"], plan["tbl_id"], ask, forced)
        if res is not None:
            trace["plan_cache"] = {"hit": plan["id"], "tbl_id": plan["tbl_id"]}
            used = f"전에 계산한 표 {plan['tbl_id']} (통계표 찾기 건너뜀)"
            flow.add("계산 방법 캐시", "씀", used)
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
    """표 하나로 계산해 보고 처리 흐름에 남긴다 (_try_once)"""
    res, ask = await _try_once(
        tbl_id, org_id, question, parsed, trace, forced, slots, notice, fetch_sem
    )
    step = (trace.get("dynamic") or [{}])[-1]
    if res is not None:
        outcome = res.get("status")
    else:
        outcome = "되묻기" if ask is not None else "못 함"
    how = "칸 고르기 LLM" if step.get("by") == "cell_llm" else "규칙"
    parts = [tbl_id, f"칸={step['cells']} ({how})" if step.get("cells") else ""]
    if got := step.get("partial"):  # 적재 안 된 큰 표에서 칸만 받아 계산 (채점·요청 기록에서 센다)
        parts.append(f"칸만 바로 받음 (요청 {got['requests']}번)")
    parts.append(step.get("skip") or "")
    flow.add("표 칸으로 계산", outcome, " · ".join(p for p in parts if p))
    return res, ask


async def _try_once(
    tbl_id, org_id, question, parsed, trace, forced, slots=None, notice=None, fetch_sem=None
):
    """표 하나로 계산해 본다 → (답 또는 None, 되물을 것 또는 None)
    slots: 통계표 찾기의 조건 (칸 반영 검사), notice: 통계표 찾기의 주의 (답에 붙인다)
    fetch_sem: 적재 안 된 표를 KOSIS에서 동시에 받는 수 제한 (꾸러미로 여러 표를 계산할 때)"""
    step = {"tbl_id": tbl_id, **({"forced": forced} if forced else {})}
    trace["dynamic"].append(step)
    if _checks_population(question, kind := parsed.get("type")):
        tbl_nm = (slots or {}).get("table_name")
        if not tbl_nm:
            try:
                tbl_nm = (await run_in_threadpool(lookup._table, tbl_id))["tbl_nm"]
            except Exception:
                tbl_nm = ""
        if limit := age_limit(tbl_nm, question, kind):
            step["skip"] = f"{limit}만 집계해 질문의 모집단과 다름"
            return population_limit_answer(parsed, tbl_id, org_id, tbl_nm, limit), None
    extra = {"fetch_sem": fetch_sem} if fetch_sem is not None else {}
    spec, why = await _build(tbl_id, org_id, question, parsed, step, forced, slots, **extra)
    if isinstance(why, BandMiss):
        step["skip"] = str(why)
        return await run_in_threadpool(band_answer, parsed, tbl_id, why), None
    if spec is None:
        step["skip"] = str(why)
        return None, why if isinstance(why, Ask) else None
    step["cells"] = spec["cells"]
    notice = table_notice(notice, bool(spec.get("couple")))
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
    y0, y1 = parsed.get("start_year"), parsed.get("end_year")
    # 기간의 합 ("최근 5년간 외국인이랑 결혼한 거 다 합치면"): 칸마다 그 기간을 모두 더한다
    years = list(range(y0, y1 + 1)) if op["op"] == "sum" and y0 and y1 and y0 < y1 else [year]
    rows, first = [], None
    for code, label in zip(op["codes"], op["labels"], strict=True):
        total_of_code = 0
        for y in years:
            r = lookup.value(_set_code(spec, op["col"], code), region, y, sex)
            if r.get("status") != "ok":
                return {"status": r.get("status"), "answer": r.get("reason") or "", "result": r}
            first = first or r
            if r.get("value") is None:  # 값이 없는 칸 (비공개 · 결측): 그 칸은 값 없음으로
                total_of_code = None
                break
            total_of_code += r["value"]
        rows.append({"label": label, "value": total_of_code})
    unit, name = first.get("unit") or "", spec["name"]
    when = f"{years[0]}~{years[-1]}년" if len(years) > 1 else f"{year}년"
    where = f"{when} {first.get('region') or region}"
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
    if spec.get("answer_note"):  # 추계 표: 예상값이라는 것을 첫 문장 바로 뒤에 (qa._summary와 같음)
        text += " " + spec["answer_note"]
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


def population_limit_answer(
    parsed: dict, tbl_id: str, org_id: str | None, tbl_nm: str, limit: str
) -> dict:
    reason = (
        f"'{tbl_nm}' 표는 {limit}만 집계합니다. "
        "질문의 모집단을 모두 포함하는 자료를 확인할 수 없어 값을 계산할 수 없습니다."
    )
    result = {
        "status": "band_mismatch",
        "reason": reason,
        "population_limit": limit,
        "source": tbl_nm,
        "basis": f"{limit} 집계",
        "tbl_id": tbl_id,
        "url": indicators.KOSIS_URL.format(org_id=org_id, tbl_id=tbl_id) if org_id else None,
        "method": "dynamic",
    }
    return {
        "status": result["status"],
        "answer": reason,
        "result": result,
        "parsed": parsed,
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


PARTIAL_OUTCOMES = ("too_big", "busy")  # 표 전체를 바로 받지 못해 칸만 받아 보는 경우


async def _build(tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None):
    """fetch_sem: 후보 여러 개를 동시에 계산할 때 KOSIS에서 받는 일을 동시에 몇 개까지 할지"""
    if partial_table.active(tbl_id) is not None:  # 이 질문에서 이미 칸만 받은 표 (칸 고르기 LLM)
        return await _build_partial(
            tbl_id, org_id, question, parsed, step, forced, slots, fetch_sem
        )
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
    except StatsTableMissing:
        pass
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"
    # 적재 안 된 표: 작은 표는 지금 받아 넣는다. 못 받으면 대기열에 넣고,
    # 큰 표는 답에 필요한 칸만 받아 계산해 본다 (_build_partial). 그것도 못 하면 이번에는 표 안내
    try:
        org_id = org_id or (await load_queue.table_info(tbl_id))["org_id"]
        if fetch_sem is None:
            outcome = await table_cache.fetch_now(tbl_id, org_id, step)
        else:
            async with fetch_sem:
                outcome = await table_cache.fetch_now(tbl_id, org_id, step)
        if outcome != "loaded":
            await load_queue.enqueue(tbl_id, "question")
            queued = f"적재 안 됨 ({outcome}, 대기열에 넣음)"
            if outcome in PARTIAL_OUTCOMES and table_cache.PARTIAL_FETCH:
                return await _build_partial(
                    tbl_id, org_id, question, parsed, step, forced, slots, fetch_sem, queued
                )
            return None, queued
    except Exception:
        logger.exception("동적 지표 표 받기 실패: %s", tbl_id)
        return None, "표를 받지 못함"
    try:
        return await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"


async def _build_partial(
    tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None, queued=""
):
    """적재 안 된 큰 표: 메타로 칸을 고르고, 고른 칸만 KOSIS에서 받아 정의를 만든다
    (partial_table.py). 받은 값은 이 질문에서만 쓰고, 표 전체 적재는 대기열이 맡는다 (_build가 넣음)

    1차 build(값 없이 칸만 고름) → 고른 칸만 요청 → 2차 build(받은 값으로 단위 등을 정함).
    칸을 고르지 못하면 그 이유를 그대로 돌려준다 (되묻기 · 칸 고르기 LLM이 지금처럼 이어 간다).
    비율처럼 표 전체로 검증해야 하는 계산은 build가 멈춘다 (partial_table.CHECK_REASON)
    step["partial"]: 이번에 보낸 요청 수, 받아 둔 행 수, 걸린 시간 (ms)"""
    start = time.perf_counter()
    queued = queued or "적재 안 됨"
    try:
        part = partial_table.active(tbl_id)
        if part is None:
            org_id = org_id or (await load_queue.table_info(tbl_id))["org_id"]
            meta = await table_cache.table_meta(tbl_id, org_id)
            part = await run_in_threadpool(partial_table.from_meta, tbl_id, org_id, meta)
            partial_table.register(part)
        spec, why = await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
        if spec is None:
            return None, why
        planning = parsed
        if parsed.get("type") == "trend" and not parsed.get("year") and not spec.get("parts_op"):
            # qa._compute가 추이의 끝 연도를 정할 때 쓰는 기본 연도와 맞춘다.
            _, latest = await run_in_threadpool(indicators._data_years)
            planning = {**parsed, "year": latest}
        fetch = partial_table.requests_for(part, spec, planning)
        if fetch is None:
            return None, f"{queued} · 칸이 많아 칸만 받지 못함"
        async with fetch_sem or contextlib.nullcontext():
            sent = await table_cache.fetch_cells(part, fetch)
        spec, why = await run_in_threadpool(build, tbl_id, question, parsed, forced, slots)
    except partial_table.Unsupported as e:
        return None, f"{queued} · 칸만 받지 못함 ({e})"
    except Exception as e:  # noqa: BLE001 - 칸만 받기가 실패해도 지금처럼 표 안내로 끝낸다
        from app.services.kosis_client import mask_key

        step["partial_error"] = mask_key(f"{type(e).__name__}: {e}")[:300]
        logger.warning("%s: 칸만 받기 실패: %s", tbl_id, step["partial_error"])
        return None, f"{queued} · 칸만 받기 실패"
    ms = round((time.perf_counter() - start) * 1000)
    step["partial"] = {"requests": sent, "rows": len(part.rows), "ms": ms}
    return spec, why


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
