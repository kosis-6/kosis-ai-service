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
    agegroup,
    alternative,
    band,
    cell_pick,
    flow,
    indicators,
    load_queue,
    lookup,
    partial_table,
    plan_cache,
    progress,
    qa,
    search,
    search_answer,
    table_cache,
)
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

SAFE_TOTAL = re.compile(
    r"(?<!특)성별|연령|각세|나이|국적|교육정도|혼인상태|가구원수|세대구성|거처|점유형태|사용방수|직업|산업|"
    r"경제활동|종사상지위|체류자격|체류기간|출산순위|자녀|전입지|전출지|현거주지|통근통학지|"
    r"거주외국인|외국인(아내|남편)"
)
NOTE = "지표 사전에 없는 값을 통계표에서 바로 계산했습니다. 공식 지표 정의와 다를 수 있습니다"
OTHER_TABLE_NOTE = "지표 사전의 표에 없는 값이라 같은 통계의 다른 통계표에서 바로 꺼냈습니다"
PROJECTION_NOTE = "장래추계 표의 추계값입니다(실적 아님). 다음 추계 때 바뀔 수 있습니다"
PROJECTION_ANSWER = "장래추계에 따른 예상값으로, 실제 통계가 아닙니다."
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총", "전국"}
SEX_NAMES = {"남": {"남자", "남", "남성", "남편"}, "여": {"여자", "여", "여성", "아내"}}
COUNT_UNITS = {"명", "가구", "건", "개", "세대", "호", "곳", "개소", "천명", "천가구"}
SAME_WORDS = {"출퇴근": "통근", "출근": "통근"}
UNIT_TAIL = re.compile(r"\s*\([^()]*\)\s*$")
SCHOOL_GOING = re.compile(r"(?:^|(?<=\s))학교\s*(?:에\s*)?(?:가는|가고|간|다니는|다니고|다닌)")
MOVE_CONTEXT = re.compile(
    r"출근|출퇴근|통근|지하철|전철|버스|자가용|승용차|자전거|타고|걸어서|교통"
)
BIRTH_WORD = r"출생(?:한|해|하여)?(?![가-힣]|\s*아(?:\s|$))"
BIRTHPLACE_AFTER = re.compile(
    rf"\s*(?:출신|{BIRTH_WORD}|태생|에서\s*(?:\S+\s+)?(?:태어|{BIRTH_WORD}))"
)
BIRTHPLACE_BEFORE = re.compile(r"고향(?:은|이)?\s*$")
BIRTHPLACE_AXIS = re.compile(r"출생지|태어난\s*곳")
PLACE_AFTER = re.compile(
    r"\s*(?:에서\s*(?:\S+\s+)?(?:온|왔|오는|이사|이주|이동|전출|옮|떠난)"
    r"|(?:으로|로)\s*(?:\S+\s+)?(?:간|가는|갔|이사|이주|이동|전입|옮|통근|통학|출퇴근|출근|등교|다니)"
    r"|에\s*(?:살던|살았던|거주하던|있던))"
)
PLACE_BEFORE = re.compile(r"(?:년\s*전(?:에)?|전\s*거주지(?:가|는)?)\s*$")
NOT_PLACE_WORDS = {"전체"}
PLACE_REST = re.compile(
    r"[()\[\]\-·_,./]|직할시|특별시|광역시|특별자치시|특별자치도|시부|군부|구부|동부|읍부|면부|미상"
    r"|합계|소계|계|이외|외|지역|도|시|군|구"
)
TOTAL_SUFFIX = re.compile(r"(계|합계|소계|전체)$")
PCT_ASK = re.compile(r"%|퍼센트|비율|비중|몇\s*프로")
HOUSEHOLD_ASK = re.compile(
    r"가구\s*(?:[은는이가을를]\s*)?(?:수|몇|얼마|[?.!]|$)"
    r"|가구\s*[은는이가을를](?=\s)|몇\s*가구"
)
PERSON_ASK = re.compile(r"가구원|몇\s*명|사람|인구(?!\s*총조사)|평균|구성비|%|퍼센트|비율|비중")
HOUSEHOLD_CELL = re.compile(r"가구(?:\s*수)?$")
POPULATION_TARGET = re.compile(r"인구(?:\s*수)?$")
PERSON_UNIT_TAIL = re.compile(r"\s*\(\s*(?:천\s*)?명\s*\)$")
HOUSEHOLD_UNITS = {"명": "가구", "천명": "천가구"}
WITHIN = re.compile(r"\s+(?:중(?:에?서|에)?|가운데)\s+")
NORM = re.compile(r"[\s·,.()\[\]{}<>\"'‘’“”_/-]")


def _norm(text: str) -> str:
    return NORM.sub("", str(text))


@dataclass
class Dim:
    no: int
    name: str
    codes: dict[str, str]

    @property
    def col(self) -> str:
        return "item_id" if self.no == 0 else f"c{self.no}_id"

    def total(self) -> str | None:
        found = [
            c for c, n in self.codes.items()
            if _norm(n) in TOTAL_NAMES or _norm(UNIT_TAIL.sub("", n)) in TOTAL_NAMES
        ]  # fmt: skip
        return found[0] if len(found) == 1 else None

    def default(self) -> str | None:
        names = {c: _norm(n) for c, n in self.codes.items()}
        whole = [c for c, n in names.items() if n == "총인구"]
        if len(whole) == 1 and set(names.values()) - {"총인구"} <= {"내국인", "외국인"}:
            return whole[0]
        if any(n.startswith(("고위", "저위")) for n in names.values()):
            middle = [c for c, n in names.items() if n.startswith("중위")]
            if len(middle) == 1:
                return middle[0]
        return None

    def sex_codes(self) -> dict[str, str]:
        out = {}
        groups = {_norm(_label_name(n)) for n in self.codes.values() if is_subtotal(n)}
        for sex, names in SEX_NAMES.items():
            hits = [c for c, n in self.codes.items() if _norm(n) in names]
            if not hits and groups:
                hits = [
                    c
                    for c, n in self.codes.items()
                    if len(part := re.split(r"[_\-]", n.strip())) >= 2
                    and _norm(part[-1]) in names
                    and _norm(" ".join(part[:-1])) in groups
                ]
            if len(hits) == 1:
                out[sex] = hits[0]
        return out if len(out) == 2 else {}

    def is_age(self) -> bool:
        ages = [n for n in self.codes.values() if lookup.parse_age(n)]
        return len(ages) >= 3 and len(ages) * 2 >= len(self.codes)


@dataclass
class Plan:
    pick: dict[int, str] = field(default_factory=dict)
    matched: dict[int, str] = field(default_factory=dict)
    defaults: list[str] = field(default_factory=list)
    sex_dim: Dim | None = None
    age_dim: Dim | None = None
    multi: tuple[int, list[str], str] | None = None
    multi_dim: Dim | None = None
    couple: bool = False
    split_sex: dict[str, str] | None = None
    # 숫자 구간 축의 구간 합 (band.py): 분류 번호 → (칸 코드들, 답에 쓰는 말).
    # 건수면 합, 비율이면 합 ÷ 합계
    bands: dict[int, tuple[list[str], str]] = field(default_factory=dict)


def schema(tbl_id: str) -> tuple[list[Dim], int | None, str]:
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
        longest = _rows(
            "select max(length(region_code)) as n from region_code_map"
            " where tbl_id = ? and src_dim = ? and region_code <> '00'",
            [tbl_id, region_dim],
        )[0]["n"]
        if longest:
            level = "시군구" if longest >= 5 else "시도"
    return dims, region_dim, level


def is_projection(tbl_id: str) -> bool:
    if lookup._projection(tbl_id):
        return True
    r = _rows("select survey_name, table_name from table_meta where tbl_id = ?", [tbl_id])
    return bool(r and search.PROJECTION_TABLE.search(f"{r[0]['survey_name']} {r[0]['table_name']}"))


def _asks_future(q: dict, after: int) -> bool:
    years = [y for y in (q.get("year"), q.get("start_year"), q.get("end_year")) if y]
    return bool(years) and min(years) > after


def _labels(tbl_id: str, dim_no: int) -> list[tuple[str, str]]:
    return sorted((code, name) for code, (_, name) in lookup._codes(tbl_id, dim_no).items())


def _unit(
    tbl_id: str, item: str, cells: dict[int, list[str]] | None = None
) -> tuple[str | None, bool]:
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


NAME_UNIT = re.compile(r"\(\s*(백만원|천만원|만원|천원|원|%|세|㎡|m²|평)\s*\)\s*$")


def _name_unit(plan: Plan, dims: list[Dim], unit: str) -> str:
    if unit not in COUNT_UNITS:
        return unit
    for d in sorted(dims, key=lambda d: d.no):
        if (m := NAME_UNIT.search(d.codes.get(plan.pick.get(d.no, ""), ""))) is not None:
            return m[1]
    return unit


def _unit_cells(plan: Plan, dims: list[Dim], region_dim: int | None) -> dict[int, list[str]]:
    skip = {0, region_dim, plan.age_dim.no if plan.age_dim else None}
    cells = {d.no: [plan.pick[d.no]] for d in dims if d.no not in skip and d.no in plan.pick}
    if plan.multi and plan.multi[0] not in skip:
        cells[plan.multi[0]] = list(plan.multi[1])
    return cells


COUNT_CLASS = re.compile(r"^\d+\s*(?:자녀|명|인|세대)")
COUNTED_UNITS = ("가구", "인구", "세대")


def table_words(question: str) -> str:
    if MOVE_CONTEXT.search(question):
        question = SCHOOL_GOING.sub("통학", question)
    q = _norm(agegroup.count_digits(question))
    for word, same in SAME_WORDS.items():
        q = q.replace(word, same)
    return q


def _is_place_cell(name: str) -> bool:
    n = _norm(name)
    if n == "전국":
        return True
    try:
        found = [f for f in indicators.find_regions(name) if f not in NOT_PLACE_WORDS]
    except Exception:
        logger.warning("지역 이름 확인 실패 → 지역 칸 검사 건너뜀: %s", name)
        return False
    for f in sorted(found, key=len, reverse=True):
        n = n.replace(f, "")
    return bool(found) and not PLACE_REST.sub("", n)


@dataclass(frozen=True)
class PlaceMention:
    name: str
    role: str | None
    phrase: str


def _place_mentions(question: str) -> list[PlaceMention]:
    try:
        mentions = indicators.region_mentions(question)
    except Exception:
        return []
    out = []
    for i, (start, end, name) in enumerate(mentions):
        before = question[:start]
        next_start = mentions[i + 1][0] if i + 1 < len(mentions) else len(question)
        after = BIRTHPLACE_AFTER.match(question, end, next_start)
        prefix = BIRTHPLACE_BEFORE.search(before)
        role = "birthplace" if after or prefix else None
        if role is None:
            after = PLACE_AFTER.match(question, end)
            prefix = PLACE_BEFORE.search(before)
            role = "other" if after or prefix else None
        if role:
            end = after.end() if after else end
            while end < len(question) and not question[end].isspace():
                end += 1
        out.append(PlaceMention(name, role, question[prefix.start() if prefix else start : end]))
    return out


def _other_places(question: str) -> list[str]:
    return [mention.name for mention in _place_mentions(question) if mention.role]


