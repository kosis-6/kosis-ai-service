"""계획: 질문 → 지표 목록·조건·결합 방식 (HCX-007 1회) + 코드 보정

  call(question)                 계획 LLM 호출 (보정 전)
  correct(question, plan)        코드 보정: 근거 없는 조건 삭제 → 시점·지역 단위 → 빠진 지표
  merged_conditions / render     지표 하나를 '조건을 붙인 단일 질문 문장'으로

LLM이 내놓은 계획을 그대로 믿지 않는다. 질문 원문에 근거가 없는 조건은 지우고,
질문에 있는데 빠뜨린 지표는 채운다.
"""

import json
import re
from datetime import date
from typing import Any

from app.prompts.plan import SCHEMA, SYSTEM
from app.services import search, timeref
from app.services import slots as slot_parser
from app.services.hcx_client import HcxClient

MODEL = "HCX-007"
# 실제 출력은 최대 306토큰이었다. 호출 한도는 요청한 최대치만큼 깎이므로 넉넉하게만 둔다
MAX_TOKENS = 512

Plan = dict[str, Any]


async def call(question: str) -> Plan:
    """계획 LLM 호출 (보정 전). 실패하면 HcxError가 그대로 올라간다"""
    result = await HcxClient().chat(
        MODEL,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}],
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        responseFormat={"type": "json", "schema": SCHEMA},
    )
    return parse(result.content)


def parse(content: str) -> Plan:
    """HCX 출력 → 계획. JSON이 깨졌거나 모양이 다르면 지표 없는 빈 계획"""
    try:
        p = json.loads(content)
    except (TypeError, ValueError):
        p = None
    if not isinstance(p, dict):
        p = {"_raw": content}
    if not isinstance(p.get("shared"), dict):
        p["shared"] = {}
    inds = p.get("indicators")
    p["indicators"] = [i for i in inds if isinstance(i, dict)] if isinstance(inds, list) else []
    if not isinstance(p.get("combine"), dict):
        p["combine"] = {"op": "none"}
    return p


def correct(question: str, p: Plan, today: date | None = None) -> Plan:
    """계획을 코드로 보정한다. 지운 조건은 _dropped, 고친 내용은 _fixed에 남긴다"""
    p["_dropped"] = ground_plan(question, p)
    p["_fixed"] = fix_periods_and_level(question, p, today) + fix_indicators(question, p)
    return p


def _conditions(p: Plan) -> list[dict[str, Any]]:
    """공통 조건과 지표별 조건 dict들 (고치면 계획에 그대로 반영된다)"""
    shared = p.get("shared")
    overrides = [(i.get("override") or {}) for i in p.get("indicators") or []]
    return [shared if isinstance(shared, dict) else {}, *overrides]


# ---- 근거 없는 조건 지우기 ------------------------------------------------------------

MALE = re.compile(r"남성|남자|남녀|남편|아버지|부(?![가-힣])")
FEMALE = re.compile(r"여성|여자|남녀|아내|어머니|산모|모(?![가-힣])")
RELATIVE_TIME = re.compile(r"최근|작년|올해|재작년|\d+\s*년\s*(간|사이|동안|전)")
AGE_NUMBER = re.compile(r"\d+\s*(세|대|살)")
AGE_GROUP = re.compile(r"청년|노인|고령|미성년|아동|청소년|초고령")
AGE_HINT = re.compile(r"\d+\s*(세|대|살)|청년|노인|고령|미성년|아동|청소년")


def ground_plan(question: str, p: Plan) -> list[tuple[str, Any]]:
    """질문에 근거 없는 조건(지어낸 연도·성별·연령)을 지운다"""
    years = {int(y) for y in re.findall(r"(?:19|20)\d\d", question)}
    relative = bool(RELATIVE_TIME.search(question))
    male, female = bool(MALE.search(question)), bool(FEMALE.search(question))
    both = male and female or "성별" in question or "성비" in question
    dropped: list[tuple[str, Any]] = []
    for cond in _conditions(p):
        for k in ("year_from", "year_to"):
            v = cond.get(k)
            if v and v not in years and not relative:
                dropped.append((k, v))
                cond.pop(k)
        sex = cond.get("sex")
        if sex == "남" and not male or sex == "여" and not female or sex == "남녀" and not both:
            dropped.append(("sex", sex))
            cond.pop("sex")
        age = cond.get("age")
        if age and not _age_grounded(str(age), question):
            dropped.append(("age", age))
            cond.pop("age")
    return dropped


def _age_grounded(age: str, question: str) -> bool:
    if search.norm(age) in search.norm(question):
        return True
    if AGE_NUMBER.search(age) and AGE_HINT.search(question):
        return True
    return bool(AGE_GROUP.search(age) and AGE_GROUP.search(question))


# ---- 시점·지역 단위 ------------------------------------------------------------------

YEAR_MARK = re.compile(r"(?:19|20)\d\d(?=\s*년)")
RANGE_WORD = re.compile(r"~|부터|까지|사이|대비|동안|와\s*(?:19|20)\d\d년\s*(?:비교|사이)")
# 현재를 가리키는 말 (지표 구절 안이나 바로 앞에 있으면 그 지표는 공통 연도를 물려받지 않는다)
PRESENT = re.compile(r"지금|현재|요즘|최신")


def _level(question: str) -> str | None:
    """질문이 말한 지역 단위 (슬롯 규칙과 같은 낱말)"""
    return next((lv for lv, pat in slot_parser.LEVEL_WORDS if pat.search(question)), None)


def fix_periods_and_level(question: str, p: Plan, today: date | None = None) -> list[tuple]:
    """상대 시점은 시점 규칙이 풀고, 지역 단위는 슬롯 규칙으로, 한쪽만 쓴 연도는 양쪽에"""
    today = today or date.today()
    shared = p["shared"] = p.get("shared") or {}
    fixes: list[tuple] = []

    years = re.findall(r"(?:19|20)\d\d", question)
    ref = None
    if not years:
        ref = timeref.recent_period(question, today) or timeref.resolve(question, today)
    if ref:
        lo, hi = ref.year, (today.year if ref.open_end else ref.year)
        for cond in _conditions(p):
            cond.pop("year_from", None)
            cond.pop("year_to", None)
        shared["year_from"], shared["year_to"] = lo, hi
        fixes.append(("상대시점", ref.expr, lo, hi))

    # 연도가 여럿이고 기간 표현이 없으면 지표마다 바로 앞에 나온 연도를 붙인다
    # ('2020년 A와 2025년 B')
    marks = [(m.start(), int(m[0])) for m in YEAR_MARK.finditer(question)]
    inds = [i for i in p.get("indicators") or [] if i.get("scope") != "out"]
    found = [question.find(i.get("span") or "@@") for i in inds]
    span_at = sorted(at for at in found if at >= 0)
    interleaved = any(
        any(a < at < b for at in span_at) for (a, _), (b, _) in zip(marks, marks[1:], strict=False)
    )
    several_years = len({y for _, y in marks}) >= 2
    if several_years and not RANGE_WORD.search(question) and len(inds) >= 2 and interleaved:
        for ind in inds:
            at = question.find(ind.get("span") or "@@")
            before = [y for pos, y in marks if 0 <= at and pos < at]
            if before:
                override = ind["override"] = ind.get("override") or {}
                override["year_from"] = override["year_to"] = before[-1]
        shared.pop("year_from", None)
        shared.pop("year_to", None)
        by_indicator = [(i.get("span"), (i.get("override") or {}).get("year_from")) for i in inds]
        fixes.append(("지표별 연도", by_indicator))

    # '2050년 총인구 전망을 지금 주민등록인구와 비교'처럼 현재를 가리키는 지표는 공통 연도를
    # 물려받지 않는다 (최신 연도로 찾는다). 물려받으면 두 지표가 같은 연도 조건이 되어 같은 표
    # 묶음으로 합쳐지거나(2026-11-13 결함 3), 2050년 자료가 없는 표를 놓쳤다(측정 x02)
    if shared.get("year_from") or shared.get("year_to"):
        for ind in inds:
            if (ind.get("override") or {}).get("year_from"):
                continue
            span = ind.get("span") or ""
            at = question.find(span) if span else -1
            before = question[max(0, at - 6) : at] if at > 0 else ""
            if PRESENT.search(span) or PRESENT.search(before):
                keep = list(ind.get("no_inherit") or [])
                ind["no_inherit"] = [*keep, *(k for k in ("year_from", "year_to") if k not in keep)]
                fixes.append(("현재 시점 지표", span))

    level = _level(question)
    if level:
        if shared.get("region_level") != level:
            fixes.append(("지역단위", shared.get("region_level"), level))
        shared["region_level"] = level
    elif shared.get("region_level") not in (None, "", "없음"):
        fixes.append(("지역단위 지움", shared.pop("region_level")))

    for cond in _conditions(p):
        if cond.get("year_to") and not cond.get("year_from"):
            cond["year_from"] = cond["year_to"]
        if cond.get("year_from") and not cond.get("year_to"):
            cond["year_to"] = cond["year_from"]
    return fixes