def _named_place(cell: str, places: list[str]) -> bool:
    if not places:
        return False
    cell_names = [_norm(f) for f in indicators.find_regions(cell) if f not in NOT_PLACE_WORDS]
    return any(c.startswith(p) or p.startswith(c) for c in cell_names for p in map(_norm, places))


def _drop_region_words(
    question: str,
    hits: dict[int, list[str]],
    dims: list[Dim],
    region_dim: int | None,
    subject: str = "",
) -> dict[int, list[str]]:
    if region_dim is None:
        return hits
    by_no = {d.no: d for d in dims}
    places = _other_places(question)

    def allowed(d: Dim | None, code: str) -> bool:
        return (
            d is None
            or d.no == region_dim
            or not _is_place_cell(d.codes[code])
            or _named_place(d.codes[code], places)
        )

    out = {}
    for no, codes in hits.items():
        d = by_no.get(no)
        kept = [c for c in codes if allowed(d, c)]
        if not kept and d is not None:
            kept = [c for c in _word_hits(question, d, subject) if allowed(d, c)]
        if kept:
            out[no] = kept
    return out


def _place_cell_unasked(question: str, d: Dim, code: str, region_dim: int | None) -> bool:
    return (
        region_dim is not None
        and d.no != region_dim
        and code != d.total()
        and _is_place_cell(d.codes[code])
        and not _named_place(d.codes[code], _other_places(question))
    )


def _text_hits(
    question: str, dims: list[Dim], skip: set[int], subject: str = ""
) -> dict[int, list[str]]:
    q = table_words(question)
    spans = []
    for d in dims:
        if d.no in skip:
            continue
        for code, name in d.codes.items():
            n = _norm(name)
            if len(n) < 2 or n in TOTAL_NAMES:
                continue
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
        if found := _word_hits(question, d, subject):
            hits[d.no] = found
    return hits


MAX_WORD_CELLS = 2
GENERIC_WORDS = {
    "인구", "이상", "이하", "미만", "전체", "합계", "소계", "기타", "가구", "건수", "사람",
}  # fmt: skip
JOSA_TAIL = re.compile(
    r"(이랑|랑|하고|와|과|이나|나|으로|로|에서|에|의|은|는|이|가|을|를|도|만|중에|중|들)$"
)


def _word_hits(question: str, d: Dim, subject: str = "") -> list[str]:
    """질문 낱말이 칸 이름에 들어 있으면 그 칸을 말한 것으로 본다.
    표 이름(subject)에도 있는 낱말은 표의 주제다. 그런 낱말은 다음일 때 칸을 고른 말로 보지 않는다
    - 칸 여러 개에 걸림: 주제를 말한 것 ('한국' → '중국(한국계)' · '러시아(한국계)',
      '통근' → '통근·통학 인구' · '통근·통학버스'). 칸 이름과 똑같은 칸만 남긴다
    - 분류 칸에서 더 긴 낱말의 일부로만 걸림: 주제와 다른 하위 분류다
      ('외국인' → '외국인근로자', '주민' → '주민등록인구', '국적' → '무국적', '연령' → '연령미상')
    분류 칸 이름에 낱말 그대로 있거나('혼인' → '미혼→혼인 변화') 지역 칸이면('인천' → '인천광역시',
    지역 규칙이 맡음) 센다. 항목(0번)은 '주제 + 단위' 이름이 흔해 하나에만 걸리면 센다
    ('이혼' → '이혼건수', '다문화가구' → '다문화가구 현황')"""
    topic = _norm(subject)
    words = []
    for token in question.split():
        w = _norm(JOSA_TAIL.sub("", token))
        if len(w) >= 2 and w not in GENERIC_WORDS and not w.isdigit():
            words.append(w)
    whole = {d.total()} | {
        c for c, n in d.codes.items() if is_subtotal(n) or _norm(n).endswith("전체")
    }
    cells = [(c, _norm(n)) for c, n in d.codes.items() if c not in whole]
    out: list[str] = []
    for w in words:
        found = [c for c, n in cells if w in n or (len(w) >= 4 and w[:2] in n and w[-2:] in n)]
        if topic and w in topic:
            if len(found) > 1:
                found = [c for c in found if _norm(d.codes[c]) == w]
            elif d.no != 0:
                found = [c for c in found if _as_word(w, d.codes[c]) or _is_place_cell(d.codes[c])]
        if len(found) <= MAX_WORD_CELLS:
            out += [c for c in found if c not in out]
    return out


def _as_word(word: str, name: str) -> bool:
    """칸 이름에 word가 낱말 그대로 있는지
    ('미혼→혼인 변화'의 '혼인'은 예, '외국인근로자'의 '외국인'은 아니오)"""
    return word in {_norm(part) for part in re.split(r"[^0-9A-Za-z가-힣]+", name) if part}


@dataclass
class Ask:
    dim_no: int
    dim_name: str
    options: list[tuple[str, str]]
    forced: dict[int, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.dim_name}에서 칸을 고르지 못함 (후보 {len(self.options)}개)"


MAX_ASK_OPTIONS = 8


SUM_WORDS = re.compile(r"합|합계|합친|더한|더하면|모두|전부|또는|이거나|(?<=[가-힣])(이)?나\s")
COMPARE_WORDS = re.compile(
    r"비교|차이|몇\s*배|보다|대비|vs|더\s*(많|적|높|낮)|어느\s*(게|쪽|것)|뭐가\s*더|뭘\s*더", re.I
)
RANK_PARTS = re.compile(
    r"top\s*\d|상위\s*\d|순위|(가장|제일)\s*(많|적|높|낮)|1위|몇\s*위|(많|높|적|낮)은\s*(순서|순)",
    re.I,
)
REGION_RANK = re.compile(r"시도|시군구|지역|곳|어디|구별|도시|광역시")
TOTAL_TAIL = re.compile(r"(?:[\s\-·_:]+계|(?<=[주인타])계|[\s\-·_:]*(?:합계|소계|총계))$")
TERM_TOTAL = re.compile(r"[_\-:]\s*(?:계|합계|소계)(?=\s|$)")


def _table_target(title: str) -> str:
    title = re.sub(r"\([^()]*\)", "", title)
    title = re.sub(r"(?:\s*[-–—_／/]\s*(?:시군구|시도|읍면동|전국)(?:별)?)+\s*$", "", title)
    return title.rsplit("별", 1)[-1].strip()


def counts_households(target: str, plan: "Plan", item: "Dim") -> bool:
    if not HOUSEHOLD_CELL.search(target):
        return False
    codes = plan.multi[1] if plan.multi and plan.multi[0] == item.no else [plan.pick[item.no]]
    return all(
        HOUSEHOLD_CELL.search(_label_name(PERSON_UNIT_TAIL.sub("", item.codes[c].strip())))
        for c in codes
    )


def is_subtotal(label: str) -> bool:
    return bool(TOTAL_TAIL.search(label.strip()))


def _label_name(label: str) -> str:
    name = TOTAL_TAIL.sub("", label.strip()) or label
    return " ".join(name.replace("_", " ").split()) or label


def _term_name(term: str) -> str:
    name = TERM_TOTAL.sub("", indicators.without_national(term))
    return " ".join(name.replace("_", " ").split()) or term


def _multi_op(question: str) -> str | None:
    if COMPARE_WORDS.search(question):
        return "compare"
    if SUM_WORDS.search(question):
        return "sum"
    return None


def _leaf_codes(d: Dim) -> list[str]:
    return [c for c, n in d.codes.items() if c != d.total() and not is_subtotal(n)]


RANK_AXIS_WORDS = {"국적": re.compile(r"어느\s*(?:나라|국가)|무슨\s*나라|나라\s*별|국가\s*별")}


# 순위 축으로 말한 일상어 → 표 분류 이름의 어간 (11/24, 적재된 표의 분류 이름에서 고름).
# 글자가 겹치는 말(국적 · 직업 · 산업 · 종교 · 전입지 · 사유 …)은 포함 매칭으로 맞으니 적지 않는다.
# 여기 없는 말이 들어오면 _rank_dim 이 "축에 맞는 분류 없음"을 남기고 지역 순위로 떨어지지 않는다
AXIS_SYNONYMS = {
    "나라": "국적",
    "국가": "국적",
    "어느나라": "국적",
    "이동사유": "전입사유",
    "이사이유": "전입사유",
    "이사한이유": "전입사유",
    "이혼이유": "이혼사유",
    "학력": "교육정도",
    "학벌": "교육정도",
    "교육수준": "교육정도",
    "직종": "직업",
    "일자리종류": "직업",
    "업종": "산업",
    "식구수": "가구원수",
    "가족수": "가구원수",
    "집종류": "거처의종류",
    "사는집형태": "거처의종류",
    "집형태": "거처의종류",
    "비자": "체류자격",
    "체류자격": "체류자격",
    "결혼상태": "혼인상태",
    "혼인여부": "혼인상태",
    "몇째": "출산순위",
    "출산순서": "출산순위",
    "자녀수": "출생자녀수",
    "아이수": "출생자녀수",
    "집소유": "점유형태",
    "자가전세": "점유형태",
}
AXIS_UNMATCHED = "축 '{axis}'에 맞는 분류가 표에 없음"


def _axis_words(axis: str) -> set[str]:
    """축 말 자체와 동의어 표의 어간 (띄어쓰기 · '별' 제거)"""
    a = _norm(re.sub(r"(별|구분)$", "", axis))
    return {a, _norm(AXIS_SYNONYMS.get(a, a))}


def _rank_dim(
    question: str, dims: list[Dim], region_dim: int | None, tbl_nm: str, axis: str | None = None
) -> Dim | None:
    """분류 안의 순위를 묻는가: 그 분류. 지역 순위("가장 많은 시도")는 아니다.
    axis: 질문 이해(rank_axis) · 재계획(combine.axis)이 정한 분류 이름. 있으면 그 이름과 동의어로
    고른다 ("어느 나라 사람이 제일 많아" → '나라' → 국적(지역)별). 못 찾으면 None 을 돌려주고,
    choose 가 지역 순위로 떨어지는 대신 AXIS_UNMATCHED 로 계산을 멈춘다"""
    if axis:
        words = _axis_words(axis)
        for d in dims:
            stem = _norm(re.sub(r"(별|구분)$", "", re.sub(r"\(.*?\)", "", d.name)))
            if d.no not in (0, region_dim) and stem and any(w in stem or stem in w for w in words):
                return d
        return None
    if not RANK_PARTS.search(question) or REGION_RANK.search(question):
        return None
    q = _norm(question)
    for d in dims:
        stem = _norm(re.sub(r"(별|구분)$", "", re.sub(r"\(.*?\)", "", d.name)))
        if d.no not in (0, region_dim) and len(stem) >= 2 and stem in q:
            return d
    for axis, pattern in RANK_AXIS_WORDS.items():
        if pattern.search(question):
            hit = [d for d in dims if d.no not in (0, region_dim) and axis in _norm(d.name)]
            if len(hit) == 1:
                return hit[0]
    names = [_norm(d.name) for d in dims]
    words = re.findall(r"([가-힣]+\s[가-힣]+)별", tbl_nm) + re.findall(r"([가-힣]{2,})별", tbl_nm)
    for word in words:
        w = _norm(word)
        if w in q and not any(w in n for n in names):
            item = next((d for d in dims if d.no == 0), None)
            return Dim(item.no, w, item.codes) if item else None
    return None