# ---- 빠진 지표·파생 지표 --------------------------------------------------------------

# 파생 지표 → 기초 지표 검색어 (그 이름의 표가 없거나 계산으로 만드는 지표).
# 원문 구절은 지우지 않고 덧붙인다
DERIVED = [
    (r"고용률|취업률|일하는 (?:사람|비율)|실업률", "성 연령 경제활동상태별 인구"),
    (r"성비|성별 불균형|성비 불균형", "성별 인구"),
    (r"황혼\s*이혼", "혼인지속기간별 이혼 건수"),
    (r"자연\s*(?:감소|증가)", "자연증가 출생 사망 건수"),
    (r"순유출|순유입|순이동|빠져나가", "순이동자수 이동자수"),
    (r"사유", "전입사유별 이동자수"),
    (r"노령화\s*지수|부양비|중위\s*연령", "주요 인구지표 노령화지수"),
    (r"고령\s*산모|35세 이상 산모|출산 연령", "모의 연령별 출생"),
    (r"저체중|출생아 체중|체중별", "출생아 체중별 출생"),
    (r"다태아|쌍둥이", "다태아 출생"),
    (r"미혼율|미혼 (?:비율|인구)", "혼인상태별 인구"),
    (r"본관|성씨", "본관별 인구"),
    (r"미혼모|미혼부|미혼 부모", "미혼모 미혼부"),
]
POPULATION_WORD = re.compile(
    r"인구|가구|출생|출산|사망|혼인|결혼|이혼|재혼|초혼|이동|전입|전출|외국인|다문화|이주배경"
    r"|장애|본관|성씨|노령|고령|산모|미혼|청년|노인|아동|학생|기대\s*(?:여명|수명)|생명표"
)
NOT_POPULATION = re.compile(r"가격|집값|전세|임금|소득|병원|의사")
REGION_ASK = re.compile(r"지역|어디|곳|시도|시군구|상위\s*\d+\s*개|하위\s*\d+")
# 질문에 나오면 반드시 지표 하나가 맡아야 하는 기초 지표 말 (계획이 빠뜨린 지표 채우기)
INDICATOR_TERMS = re.compile(
    r"합계출산율|조출생률|조혼인율|조이혼율|출생아\s*수|사망자\s*수|혼인\s*건수|이혼\s*건수"
    r"|자연\s*(?:감소|증가)|순이동|순유출|평균\s*초혼\s*연령|초혼\s*연령|평균\s*출산\s*연령"
    r"|노령화\s*지수|중위\s*연령|외국인\s*(?:수|인구)|\d+\s*~\s*\d+\s*세\s*남성\s*외국인"
    r"|1인\s*가구|총인구|이주배경|장애인|미혼모|미혼부"
)
WHOLE_BEFORE = re.compile(r"전체\s*$")


def _text(ind: dict[str, Any]) -> str:
    return f"{ind.get('span') or ''} {ind.get('search') or ''}"