COUNT_CELL = re.compile(r"^(인원|인구|명|건수|가구|.+수)$")
SHARE_CELL = re.compile(r"^(구성비|비율|비중)")


def _count_or_share(d: Dim, question: str) -> str | None:
    if len(d.codes) != 2:
        return None
    count = [c for c, n in d.codes.items() if COUNT_CELL.match(_norm(n))]
    share = [c for c, n in d.codes.items() if SHARE_CELL.match(_norm(n))]
    if len(count) != 1 or len(share) != 1:
        return None
    return share[0] if PCT_ASK.search(question) else count[0]


def choose(
    question: str,
    parsed: dict,
    dims: list[Dim],
    region_dim: int | None,
    forced: dict[int, str] | None = None,
    tbl_nm: str = "",
    couple: bool = False,
    multi: tuple[int, list[str], str] | None = None,
    quiet: dict[int, str] | None = None,
    bands: dict[int, tuple[list[str], str]] | None = None,
):
    forced = forced or {}
    plan = Plan()
    sex = parsed.get("sex") or "계"
    # 숫자 구간 축의 구간 합(band.find): 그 축은 질문 조건을 이미 이은 것으로 본다
    for no, (codes, label) in (bands or {}).items():
        d = next((x for x in dims if x.no == no), None)
        if d is not None and codes and all(c in d.codes for c in codes):
            plan.bands[no] = (list(codes), label)
            plan.pick[no] = plan.matched[no] = codes[0]
    for d in dims:
        if d.no in plan.bands:
            continue
        if d.no != region_dim and d.sex_codes():
            plan.sex_dim = d
        elif d.no not in (0, region_dim) and d.is_age() and d.total():
            plan.age_dim = d
    if plan.sex_dim is None and sex in ("남", "여"):
        return None, "표에 성별 구분이 없음"
    if plan.age_dim is None and parsed.get("ages"):
        return None, "표에 연령 구분이 없음"
    skip = {region_dim} | ({plan.age_dim.no} if plan.age_dim else set()) | set(plan.bands)
    hits = _drop_region_words(
        question, _text_hits(question, dims, skip, tbl_nm), dims, region_dim, tbl_nm
    )
    household_table = HOUSEHOLD_CELL.search(_table_target(tbl_nm))
    asks: list[Ask] = []
    axis = parsed.get("rank_axis")
    ranked = _rank_dim(question, dims, region_dim, tbl_nm, axis)
    if axis and ranked is None:
        # 지역이 아닌 분류의 순위를 물었는데 이 표에 그 분류가 없다: 지역 순위로 조용히 떨어지지
        # 않고 멈춘다 (다음 출처 표 · 재계획 · 표 안내로). 11/24: '나라' 축 → 시군구 순위를 냈다
        return None, AXIS_UNMATCHED.format(axis=axis)
    if ranked is not None and len(leaves := _leaf_codes(ranked)) >= 2:
        plan.multi, plan.multi_dim = (ranked.no, leaves, "rank"), ranked
        plan.pick[ranked.no] = plan.matched[ranked.no] = leaves[0]
    if multi is not None and multi[1] and (md := next((d for d in dims if d.no == multi[0]), None)):
        plan.multi, plan.multi_dim = (md.no, list(multi[1]), multi[2]), md
        plan.pick[md.no] = plan.matched[md.no] = multi[1][0]
    for d in dims:
        if d.no in skip or (plan.multi and d.no == plan.multi[0]):
            continue
        if (
            d.no in forced
            and forced[d.no] in d.codes
            and not _place_cell_unasked(question, d, forced[d.no], region_dim)
        ):
            plan.pick[d.no] = forced[d.no]
            how = (quiet or {}).get(d.no)
            if how is None:
                plan.matched[d.no] = forced[d.no]
            elif how == "couple":
                plan.defaults.append(f"{d.name}: {d.codes[forced[d.no]]} {couple_note(region_dim)}")
                plan.couple = True
            continue
        codes = hits.get(d.no, [])
        if d is plan.sex_dim:
            sex_set = set(d.sex_codes().values())
            others = [c for c in codes if c not in sex_set]
            if (
                not plan.multi
                and not others
                and sex_set <= set(codes)
                and _multi_op(question) == "compare"
            ):
                both = [d.sex_codes()["남"], d.sex_codes()["여"]]
                plan.multi, plan.multi_dim = (d.no, both, "compare"), d
                plan.pick[d.no] = plan.matched[d.no] = both[0]
                continue
            base = (others[:1] or [d.total()] or [None])[0]
            if base is None:
                rest = [c for c in d.codes if c not in sex_set]
                base = rest[0] if len(rest) == 1 else None
            if base is None and sex in ("남", "여"):
                plan.pick[d.no] = plan.matched[d.no] = d.sex_codes()[sex]
                plan.couple = couple or _couple_code(d) is not None
                continue
            if (
                base is None
                and sex not in ("남", "여")
                and (husband := _couple_code(d) or (d.sex_codes()["남"] if couple else None))
            ):
                if parsed.get("ages"):
                    plan.pick[d.no] = husband
                    plan.split_sex = {"남": "남편", "여": "아내"}
                    plan.defaults.append(
                        f"{d.name}: 남편 · 아내 따로"
                        " (부부 단위 건수라 연령은 남편 · 아내 나이가 다름)"
                    )
                    plan.couple = True
                    continue
                plan.pick[d.no] = husband
                plan.defaults.append(f"{d.name}: {d.codes[husband]} {couple_note(region_dim)}")
                plan.couple = True
                continue
            if len(others) > 1:
                asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in others]))
            elif base is None:
                male, female = d.sex_codes()["남"], d.sex_codes()["여"]
                plan.pick[d.no] = male
                plan.split_sex = {"남": d.codes[male], "여": d.codes[female]}
                plan.defaults.append(f"{d.name}: {d.codes[male]} · {d.codes[female]} 따로")
            else:
                plan.pick[d.no] = base
                if others or sex_set <= set(codes):
                    plan.matched[d.no] = base
            continue
        if len(codes) > 1 and d.total() and set(codes) == set(_leaf_codes(d)):
            plan.pick[d.no] = plan.matched[d.no] = d.total()
            continue
        op = _multi_op(question) if len(codes) > 1 and not plan.multi else None
        if op:
            plan.multi = (d.no, codes, op)
            plan.pick[d.no] = plan.matched[d.no] = codes[0]
        elif len(codes) > 1:
            asks.append(Ask(d.no, d.name, [(c, d.codes[c]) for c in codes]))
        elif codes:
            plan.pick[d.no] = plan.matched[d.no] = codes[0]
        elif total := d.total() or (_item_whole(d, tbl_nm) if household_table else None):
            plan.pick[d.no] = total
            if not SAFE_TOTAL.search(_norm(d.name)):
                plan.defaults.append(f"{d.name}: {d.codes[total]}")
        elif len(d.codes) == 1:
            plan.pick[d.no] = next(iter(d.codes))
        elif (default := d.default()) is not None:
            plan.pick[d.no] = default
            plan.defaults.append(f"{d.name}: {d.codes[default]}")
        elif (count := _count_or_share(d, question)) is not None:
            plan.pick[d.no] = count
            plan.defaults.append(f"{d.name}: {d.codes[count]}")
        elif (husband := _couple_code(d)) and not parsed.get("ages"):
            plan.pick[d.no] = husband
            plan.defaults.append(f"{d.name}: {d.codes[husband]} {couple_note(region_dim)}")
            plan.couple = True
        elif len(d.codes) <= MAX_ASK_OPTIONS:
            asks.append(Ask(d.no, d.name, list(d.codes.items())))
        else:
            return None, f"{d.name}에서 칸을 고르지 못함"
    if not plan.matched and not (plan.sex_dim and sex in ("남", "여")) and quiet is None:
        return None, "질문의 말과 맞는 칸이 없음"
    if asks:
        return None, asks[0]
    return plan, None


def _couple_code(d: Dim) -> str | None:
    names = {_norm(n): c for c, n in d.codes.items()}
    if set(names) == {"남편", "아내"}:
        return names["남편"]
    return None


COUPLE_NOTE = "기준 (부부 단위 건수. 지역별 값은 남편 주소지 기준으로, 공표된 시도별 건수와 같음)"
COUPLE_NOTE_NATIONAL = "기준 (부부 단위 건수)"


def couple_note(region_dim: int | None) -> str:
    return COUPLE_NOTE if region_dim is not None else COUPLE_NOTE_NATIONAL


def table_notice(notice: str | None, couple: bool) -> str | None:
    if not notice or not couple:
        return notice
    left = notice.replace(search_answer.NO_TOTAL_NOTE.rstrip("."), "")
    return re.sub(r"(?:\.\s*){2,}", ". ", left).strip(" .") or None


@lru_cache(maxsize=512)
def couple_counted(tbl_id: str) -> bool:
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


@dataclass
class BandMiss:
    item: str
    dim_name: str
    labels: list[str]
    dim_no: int = -1

    def __str__(self) -> str:
        return f"'{self.item}'에 맞는 {self.dim_name} 구간 없음"


MEASURE_UNIT = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d+)?\s*(kg|cm|개월|년(?:\s*반)?|g)(?!도)", re.I)


def _unit_norm(text: str) -> str:
    return unicodedata.normalize("NFKC", re.sub(r"\s+", "", str(text))).lower()


def _band_unions(plan: Plan) -> list[band.Union]:
    return [band.Union(no, tuple(codes), label) for no, (codes, label) in plan.bands.items()]


def _band_miss(texts: list[str], dims: list[Dim], region_dim: int | None) -> BandMiss | None:
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
    question: str = "",
    tbl_nm: str = "",
) -> "str | BandMiss | None":
    if not slots:
        return None
    cond = slots.get("conditions") or {}
    names = [_norm(d.codes[c]) for d in dims for no, c in plan.pick.items() if no == d.no]
    if plan.multi:
        md = plan.multi_dim or next(d for d in dims if d.no == plan.multi[0])
        names += [_norm(md.codes[c]) for c in plan.multi[1]]
    read = [_norm(a) for a, _ in slots.get("read_as") or [] if len(_norm(a)) >= 2]
    titles = [_norm(w) for w in slots.get("table_words") or [] if w]
    by_cells = slots.get("cells_by_llm") or slots.get("cells_by_binding")
    for item in [] if by_cells else cond.get("items") or []:
        if len(word := _norm(item)) >= 2 and any(word in t for t in titles):
            continue
        if region_dim is not None and _lives_in_asked_region(item, parsed):
            continue
        if not _item_left(item, names, read, plan, parsed, question):
            continue
        if any(u.label and _norm(item) in _norm(u.label) for u in _band_unions(plan)):
            continue  # 구간 합으로 이미 이었다 ("저체중아" → 2.5kg 미만 칸들)
        if miss := _band_miss([item], dims, region_dim):
            return miss
        return f"질문의 조건 '{item}'을 표의 칸에 반영할 수 없음"
    if slots.get("exclude"):
        return f"빼 달라는 대상({', '.join(slots['exclude'])})이 합계 칸에 들어 있음"
    if slots.get("check_target") and (target := _norm(slots.get("target") or "")):
        words = [*names, *(_norm(w) for w in slots.get("table_words") or [] if w), *read]
        if not any(target in w or (len(w) >= 2 and w in target) for w in words):
            return f"질문의 대상 '{slots['target']}'이 표나 고른 칸에 없음"
    skip = {"성별"} | ({"연령", "나이"} if parsed.get("ages") else set())
    said = _text_hits(question, dims, {region_dim}, tbl_nm) if question else {}
    classes = [
        c
        for c in cond.get("classes") or []
        if _norm(c) not in skip and not _class_narrowed(c, plan, dims, said)
    ]
    split = bool(slots.get("cells_by_binding") and plan.multi and plan.multi[2] in SPLIT_OPS)
    if classes and not split and parsed.get("type") not in ("composition", "rank", "trend"):
        return f"나눠 보기({', '.join(classes)}별)는 값 하나로 답할 수 없음"
    return None


def _class_narrowed(cls: str, plan: Plan, dims: list[Dim], said: dict[int, list[str]]) -> bool:
    word = _norm(cls)
    if len(word) < 2:
        return False
    in_item = not any(word in _norm(d.name) for d in dims)
    return any(
        d.no in plan.matched
        and plan.matched[d.no] != d.total()
        and plan.matched[d.no] in (said.get(d.no) or [])
        and (
            word in _norm(d.name)
            or word in _norm(d.codes[plan.matched[d.no]])
            or (in_item and d.no == 0)
        )
        for d in dims
    )


def _chosen_places(plan: Plan, dims: list[Dim], region_dim: int | None) -> list[tuple[Dim, str]]:
    matched = {no: [code] for no, code in plan.matched.items()}
    if plan.multi:
        no, codes, op = plan.multi
        matched[no] = codes if op != "rank" else []
    return [
        (d, d.codes[code])
        for d in dims
        if d.no != region_dim
        for code in matched.get(d.no, [])
        if code != d.total() and _is_place_cell(d.codes[code])
    ]


def _elsewhere(parsed: dict, cells: list[str], mentions: list[PlaceMention]) -> list[str]:
    residences = [m.name for m in mentions if m.role is None]
    regions = parsed.get("regions") or []
    used = [
        r
        for r in regions
        if not _named_place(r, residences) and any(_named_place(c, [r]) for c in cells)
    ]
    return used if used and len(used) < len(regions) else []


def _place_name(
    name: str,
    mentions: list[PlaceMention],
    places: list[str],
    target: str,
    counted: bool,
) -> str:
    if not places or not target or not _norm(name).startswith(_norm(_label_name(places[0]))):
        return name
    phrase = _label_name(places[0])
    for mention in mentions:
        if mention.role and _named_place(places[0], [mention.name]):
            phrase = mention.phrase
            break
    whole = f"{phrase} {_label_name(target)}"
    return f"{whole} 수" if counted and not whole.endswith("수") else whole


def _birthplace_axes(dims: list[Dim], region_dim: int | None, title: str) -> set[int]:
    axes = {d.no for d in dims if d.no != region_dim and BIRTHPLACE_AXIS.search(d.name)}
    if not axes and region_dim != 0 and BIRTHPLACE_AXIS.search(title):
        return {0}
    return axes


LIVES_IN = re.compile(r"\s*(?:에\s*)?(?:사는|살고\s*있는|거주하는|거주자|거주)\s*$")


def _lives_in_asked_region(item: str, parsed: dict) -> bool:
    place = LIVES_IN.sub("", item)
    regions = parsed.get("regions") or []
    return bool(place) and _is_place_cell(place) and _named_place(place, regions)


def _item_left(
    item: str, names: list[str], read: list[str], plan: Plan, parsed: dict, question: str = ""
) -> bool:
    word = _norm(item)
    if not word:
        return False
    if any(word in n or n in word for n in names if len(n) >= 2):
        return False
    counts = [w for w in agegroup.count_words(item, question) if re.search(r"\d", w)]
    if counts and all(any(w in n for n in names) for w in counts):
        return False
    if any(word in r or r in word for r in read):
        return False
    is_age = lookup.parse_age(item) or qa.extract_ages(item)
    if is_age and parsed.get("ages") and plan.age_dim is not None:
        return False
    return not (
        any(word in w for w in ("남자", "여자", "남성", "여성")) and plan.sex_dim is not None
    )


def _report_cells(
    slots: dict | None, plan: Plan | None, dims: list[Dim], parsed: dict, region_dim: int | None
) -> None:
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
        cells=cells,
        leftover=[
            i
            for i in items
            if _item_left(i, names, read, plan, parsed, (slots or {}).get("question") or "")
        ],
    )


def _has_age_dim(dims: list[Dim], region_dim: int | None) -> bool:
    return any(d.no not in (0, region_dim) and d.is_age() and d.total() for d in dims)


def _filters(plan: Plan, dims: list[Dim], region_dim: int | None) -> dict:
    out = {
        d.col: plan.pick[d.no] for d in dims if d.no not in (0, region_dim) and d.no in plan.pick
    }
    for d in dims:
        if d.no in plan.bands and d.no not in (0, region_dim):
            out[d.col] = list(plan.bands[d.no][0])  # 칸 여러 개 → lookup 이 더한다
    return out


def _sex_override(plan: Plan, sex: str) -> dict:
    d = plan.sex_dim
    code = plan.pick[d.no] if sex == "계" else d.sex_codes()[sex]
    return {"item_id": code} if d.no == 0 else {"filters": {d.col: code}}


def _without_age_words(term: str) -> str:
    for start, end, _ in sorted(qa.age_spans(term), reverse=True):
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
    target: str = "",
) -> str:
    named = [d for d in dims if d.no in plan.matched and d.no not in plan.bands]
    named = [d for d in named if len(d.codes) > 1] or named
    words = [d.codes[plan.matched[d.no]] for d in named]
    words = words or [dims[0].codes[plan.pick[0]]]
    if unit:
        tail = re.compile(rf"\s*\(\s*{re.escape(unit)}\s*\)\s*$")
        words = [tail.sub("", w) or w for w in words]
    words = [_label_name(w) for w in words]
    informative = [w for w in words if w not in TOTAL_NAMES]
    words = informative or words
    for source in ("term", "indicator"):
        term = parsed.get(source)
        if not term or re.search(r"\d{4}", str(term)):
            continue
        if indicators.find_regions(str(term)):
            continue
        named = _term_name(str(term))
        about = _norm(_label_name(target)) if target else ""
        needed = informative if source == "term" else words
        fits_target = bool(informative) or not about or about in _norm(named)
        if fits_target and all(_norm(w) in _norm(named) for w in needed):
            return _without_age_words(named) if strip_age else named
    if not informative and target:
        words = [_label_name(target)]
    name = " ".join(words)
    head = (_label_name(target).split() or [""])[-1] if target else ""
    if head in COUNTED_UNITS and COUNT_CLASS.match(name) and head not in name:
        name = f"{name} {head}"
    return f"{name} 수" if counted and not name.endswith("수") else name


def _cells(plan: Plan, dims: list[Dim]) -> str:
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
    dims, region_dim, level = schema(tbl_id)
    if not dims or dims[0].no != 0:
        return None, "항목 정보가 없음"
    projection = {}
    if is_projection(tbl_id):
        future_after = indicators._data_years()[1]
        if not _asks_future(parsed, future_after):
            return None, "장래추계 표는 미래 연도를 물을 때만 씀"
        projection = {
            "future_after": future_after,
            "note": f"{NOTE}. {PROJECTION_NOTE}",
            "answer_note": PROJECTION_ANSWER,
        }
    ignore_ages = False
    if parsed.get("ages") and parsed.get("age_group") and not _has_age_dim(dims, region_dim):
        parsed, ignore_ages = {**parsed, "ages": None}, True
    try:
        tbl_nm = lookup._table(tbl_id)["tbl_nm"]
    except Exception:
        tbl_nm = ""
    target = _table_target(tbl_nm)
    housing = qa.housing_population_condition(question) or parsed.get("housing_condition")
    if housing and "고령자" in target:
        ages = qa.resolved_ages(question)
        if not ages or any(lo < 60 for lo, _ in ages):
            return None, "이 통계표의 집계 대상은 고령자로 한정됨"
    household_asked = HOUSEHOLD_ASK.search(question) and not PERSON_ASK.search(question)
    if household_asked and POPULATION_TARGET.search(target):
        return None, "가구 수를 물었는데 이 통계표의 집계 대상은 사람 수"
    mentions = _place_mentions(question)
    birthplaces = [m.name for m in mentions if m.role == "birthplace"]
    birthplace_axes = _birthplace_axes(dims, region_dim, tbl_nm) if birthplaces else set()
    if birthplaces and not birthplace_axes:
        return None, "질문의 출생지 조건을 적용할 분류가 이 통계표에 없음"
    if (
        not (birthplaces or housing)
        and PER_AVERAGE.search(question)
        and (avg := _per_average(tbl_id, dims, region_dim, level))
    ):
        return {**avg, **projection}, None
    try:
        couple = partial_table.active(tbl_id) is None and couple_counted(tbl_id)
    except Exception:
        logger.exception("부부 단위 표 확인 실패: %s", tbl_id)
        couple = False
    multi = (slots or {}).get("binding_multi")
    quiet = (slots or {}).get("binding_quiet")
    # 숫자 구간 축의 구간 합: "2.5kg 미만" · "고령산모(35세 이상)"가 덮는 칸들을 그 축에 잇는다
    items = list(((slots or {}).get("conditions") or {}).get("items") or [])
    literal = set(
        _text_hits(question, dims, {region_dim}, tbl_nm)
    )  # 칸 이름이 글자로 있으면 그 매칭이 맡는다
    bands = {
        u.dim_no: (list(u.codes), u.label)
        for u in band.find([question, *items], dims, region_dim)
        if u.dim_no not in literal
    }
    plan, why = choose(
        question, parsed, dims, region_dim, forced, tbl_nm, couple, multi, quiet, bands or None
    )
    if (seen := (slots or {}).get("seen")) is not None:
        seen["plan"] = plan
    _report_cells(slots, plan, dims, parsed, region_dim)
    if plan is None:
        items = list(((slots or {}).get("conditions") or {}).get("items") or [])
        if not isinstance(why, Ask) and (miss := _band_miss([*items, question], dims, region_dim)):
            return None, miss
        return None, why
    chosen_places = _chosen_places(plan, dims, region_dim)
    birthplace_cells = [cell for d, cell in chosen_places if d.no in birthplace_axes]
    if any(
        not any(_named_place(cell, [place]) for cell in birthplace_cells) for place in birthplaces
    ):
        return None, "질문의 출생지 조건이 선택한 칸에 반영되지 않음"
    if ignore_ages:
        expr = _norm((parsed.get("age_group") or {}).get("expr") or "")
        names = [_norm(d.codes[plan.matched[d.no]]) for d in dims if d.no in plan.matched]
        names += [_norm(tbl_nm)] + [_norm(w) for w in (slots or {}).get("table_words") or [] if w]
        if not expr or not any(expr in n for n in names):
            return None, "질문의 연령 조건을 표의 칸에 반영할 수 없음"
    if why := uncovered(slots, plan, dims, parsed, region_dim, question, tbl_nm):
        return None, why
    miss = _band_miss([question], dims, region_dim)
    if miss and miss.dim_no not in plan.matched and miss.dim_no not in plan.bands:
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
    unit = _name_unit(plan, dims, unit or "")
    counted = unit in COUNT_UNITS and whole
    display_unit = unit
    if counted and (household_unit := HOUSEHOLD_UNITS.get(unit)):
        if counts_households(target, plan, dims[0]):
            display_unit = household_unit
        elif household_asked:
            return None, "가구 수를 물었는데 고른 칸은 사람 수(단위 명)를 센 칸"
    if housing:
        if not counted or display_unit not in ("명", "천명"):
            return None, "거주 인원을 물었는데 고른 칸은 사람 수를 집계하지 않음"
        if not any(
            _norm(housing) in _norm(d.codes[plan.pick[d.no]])
            for d in dims
            if d.no != region_dim and d.no in plan.pick
        ):
            return None, f"'{housing}' 조건이 선택한 칸에 반영되지 않음"
    strip_age = bool(
        plan.age_dim
        and parsed.get("ages")
        and parsed.get("type") != "composition"
        and not PCT_ASK.search(question)
    )
    places = [cell for _, cell in chosen_places]
    name = _place_name(
        _name(parsed, dims, plan, counted, strip_age, unit, target),
        mentions,
        places,
        target,
        counted,
    )
    spec = {
        "name": name,
        "unit": display_unit,
        "kind": "count" if counted else "value",
        "method": "lookup",
        "dynamic": True,
        "note": OTHER_TABLE_NOTE if indicators.canonical_name(name) else NOTE,
        "sources": [src],
        **projection,
        **({"couple": True} if plan.couple else {}),
        **({"elsewhere": elsewhere} if (elsewhere := _elsewhere(parsed, places, mentions)) else {}),
    }
    kind = parsed.get("type")
    if plan.split_sex and (plan.multi or kind == "composition" or PCT_ASK.search(question)):
        return None, f"{plan.sex_dim.name}에서 칸을 고르지 못함"
    if plan.multi:
        return _parts_spec(spec, src, plan, dims, tbl_nm)
    if kind == "composition" or PCT_ASK.search(question):
        made = _composition(spec, src, plan, dims, question) if kind == "composition" else None
        if made is None and kind == "composition":
            if axis := _asked_axis(question):
                return None, f"{axis}별 구성비를 이 표로 낼 수 없음"
            if not PCT_ASK.search(question):
                return None, "구성비를 낼 분류를 표에서 찾지 못함"
        if made is None:
            made, why = _ratio(spec, src, plan, dims, parsed, tbl_nm, question=question)
            if made is None:
                return None, why
        spec = made
    else:
        if plan.split_sex:
            src["by_sex"] = {
                s: {**_sex_override(plan, s), "label": label} for s, label in plan.split_sex.items()
            }
        elif plan.sex_dim is not None:
            src["by_sex"] = {s: _sex_override(plan, s) for s in ("계", "남", "여")}
        if plan.age_dim is not None and parsed.get("ages") and not ignore_ages:
            try:
                codes = lookup._age_codes(src, tuple(parsed["ages"][0]))
            except lookup.AgeMismatch:
                codes = None
            if codes and (
                why := part_within_whole(src, src["age_dim"] + "_id", codes, src["age_total"])
            ):
                return None, why
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
        spec["ignore_ages"] = True
    if not spec.get("formula") and (cells := _cells(plan, dims)):
        spec["formula"] = f"KOSIS 공표 값 ({cells})"
    spec["cells"] = _cells(plan, dims)
    pair_unit = spec.get("unit") == "쌍" or "(쌍)" in spec["cells"]
    if PAIRS_ASKED.search(question) and not pair_unit:
        return None, "질문은 '쌍' 단위인데 표의 칸은 다른 단위"
    return spec, None