def fix_indicators(question: str, p: Plan) -> list[tuple]:
    """빠진 지표 채우기, 범위 밖으로 잘못 본 인구 지표 되돌리기, 파생 지표 풀기"""
    fixes: list[tuple] = []
    shared = p["shared"] = p.get("shared") or {}
    inds: list[dict[str, Any]] = p.setdefault("indicators", [])

    covered = " ".join(_text(i) for i in inds)
    for m in INDICATOR_TERMS.finditer(question):
        term = m[0]
        if search.norm(term) not in search.norm(covered):
            inds.append(
                {"span": term, "search": term, "scope": "in", "role": "measure", "override": {}}
            )
            covered += " " + term
            fixes.append(("빠진 지표 추가", term))

    in_scope = [i for i in inds if i.get("scope") != "out"]
    for ind in inds:
        text = _text(ind)
        if (
            ind.get("scope") == "out"
            and POPULATION_WORD.search(text)
            and not NOT_POPULATION.search(text)
        ):
            ind["scope"] = "in"
            fixes.append(("범위 안으로", ind.get("search")))
        only_one = len(in_scope) == 1
        add = [
            canon
            for pattern, canon in DERIVED
            if re.search(pattern, text) or (only_one and re.search(pattern, question))
        ]
        if add:
            ind["search"] = " ".join([ind.get("search") or "", *dict.fromkeys(add)]).strip()

    for ind in in_scope:
        span = ind.get("span") or ""
        at = question.find(span)
        after_whole = at > 0 and WHOLE_BEFORE.search(question[max(0, at - 4) : at])
        if after_whole or span.startswith("전체"):
            keep = list(ind.get("no_inherit") or [])  # 현재 시점 지표의 연도 칸은 남긴다
            ind["no_inherit"] = [*keep, *(k for k in ("sex", "age") if k not in keep)]
            fixes.append(("전체 지표는 성별·연령 안 물려받음", span))

    asks_region = REGION_ASK.search(question)
    if (
        not _level(question)
        and asks_region
        and shared.get("region_level")
        in (
            None,
            "",
            "없음",
            "읍면동",
        )
    ):
        fixes.append(("지역 기본 시도", shared.get("region_level")))
        shared["region_level"] = "시도"
    return fixes


# ---- 지표 하나를 단일 질문 문장으로 -----------------------------------------------------

CONDITION_KEYS = ("region_level", "year_from", "year_to", "sex", "age")


def merged_conditions(
    shared: dict[str, Any] | None,
    override: dict[str, Any] | None,
    no_inherit: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """지표별 조건 > 공통 조건. '없음'·빈 값은 없는 것으로 본다.
    no_inherit에 든 칸은 공통 조건을 물려받지 않는다"""
    out: dict[str, Any] = {}
    for k in CONDITION_KEYS:
        for src in (override or {}, {} if k in no_inherit else (shared or {})):
            v = src.get(k)
            if v not in (None, "", "없음", 0):
                out[k] = v
                break
    return out


def render(ind: dict[str, Any], cond: dict[str, Any]) -> str:
    """지표 검색어 + 조건 → 표 하나를 찾는 문장 ('2015~2025년 시군구별 80세 이상 남자 사망자 수')"""
    parts: list[str] = []
    year_from, year_to = cond.get("year_from"), cond.get("year_to")
    if year_from and year_to and year_from != year_to:
        parts.append(f"{year_from}~{year_to}년")
    elif year_from or year_to:
        parts.append(f"{year_from or year_to}년")
    text = ind["search"]
    if cond.get("region_level") and not re.search(r"시도|시군구|읍면동|구별|군별", text):
        parts.append(f"{cond['region_level']}별")
    if cond.get("age") and cond["age"] not in text:
        parts.append(cond["age"])
    if cond.get("sex") in ("남", "여") and not re.search("남|여", text):
        parts.append("남자" if cond["sex"] == "남" else "여자")
    elif cond.get("sex") == "남녀" and "성별" not in text:
        parts.append("성별")
    return " ".join([*parts, text])