def _parts_spec(spec: dict, src: dict, plan: Plan, dims: list[Dim], tbl_nm: str):
    dim_no, codes, op = plan.multi
    d = plan.multi_dim or next(x for x in dims if x.no == dim_no)
    if op == "sum" and d.total() and (why := part_within_whole(src, d.col, codes, d.total())):
        return None, why
    if plan.sex_dim is not None and d is not plan.sex_dim:
        src["by_sex"] = {s: _sex_override(plan, s) for s in ("계", "남", "여")}
    words = [
        _label_name(x.codes[plan.matched[x.no]])
        for x in dims
        if x.no in plan.matched and x.no != dim_no and plan.matched[x.no] != x.total()
    ]
    subject = " ".join(words) or re.sub(r"\(.*$|-[^-]*$", "", tbl_nm.split("별")[-1]).strip()
    if not words and any(_norm(subject) in _norm(d.codes[c]) for c in codes):
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
    if d.no != 0 or not tbl_nm:
        return None
    code, name = next(iter(d.codes.items()), (None, ""))
    for n in {_norm(name), _norm(_label_name(name))}:
        if len(n) >= 4 and n in _norm(tbl_nm):
            return code
    marked = [c for c, n in d.codes.items() if TOTAL_SUFFIX.search(_norm(n)) and "-" in n]
    return code if marked == [code] else None


PER_AVERAGE = re.compile(
    r"(가구|세대)\s*당\s*(평균\s*)?(가구원|세대원)|평균\s*(가구원|세대원)\s*수"
)
MEMBER_CELL = re.compile(r"^(.*?)_?(가구원|세대원)(_?계)?$")


def _per_average(tbl_id: str, dims: list[Dim], region_dim: int | None, level: str) -> dict | None:
    names = {re.sub(r"\s", "", n): c for c, n in dims[0].codes.items()}
    pair = None
    for name, code in names.items():
        m = MEMBER_CELL.match(name)
        if not m:
            continue
        prefix = m[1].rstrip("_")
        household = "가구" if m[2] == "가구원" else "세대"
        den = names.get(f"{prefix}_{household}" if prefix else household)
        if den is None and prefix.endswith(household):
            den = names.get(prefix)
        if den and (pair is None or prefix.startswith("일반")):
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
    sex = parsed.get("sex") or "계"
    parts = []
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
        if d.no in plan.bands and whole:
            codes, label = plan.bands[d.no]
            parts.append((d.col, list(codes), whole, label))
        elif d.no in plan.matched and whole and plan.matched[d.no] != whole:
            if d is not plan.sex_dim:
                code = plan.matched[d.no]
                if indicators.find_regions(d.codes[code]):
                    continue
                parts.append((d.col, [code], whole, d.codes[code]))
    if len(parts) != 1:
        return None, "비율의 분자·분모를 정하지 못함"
    _, num_codes, den_code, _ = parts[0]
    if isinstance(den_code, str) and [den_code] == list(num_codes):
        return None, "비율의 분자와 분모가 같은 칸"
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
    if plan.sex_dim is not None and plan.sex_dim.no != 0 and sex == "계" and not population_sex:
        # 성별 '계' 칸을 분자 · 분모 조회에 붙인다. 건수 경로는 by_sex 로 붙지만 비율 경로는 빠져
        # 계 + 남 + 여가 합산됐다 (값은 같은 비율이라 맞았지만 보이는 분자 · 분모가 2배, 11/26)
        whole_sex = {plan.sex_dim.col: plan.pick[plan.sex_dim.no]}
        num_src["filters"] = {**whole_sex, **num_src.get("filters", {})}
        num_src["den_filters"] = {**whole_sex, **num_src.get("den_filters", {})}
    whole = next(d.codes for d in dims if d.col == col)
    name = spec["name"]
    if population_label:
        name = f"{population_label} {_without_age_words(name)}"
    if population_sex and not qa.GENDER_WORDS[sex].search(name):
        name = f"{'남성' if sex == '남' else '여성'} {name}"
    whole_label = population_label if age_dim and col == age_dim.col and population_label else None
    out = {
        **spec,
        "name": _ratio_name(name, _label_name(part)),
        "unit": "%",
        "kind": "ratio",
        "formula": f"{part} ÷ {whole_label or whole.get(den, '합계')} × 100",
        "sources": [num_src],
    }
    if population_ages or (age_dim is not None and col == age_dim.col):
        out["ages_in_filters"] = True
    return out, None


def _ratio_name(name: str, part: str) -> str:
    if re.search(r"비율|비중|%", name):
        return name
    subject = re.sub(r"\s*수$", "", name)
    subject = " ".join(w for w in subject.split() if _norm(w) != _norm(part)) or subject
    return f"{subject} 중 {part} 비율"


CHECKS = MemoryCache("dynamic_checks", maxsize=1024, ttl=86400)


def part_within_whole(src: dict, col: str, num: list[str], den: str | list[str]) -> str | None:
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
    return {k: v for k, v in src.items() if k not in ("age_dim", "age_total", "age_min")}


AXIS_WORD = re.compile(r"([가-힣]{2,})별(?=로|\s|$|[?.!,]|의|은|는|구성|비중|비율)")
OTHER_AXES = {"남녀", "연령", "연령대", "나이", "시도", "시군구", "지역", "읍면동", "연도", "년도"}


def _asked_axis(question: str) -> str | None:
    for m in AXIS_WORD.finditer(question):
        if m.group(1) not in OTHER_AXES:
            return m.group(1)
    return None


def _composition(spec: dict, src: dict, plan: Plan, dims: list[Dim], question: str) -> dict | None:
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
                return None
            return {
                **spec,
                "unit": "%",
                "kind": "composition",
                "formula": "항목별 값 ÷ 합계 × 100",
                "sources": [out],
            }
    return None


LAST_YEAR_KINDS = ("value", "compare", "rank", "composition")


MAX_TABLES = 3


async def answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    recs = (found.get("recommendations") or [])[:MAX_TABLES]
    if found.get("together"):
        return None
    if found.get("status") != "ok":
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
    if not parsed.get("ages"):
        return False
    word = key.split(":", 1)[1] if ":" in key else key
    return "연령" in word or "나이" in word or bool(qa.extract_ages(word) or lookup.parse_age(word))


MAX_ATTEMPTS = 5
MAX_FETCH_AT_ONCE = 3
MAX_CELL_LLM = 3
MAX_RECELL = 1
SPLIT_OPS = ("rank", "compare")
_cell_llm_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "cell_llm_budget", default=None
)
_recell_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "recell_budget", default=None
)
HANDOFF_STATUSES = ("ok", "partial_match")
WHOLE_COUNTRY = {"전국", "우리나라", "한국", "대한민국", "국내"}


def to_parsed(understanding: dict, parsed: dict, *, question: str = "") -> tuple[dict, dict]:
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
        kind = q["type"] = "trend" if cond.get("year_end") else "value"
    region = cond.get("region")
    if region and len(parsed.get("regions") or []) <= 1:
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
                put("start_year", year)
        else:
            put("year", year_end or year)
    if cond.get("sex") in ("남", "여") and not qa.HOUSEHOLD_MEMBER_GENDER.search(question):
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
    cond = understanding.get("conditions") or {}
    if cond.get("year") is not None or cond.get("year_end") is not None:
        return False
    if not q.get("year") or qa._says_year(question):
        return False
    q["year"] = None
    return True


def without_regions(words: list[str]) -> list[str]:
    return [w for w in words if not indicators.find_regions(w)]


def attempt_list(target: dict, q: dict) -> list[dict]:
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
            checks = {k: v for k, v in picked["checks"].items() if k not in ("year", "period")}
            for e in picked.get("editions") or []:
                if covers(e):
                    add({**picked, **e, "checks": checks, "editions": [], "kind": "edition"})
    for c in cands:
        if c is not picked:
            add({**c, "kind": "other"})
    return out[:MAX_ATTEMPTS]


PAIRS_ASKED = re.compile(r"(쌍둥이|쌍태아)\S*\s*(\S+\s*){0,2}몇\s*쌍")
NOW_ASKED = re.compile(r"지금|현재|요즘|최근")
NOW_YEARS = 1
RECENT_YEARS = 5


def _recent_floor() -> int | None:
    try:
        return indicators._data_years()[1] - RECENT_YEARS
    except Exception:
        return None


ITEM_CHECK = "item:"


EXACT_SKIP = "검토가 고른 표를 '모두 충족'으로 봄"


def skip_reason(
    entry: dict, picked_exact: bool, parsed: dict | None = None, multi: bool = False
) -> str | None:
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
    question: str, parsed: dict, found: dict, trace: dict, repick: dict | None = None
) -> dict | None:
    """꾸러미(표 찾기 결과)로 계산한 답. 못 하면 None.

    repick: 판정 고리의 '같은 표에서 칸 다시' {"entry": 후보 표 하나, "hint": 판정 지적,
    "llm": recell 을 불러도 되나, "same": 결과가 앞 답과 같은 값인가(선택)}.
    주면 그 표 하나만, 규칙 칸 고르기 없이 칸 잇기 → recell 순서로 계산한다
    (_compute_target 의 repick_one). recell 은 표 경로의 질문당 상한(_recell_budget)을 쓰지 않는다:
    판정 고리 몫은 repick 한 번에 1회 (docs/agent_redesign_2026-11-26.md 2-2)"""
    progress.now(progress.COMPUTE)
    if repick is not None:
        trace.setdefault("dynamic", [])
        h = found.get("handoff") or {}
        entry = {"checks": {}, **repick["entry"], "picked": True}
        target = {"candidates": [entry]}
        one = {**found, "handoff": {**h, "status": "ok", "targets": [target]}}
        return await _compute_target(question, parsed, one, target, trace, repick=repick)
    h = found.get("handoff")
    if not h:
        return await answer(question, parsed, found, trace)
    trace.setdefault("dynamic", [])
    if h.get("status") not in HANDOFF_STATUSES or not h.get("targets"):
        return None
    _cell_llm_budget.set([MAX_CELL_LLM])
    _recell_budget.set([MAX_RECELL])
    targets = h["targets"]
    target = targets[0]
    if len(targets) > 1:
        if not parsed.get("scoped_age_ratio"):
            return await multi_answer(question, parsed, found, trace)
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
        if "value" in (res.get("result") or {}) and "ranking" not in (res.get("result") or {}):
            trace["dynamic"].append({"skip": "'가장 많은 지역의 값'인데 거르는 지표가 없음"})
            return None
    return res


async def multi_answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    from app.services import multi_indicator

    return await multi_indicator.answer(question, parsed, found, trace)


def _target_understanding(u: dict, target: dict) -> dict:
    cond = dict(u.get("conditions") or {})
    own = target.get("conditions") or {}
    for key in ("region", "region_level", "year", "year_end", "sex"):
        if own.get(key) not in (None, "", []):
            cond[key] = own[key]
    cond["items"], cond["classes"] = [], []
    out = {**u, "conditions": cond}
    if target.get("unsaid_age") and not own.get("age_text"):
        out["age"] = None
    if text := own.get("age_text"):
        bands = qa.extract_ages(str(text))
        if bands:
            lo, hi = bands[0]
            out["age"] = {"expr": str(text), "lo": lo, "hi": hi, "basis": None}
    return out


async def _compute_target(
    question: str,
    parsed: dict,
    found: dict,
    target: dict,
    trace: dict,
    multi: bool = False,
    repick: dict | None = None,
) -> dict | None:
    h = found["handoff"]
    trace.setdefault("dynamic", [])
    if target.get("together"):
        trace["dynamic"].append({"skip": "한 통계가 여러 표로 나뉨 (함께 볼 표)"})
        return None
    u = h.get("understanding") or {}
    if multi:
        u = _target_understanding(u, target)
    try:
        q, diff = await run_in_threadpool(to_parsed, u, parsed, question=question)
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
        slots = {
            "conditions": u.get("conditions") or {},
            "exclude": exclude,
            "target": u.get("target"),
            "check_target": (entry.get("review") or {}).get("match") != "exact",
            "table_name": entry.get("tbl_nm"),
            "table_words": [entry.get("tbl_nm"), entry.get("target")],
            "topic": u.get("topic"),
        }
        return slots, handoff_notice(entry, u, h.get("combine") or {}, found.get("notice"))

    def wants_llm(res, ask, sub: dict) -> bool:
        rule = next((x for x in reversed(sub["dynamic"]) if not x.get("by")), None)
        return (
            res is None
            and ask is None
            and rule is not None
            and cell_pick.needs_llm(rule.get("skip"))
        )

    async def compute(entry: dict, q_used: dict, sub: dict, llm: bool = True):
        slots, notice = context(entry)
        res, ask = await _try(
            entry["tbl_id"], entry.get("org_id"), question, q_used, sub, {}, slots, notice, sem
        )
        if wants_binding(entry, res, ask, sub):
            res, ask = await with_binding(entry, q_used, sub, slots, notice)
        if llm and wants_llm(res, ask, sub):
            res, ask = await with_cell_llm(entry, q_used, sub, slots, notice)
        if sub["dynamic"]:
            sub["dynamic"][0]["kind"] = entry["kind"]
        return res, ask

    def wants_binding(entry: dict, res, ask, sub: dict) -> bool:
        from app.core.config import get_settings

        return (
            get_settings().binding_use
            and res is None
            and ask is None
            and entry.get("kind") == "picked"
            and bool(sub["dynamic"])
            and bool(sub["dynamic"][-1].get("skip"))
        )

    async def with_binding(entry: dict, q_used: dict, sub: dict, slots: dict, notice):
        from app.services import binding

        tbl_id = entry["tbl_id"]
        forced = {int(k): v for k, v in (sub["dynamic"][-1].get("forced") or {}).items()}
        try:
            b = await run_in_threadpool(binding.bind, tbl_id, question, q_used, slots, forced)
            plan = await run_in_threadpool(binding.retry_plan, b, question, q_used)
        except Exception:
            logger.exception("잇기로 다시 계산할 칸 만들기 실패: %s", tbl_id)
            return None, None
        if plan is None:
            sub["dynamic"][-1]["binding_skip"] = (
                b.status if b.status != binding.BOUND else "연산 둘"
            )
            if (ask := binding.ask_of(b)) is not None:
                sub["dynamic"][-1]["binding_skip"] = f"되묻기 ({ask.dim_name})"
                return None, ask
            if b.status == binding.PARTIAL:
                sub["_partial"] = (b, sub["dynamic"][-1])
            return None, None
        return await run_binding(entry, plan, q_used, sub, slots, notice, "binding")

    async def run_binding(entry, plan, q_used, sub, slots, notice, by):
        slots_b = {
            **slots,
            "cells_by_binding": True,
            "binding_multi": plan.multi,
            "binding_quiet": plan.quiet,
        }
        res, ask = await _try(
            entry["tbl_id"],
            entry.get("org_id"),
            question,
            plan.parsed,
            sub,
            plan.forced,
            slots_b,
            notice,
            sem,
        )
        sub["dynamic"][-1]["by"] = by
        sub["dynamic"][-1]["binding_plan"] = plan.to_dict()
        said_sex = q_used.get("sex")
        if res is not None and said_sex in ("남", "여") and plan.parsed.get("sex") != said_sex:
            res["parsed"] = {**(res.get("parsed") or {}), "sex": said_sex}
        return res, ask

    async def with_recell(entry: dict, q_used: dict, sub: dict, slots: dict, notice):
        from app.core.config import get_settings
        from app.services import binding, recell

        partial = sub.pop("_partial", None)
        if partial is None:
            return None, None
        b, step = partial
        # recell이 뜻으로도 잇지 못하면, 남은 조건이 모두 칸 고르기일 때 되묻는다
        fallback = binding.ask_of(b, unsaid=True)
        if not get_settings().binding_recell:
            return None, fallback
        left = _recell_budget.get()
        if left is None:
            left = [MAX_RECELL]
            _recell_budget.set(left)
        if left[0] <= 0:
            step["recell_skip"] = f"질문당 recell 상한({MAX_RECELL}회)"
            return None, fallback
        left[0] -= 1
        try:
            fixed = await recell.fix(question, entry.get("tbl_nm"), b, u.get("topic"), step)
            plan = None
            if fixed is not None:
                plan = await run_in_threadpool(
                    binding.retry_plan, fixed.binding, question, q_used, True
                )
        except Exception:
            logger.exception("recell 실패: %s", entry["tbl_id"])
            return None, fallback
        if plan is None:
            if fallback is not None:
                step["recell_skip"] = f"뜻으로도 잇지 못함 → 되묻기 ({fallback.dim_name})"
            return None, fallback
        notice = ". ".join(n for n in (notice, fixed.notice) if n) or None
        return await run_binding(entry, plan, q_used, sub, slots, notice, "binding+recell")

    async def llm_then_binding(entry, q_used, sub, slots_llm, lines, picked):
        from app.services import binding

        tbl_id = entry["tbl_id"]
        try:
            b = await run_in_threadpool(
                binding.bind, tbl_id, question, q_used, slots_llm, picked.forced
            )
            plan = await run_in_threadpool(binding.retry_plan, b, question, q_used)
        except Exception:
            logger.exception("칸 고르기 LLM 뒤 잇기 실패: %s", tbl_id)
            return None, None
        if plan is None:
            ask = binding.ask_of(b)
            if sub["dynamic"]:
                sub["dynamic"][-1]["binding_skip"] = (
                    f"되묻기 ({ask.dim_name})" if ask is not None else b.status
                )
            return None, ask
        slots_b = {
            **slots_llm,
            "cells_by_binding": True,
            "binding_multi": plan.multi,
            "binding_quiet": plan.quiet,
        }
        res, ask = await _try(
            tbl_id,
            entry.get("org_id"),
            question,
            plan.parsed,
            sub,
            plan.forced,
            slots_b,
            ". ".join(lines) or None,
            sem,
        )
        sub["dynamic"][-1]["by"] = "cell_llm+binding"
        sub["dynamic"][-1]["binding_plan"] = plan.to_dict()
        sub["dynamic"][-1]["read_as"] = [list(p) for p in picked.read_as]
        said_sex = q_used.get("sex")
        if res is not None and said_sex in ("남", "여") and plan.parsed.get("sex") != said_sex:
            res["parsed"] = {**(res.get("parsed") or {}), "sex": said_sex}
        return res, ask

    async def with_cell_llm(entry: dict, q_used: dict, sub: dict, slots: dict, notice):
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
            budget[0] -= 1
        picked = await cell_pick.choose(
            question, u, entry.get("tbl_nm"), groups, sub["dynamic"][-1]
        )
        if picked is None:
            return None, None
        if picked.unsure:
            g = picked.unsure[0]
            codes = next((d.codes for d in dims if d.no == g.no), {})
            if len(picked.unsure) == 1 and 1 < len(codes) <= MAX_ASK_OPTIONS:
                return None, Ask(g.no, g.name, list(codes.items()), dict(picked.forced))
            return None, None
        slots_llm = {
            **slots,
            "read_as": picked.read_as,
            "check_target": False,
            "cells_by_llm": True,
        }
        lines = [n for n in (notice, picked.notice()) if n]
        if get_settings().binding_use:
            res, ask = await llm_then_binding(entry, q_used, sub, slots_llm, lines, picked)
            if res is not None and res.get("status") == "ok":
                return res, ask
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
        sub["dynamic"][-1]["read_as"] = [list(p) for p in picked.read_as]
        return res, ask

    said_year = any(q.get(k) for k in ("year", "start_year", "end_year"))
    now_asked = bool(NOW_ASKED.search(question)) and not q.get("year")
    recent = None if said_year and not now_asked else await run_in_threadpool(_recent_floor)
    if recent and now_asked:
        recent += RECENT_YEARS - NOW_YEARS
    if recent and now_asked and not any((e.get("end_year") or 0) >= recent for e in entries):
        recent = None

    async def attempt(entry: dict, exact: bool):
        sub: dict = {"dynamic": []}
        tbl_id = entry["tbl_id"]
        if why := skip_reason(entry, exact, q, multi):
            sub["dynamic"].append({"tbl_id": tbl_id, "kind": entry["kind"], "skip": why})
            return None, None, sub
        if recent and entry.get("end_year") and entry["end_year"] < recent:
            said = "지금을 물었는데" if now_asked else "연도를 말하지 않았는데"
            why = f"{said} {entry['end_year']}년까지인 표"
            sub["dynamic"].append({"tbl_id": tbl_id, "kind": entry["kind"], "skip": why})
            return None, None, sub
        if u.get("projection"):
            try:
                projection = await run_in_threadpool(is_projection, tbl_id)
            except Exception:
                projection = True
            if not projection:
                sub["dynamic"].append(
                    {"tbl_id": tbl_id, "skip": "장래 추계를 물었는데 추계 표가 아님"}
                )
                return None, None, sub
        res, ask = await compute(entry, q, sub, llm=False)
        return res, ask, sub

    def merge(sub: dict) -> None:
        trace["dynamic"].extend(sub["dynamic"])
        if sub.get("fetched"):
            trace.setdefault("fetched", []).extend(sub["fetched"])

    def is_ok(res) -> bool:
        return bool(res) and res.get("status") == "ok"

    async def run_round(idx: list[int], exact: bool) -> list:
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
            if not any(is_ok(res) for res, _, _ in outs):
                for k, (_res, ask, sub) in enumerate(outs):
                    if ask is None and sub.get("_partial"):
                        slots, notice = context(entries[idx[k]])
                        got = await with_recell(entries[idx[k]], q, sub, slots, notice)
                        if got[0] is not None or got[1] is not None:
                            outs[k] = (got[0], got[1], sub)
                        break
        return outs

    async def repick_one(entry: dict) -> dict | None:
        """판정 고리 repick: 이 표 하나에서 칸만 다시 고른 답. 칸 잇기가 앞 답과 다른 칸으로
        계산하면 그 답, 아니면(못 이음 · 같은 칸 · 값 없음) 판정 지적을 넘겨 recell 1회.
        규칙 칸 고르기는 건너뛴다: 앞 답이 규칙으로 고른 칸이 틀렸다고 판정됐다"""
        from app.core.config import get_settings
        from app.services import binding, recell

        hint = repick.get("hint") or {}
        wrong = str(hint.get("wrong") or "")
        record: dict = {"tbl_id": entry["tbl_id"]}
        trace["repick"] = record
        sub: dict = {"dynamic": []}
        slots, notice = context(entry)

        def fresh(res, by: str) -> bool:
            last = sub["dynamic"][-1] if sub["dynamic"] else {}
            if not is_ok(res) or not has_values(res.get("result") or {}):
                record[by] = (res or {}).get("status") or last.get("skip") or "못 함"
                return False
            if wrong and last.get("cells") == wrong:
                record[by] = "앞 답과 같은 칸"
                return False
            if (same := repick.get("same")) and same(res.get("result")):
                record[by] = "앞 답과 같은 값"  # 사전 답은 칸 기록이 없어 값으로 견준다
                return False
            record[by] = "ok"
            return True

        try:
            try:
                b = await run_in_threadpool(binding.bind, entry["tbl_id"], question, q, slots, {})
            except StatsTableMissing:
                # 큰 표는 첫 계산 때 칸만 받았고 그 범위가 끝났다: 한 번 받아 온 뒤 다시 잇는다
                await _try(
                    entry["tbl_id"], entry.get("org_id"), question, q, sub, {}, slots, notice, sem
                )
                b = await run_in_threadpool(binding.bind, entry["tbl_id"], question, q, slots, {})
            plan = await run_in_threadpool(binding.retry_plan, b, question, q)
        except Exception:
            logger.exception("repick 칸 잇기 실패: %s", entry["tbl_id"])
            record["binding"] = "잇기 실패"
            return None
        record["bound"] = b.status
        if plan is not None:
            res, _ = await run_binding(entry, plan, q, sub, slots, notice, "repick")
            if fresh(res, "binding"):
                merge(sub)
                return res
        else:
            record["binding"] = f"다시 계산할 칸 없음 ({b.status})"

        def ask_or_none() -> dict | None:
            """칸을 다시 고르지 못했다: 못 이은 조건이 모두 칸 고르기면 되묻기 (판정 고리가
            다른 행동도 실패하면 이 되묻기를 낸다. 고리 끝 경우 3), 아니면 None"""
            merge(sub)
            ask = binding.ask_of(b, unsaid=True)
            if ask is None:
                return None
            record["ask"] = ask.dim_name
            name = entry.get("tbl_nm") or entry["tbl_id"]
            return ask_answer(question, q, entry["tbl_id"], name, ask)

        if not repick.get("llm", True) or not get_settings().binding_recell:
            record["recell"] = "LLM 상한" if not repick.get("llm", True) else "꺼짐"
            return ask_or_none()
        step: dict = {}
        record["recell_log"] = step
        try:
            fixed = await recell.fix(question, entry.get("tbl_nm"), b, u.get("topic"), step, hint)
            plan = None
            if fixed is not None:
                plan = await run_in_threadpool(binding.retry_plan, fixed.binding, question, q, True)
        except Exception:
            logger.exception("repick recell 실패: %s", entry["tbl_id"])
            record["recell"] = "오류"
            return ask_or_none()
        if plan is None:
            record["recell"] = "뜻으로도 잇지 못함"
            return ask_or_none()
        notice = ". ".join(n for n in (notice, fixed.notice) if n) or None
        res, _ = await run_binding(entry, plan, q, sub, slots, notice, "repick+recell")
        merge(sub)
        return res if fresh(res, "recell") else None

    if repick is not None:
        entry = entries[0]
        trace["handoff"]["tried"] = 1
        with partial_table.shared_tables():
            res = await repick_one(entry)
        if is_ok(res):
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": "repick"}
        return res

    outcomes = await run_round(list(range(len(entries))), picked_exact)
    if picked_exact and not any(is_ok(res) or ask is not None for res, ask, _ in outcomes):
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
        hidden = next((s for s in sub["dynamic"] if s.get("flag") == "X"), None)
        if hidden and not is_ok(res):
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
            trace["handoff"]["suppressed"] = entry["tbl_id"]
            return await run_in_threadpool(
                suppressed_answer, q, entry["tbl_id"], hidden.get("cells") or ""
            )
        if res is not None and res.get("status") == "ok":
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
            last = sub["dynamic"][-1] if sub["dynamic"] else {}
            by_llm = last.get("by") == "cell_llm"
            forced = (last.get("forced") or {}) if by_llm else {}
            words = (last.get("read_as") or []) if by_llm else []
            if last.get("by") not in ("binding", "cell_llm+binding"):
                await _remember(parsed, entry["tbl_id"], forced, res, words)
            return res
    for entry, (_, ask, _) in zip(entries, outcomes, strict=True):
        if ask is not None:
            name = entry.get("tbl_nm") or entry["tbl_id"]
            return ask_answer(question, q, entry["tbl_id"], name, ask)

    for res, _, _ in outcomes:
        if res and (res.get("result") or {}).get("population_limit"):
            return res

    if multi:
        for entry, (res, _, _) in zip(entries, outcomes, strict=True):
            if res is not None:
                trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
                return res
        return None

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
        if res is not None:
            trace["handoff"]["used"] = {"tbl_id": entry["tbl_id"], "kind": entry["kind"]}
            return res
    return None


AGE_LIMIT = re.compile(r"(\d+)\s*세\s*이상")
WHOLE_POPULATION = re.compile(
    r"전\s*연령|모든\s*연령|연령\s*(?:제한|구분)\s*없이"
    r"|\b전체\b(?!\s*(?:기간|연도|시점|지역|시도|시군구))"
)


def _checks_population(question: str, kind: str | None) -> bool:
    return kind == "composition" or bool(
        PCT_ASK.search(question) or WHOLE_POPULATION.search(question)
    )


def age_limit(tbl_nm: str, question: str, kind: str | None = None) -> str | None:
    if not _checks_population(question, kind):
        return None
    m = AGE_LIMIT.search(tbl_nm or "")
    if m is None:
        return None
    scope = WITHIN.split(question, maxsplit=1)
    population = scope[0] if len(scope) == 2 else "" if PCT_ASK.search(question) else question
    if bands := qa.resolved_ages(population):
        return None if all(lo >= int(m[1]) for lo, _ in bands) else m[0]
    # 연령을 말하지 않았으면: 모집단이 표가 집계하는 대상 그 자체인지
    return None if _table_population(population, tbl_nm) else m[0]


POPULATION_GENERIC = {"인구", "사람", "사람들", "가구", "주민", "전체", "세대", "국민", "우리나라"}


def _table_population(population: str, tbl_nm: str) -> bool:
    """'통근·통학 인구 중'처럼 모집단을 말한 낱말이 모두 표 이름에 있으면 그 표가 집계하는
    모집단 자체다 (통근 · 통학 인구는 12세 이상으로 정의). 일반 말('1인 가구', '전체 인구')이나
    지역만 남으면 아니다: 표의 연령 제한이 질문의 모집단을 줄인다.
    표 이름에는 낱말 그대로 있어야 한다: '외국인 중'은 '외국인주민' 표, '여성 인구 중'은
    '여성인구(15세이상)' 표의 모집단이 아니다"""
    try:
        places = {_norm(p) for p in indicators.find_regions(population)}
    except Exception:
        places = set()
    words = []
    for token in re.findall(r"[가-힣]+", population):
        w = _norm(JOSA_TAIL.sub("", token))
        if len(w) >= 2 and w not in POPULATION_GENERIC and w not in places:
            words.append(w)
    return bool(words) and all(_as_word(w, tbl_nm) for w in words)


async def answer_choice(question: str, context: dict, code: str, trace: dict) -> dict | None:
    forced = {**{int(k): v for k, v in (context.get("forced") or {}).items()}}
    forced[int(context["dim_no"])] = code
    parsed = context.get("parsed") or {}
    tbl_id = context["tbl_id"]
    trace.setdefault("dynamic", [])
    res, ask = await _try(tbl_id, None, question, parsed, trace, forced)
    if res is None and ask is None:
        res, ask = await _choice_by_binding(question, parsed, tbl_id, forced, trace)
    if res is None and ask is not None:
        return ask_answer(question, parsed, tbl_id, context.get("tbl_nm") or tbl_id, ask, forced)
    if res is not None:
        await _remember(parsed, tbl_id, {}, res)
    return res


async def _choice_by_binding(question: str, parsed: dict, tbl_id: str, forced: dict, trace: dict):
    """칸잇기가 낸 되묻기는 규칙이 못 고른 표다. 고른 칸을 고정하고 칸잇기로 한 번 더 계산."""
    from app.core.config import get_settings
    from app.services import binding

    if not get_settings().binding_use:
        return None, None
    try:
        b = await run_in_threadpool(binding.bind, tbl_id, question, parsed, None, forced)
        plan = await run_in_threadpool(binding.retry_plan, b, question, parsed)
    except Exception:
        logger.exception("되묻기 답을 잇기로 계산 실패: %s", tbl_id)
        return None, None
    if plan is None:
        return None, binding.ask_of(b, unsaid=True)
    slots = {"cells_by_binding": True, "binding_multi": plan.multi, "binding_quiet": plan.quiet}
    res, ask = await _try(tbl_id, None, question, plan.parsed, trace, plan.forced, slots)
    trace["dynamic"][-1]["by"] = "binding"
    return res, ask


async def cached_answer(question: str, parsed: dict, trace: dict) -> dict | None:
    from app.core.config import get_settings

    if not get_settings().plan_cache:
        return None
    try:
        found = await run_in_threadpool(plan_cache.key_of, parsed)
        plans = await plan_cache.find(found[0]) if found else []
    except Exception:
        logger.exception("계산 방법 캐시 읽기 실패")
        return None
    trace["plan_cache"] = "miss" if found else "skip"
    if not plans:
        flow.add("계산 방법 캐시", "없음" if found else "건너뜀", (found or ["", ""])[1])
    steps = trace.setdefault("dynamic", [])
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
        step = steps[-1]
        if dropped := {k: v for k, v in saved.items() if k not in forced}:
            step["cache_dropped"] = dropped
        if step.get("flag") == "X":
            res = await run_in_threadpool(
                suppressed_answer, parsed, plan["tbl_id"], step.get("cells") or ""
            )
            res["trace"] = trace
        elif res is not None and res.get("status") != "ok":
            step.setdefault("skip", f"캐시한 표로 답하지 못함 ({res.get('status')})")
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
    by_no = {d.no: d for d in dims}
    hits = _drop_region_words(question, _text_hits(question, dims, {region_dim}), dims, region_dim)
    q = _norm(question)
    out = {}
    for no, code in forced.items():
        d = by_no.get(no)
        if d is None or code not in d.codes:
            continue
        if _place_cell_unasked(question, d, code, region_dim):
            continue
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
    spec = (res.get("parsed") or {}).get("dynamic_spec")
    from app.core.config import get_settings

    if res.get("status") != "ok" or not spec or not get_settings().plan_cache:
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
    res, ask = await _try_once(
        tbl_id, org_id, question, parsed, trace, forced, slots, notice, fetch_sem
    )
    step = (trace.get("dynamic") or [{}])[-1]
    if res is not None:
        outcome = res.get("status")
    else:
        outcome = "되묻기" if ask is not None else "못 함"
    if (slots or {}).get("cells_by_llm") and not (slots or {}).get("cells_by_binding"):
        how = "칸 고르기 LLM"
    elif (slots or {}).get("cells_by_binding"):
        how = "칸 고르기 LLM + 잇기" if (slots or {}).get("cells_by_llm") else "잇기"
    else:
        how = "규칙"
    parts = [tbl_id, f"칸={step['cells']} ({how})" if step.get("cells") else ""]
    if got := step.get("partial"):
        parts.append(f"칸만 바로 받음 (요청 {got['requests']}번)")
    parts.append(step.get("skip") or "")
    flow.add("표 칸으로 계산", outcome, " · ".join(p for p in parts if p))
    return res, ask


async def _try_once(
    tbl_id, org_id, question, parsed, trace, forced, slots=None, notice=None, fetch_sem=None
):
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
    if notice and (notice := notice.strip(" .")):
        spec["note"] = f"{spec['note'].rstrip(' .')}. {notice}"
    if spec.get("parts_op"):
        res = await run_in_threadpool(parts_answer, parsed, spec, question)
        res.setdefault("trace", trace)
        step["status"] = res["status"]
        if res["status"] == "ok":
            return res, None
        step["skip"] = res.get("answer", "")[:60]
        return None, None
    flags: list = []
    flag_scope = lookup.EMPTY_FLAGS.set(flags)
    try:
        res = await qa.answer(_with_spec(parsed, spec), trace, question)
    finally:
        lookup.EMPTY_FLAGS.reset(flag_scope)
    step["status"] = res["status"]
    if res["status"] == "ok" and not has_values(res.get("result") or {}):
        step["skip"] = "값이 없음 (비공개·결측)"
        if "X" in flags:
            step["flag"] = "X"
        return None, None
    if res["status"] in ("ok", "out_of_range", "band_mismatch"):
        return res, None
    return None, None


TOP_N = re.compile(r"(?:top|상위)\s*(\d+)|(\d+)\s*(?:위|개|가지)까지", re.I)


def _set_code(spec: dict, col: str, code: str) -> dict:
    src = dict(spec["sources"][0])
    if col == "item_id":
        src["item_id"] = code
        if any("item_id" in over for over in (src.get("by_sex") or {}).values()):
            src.pop("by_sex")
    else:
        src["filters"] = {**(src.get("filters") or {}), col: code}
    return {**spec, "sources": [src]}


def _decimals(v) -> int:
    text = repr(v)
    return (
        len(text.split(".")[1]) if isinstance(v, float) and "." in text and "e" not in text else 0
    )


def parts_answer(parsed: dict, spec: dict, question: str) -> dict:
    op = spec["parts_op"]
    region = (parsed.get("regions") or ["전국"])[0]
    year = parsed.get("year") or lookup._years(spec["sources"][0])[1]
    sex = parsed.get("sex") or "계"
    y0, y1 = parsed.get("start_year"), parsed.get("end_year")
    years = list(range(y0, y1 + 1)) if op["op"] == "sum" and y0 and y1 and y0 < y1 else [year]
    rows, first = [], None
    for code, label in zip(op["codes"], op["labels"], strict=True):
        total_of_code = 0
        for y in years:
            r = lookup.value(_set_code(spec, op["col"], code), region, y, sex)
            if r.get("status") != "ok" and op["op"] == "rank":
                total_of_code = None
                break
            if r.get("status") != "ok":
                return {"status": r.get("status"), "answer": r.get("reason") or "", "result": r}
            first = first or r
            if r.get("value") is None:
                total_of_code = None
                break
            total_of_code += r["value"]
        rows.append({"label": label, "value": total_of_code})
    if first is None:
        return {"status": "missing_data", "answer": "", "result": {"status": "missing_data"}}
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
        places = max(_decimals(hi["value"]), _decimals(lo["value"]))
        diff = round(hi["value"] - lo["value"], places)
        times = f" ({round(hi['value'] / lo['value'], 2)}배)" if lo["value"] else ""
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
    if spec.get("answer_note"):
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


def suppressed_answer(parsed: dict, tbl_id: str, cells: str) -> dict:
    tbl = lookup._table(tbl_id)
    where = f" ({cells})" if cells else ""
    result = {
        "status": "missing_data",
        "indicator": tbl["tbl_nm"],
        "reason": f"'{tbl['tbl_nm']}' 표에서 질문한 조건의 칸{where}은 값이 'X'로 표시되어 "
        "알 수 없습니다. 값이 아주 작아(5 미만 등) 개인이 드러날 수 있을 때 통계 작성 기관이 "
        "공표하지 않는 칸입니다(비밀보호).",
        "guide": "대신 더 넓은 구분(합계, 더 넓은 연령 구간, 시도 단위)의 값은 확인할 수 있습니다.",
        "suppressed": True,
        "source": f"{tbl['org_nm']}, {tbl['tbl_nm']} ({tbl_id})",
        "basis": tbl["basis"] or "KOSIS 공표 값",
        "tbl_id": tbl_id,
        "url": indicators.kosis_url(tbl_id),
        "method": "dynamic",
    }
    return {
        "status": "missing_data",
        "answer": f"{qa.summary(result)}\n\n{qa.render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": qa.sources(result),
        "trace": {},
    }


def ask_answer(question, parsed, tbl_id, tbl_nm, ask: Ask, forced=None) -> dict:
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
    for r in qa._leaves(result):
        if r.get("value") is not None or r.get("parts"):
            return True
        rows = [*(r.get("series") or []), *(r.get("values") or []), *(r.get("ranking") or [])]
        if any(x.get("value") is not None for x in rows):
            return True
    return False


PARTIAL_OUTCOMES = ("too_big", "busy")


async def _build(tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None):
    seen: dict = {}
    made = await _build_once(tbl_id, org_id, question, parsed, step, forced, slots, fetch_sem, seen)
    from app.core.config import get_settings

    if get_settings().binding_shadow:
        from app.services import binding

        rec = await run_in_threadpool(
            binding.shadow,
            tbl_id,
            question,
            parsed,
            slots,
            seen.get("plan"),
            made[0] is not None,
            forced,
        )
        if rec is not None:
            step["binding"] = rec
    return made


async def _build_once(tbl_id, org_id, question, parsed, step, forced, slots, fetch_sem, seen):
    def run():
        return build(tbl_id, question, parsed, forced, {**(slots or {}), "seen": seen})

    if partial_table.active(tbl_id) is not None:
        return await _build_partial(
            tbl_id, org_id, question, parsed, step, forced, slots, fetch_sem
        )

    try:
        return await run_in_threadpool(run)
    except StatsTableMissing:
        pass
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"
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
        return await run_in_threadpool(run)
    except Exception:
        logger.exception("동적 지표 계획 만들기 실패: %s", tbl_id)
        return None, "오류"


async def _build_partial(
    tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None, queued=""
):
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
    q = {**parsed, "indicator": spec["name"], "unsupported": False, "dynamic_spec": spec}
    if elsewhere := spec.get("elsewhere"):
        q["regions"] = [r for r in q.get("regions") or [] if r not in elsewhere]
        if q.get("type") == "compare" and len(q["regions"]) == 1:
            q["type"] = "value"
    if spec["kind"] == "ratio" and q.get("type") == "composition":
        q["type"] = "value"
    if q.get("type") in (None, "other"):
        q["type"] = "value"
    if not q.get("year") and q.get("type") in LAST_YEAR_KINDS:
        try:
            q["year"] = lookup._years(spec["sources"][0])[1]
            q["year_filled"] = True
        except Exception:
            logger.exception("동적 지표 수록 기간 확인 실패")
    return q
