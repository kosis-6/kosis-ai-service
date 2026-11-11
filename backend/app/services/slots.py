"""질의 분석: 질문 → 슬롯 JSON (실전 2 F-03)

  1. HCX-007이 질문을 슬롯 JSON으로 바꾼다 (JSON을 못 읽으면 1회 재시도)
     검색마다 맨 앞에서 부르므로 추론(thinking)은 끈다 (llm_pick과 같음)
  2. 코드가 질문 원문과 맞춰 고친다 (correct)
     - 연도·지역 수준·성별은 질문에 그 말이 있을 때만 남긴다 (LLM이 추측한 조건 제거)
     - 지역 이름은 KOSIS 분류 값과 같은 정식 시도명으로 바꾼다 ("부산" → "부산광역시")
     - 팀원 검색 실험(10/31)의 오답 분석에서 나온 규칙: 두 자리 연도("95년"), 미래 연도·예측
       표현 → 장래 추계(projection), "우리 구"·"동네" → 시군구, 부정어("귀화한 사람 말고"
       → exclude), 배우자 역할쌍("외국인 아내" ↔ "한국인 남편" → spouse)
  3. 두 번 다 JSON을 못 읽거나 HCX 호출이 실패하면 원문 규칙만으로 슬롯을 만든다
     (parse_error=True, 검색은 원문으로)

말하지 않은 조건은 null로 둔다. "전체"와 구분해야 재순위(F-05)가 감점하지 않고,
되묻기(F-14)가 무엇을 물을지 안다.

실행: python -m app.services.slots "2024년 부산 시군구별 혼인 귀화자 수"
"""

import asyncio
import json
import re
import sys
from datetime import date
from typing import Any

from app.prompts.search import SLOT_SYSTEM
from app.services import agegroup, timeref
from app.services.hcx_client import HcxClient, HcxError

MODEL = "HCX-007"
QUERY_TYPES = {"find_table", "ambiguous", "not_statistics"}
REGION_LEVELS = ("시도", "시군구", "읍면동")

SIDO = {
    "서울특별시": ["서울"],
    "부산광역시": ["부산"],
    "대구광역시": ["대구"],
    "인천광역시": ["인천"],
    "광주광역시": ["광주"],
    "대전광역시": ["대전"],
    "울산광역시": ["울산"],
    "세종특별자치시": ["세종"],
    "경기도": ["경기"],
    "강원특별자치도": ["강원", "강원도"],
    "충청북도": ["충북"],
    "충청남도": ["충남"],
    "전북특별자치도": ["전북", "전라북도"],
    "전라남도": ["전남"],
    "경상북도": ["경북"],
    "경상남도": ["경남"],
    "제주특별자치도": ["제주", "제주도"],
}
SIDO_ALIAS = {alias: name for name, aliases in SIDO.items() for alias in [name, *aliases]}
# '서울시'·'세종시'처럼 광역 지자체 뒤에 '시'만 붙인 말도 시도다
# ('세종시 2013년 나이별 성별 인구'가 시군구로 잡혀 세종시 특별센서스 표가 '시군구 ✘'로 나오던 문제)
# '광주시'는 경기도 광주시(시군구)와 겹쳐 넣지 않는다 (광주광역시는 '광주'·'광주광역시'로 잡힌다)
for _name, _aliases in SIDO.items():
    if _name.endswith(("특별시", "광역시", "특별자치시")) and _aliases[0] != "광주":
        SIDO_ALIAS.setdefault(_aliases[0] + "시", _name)

YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# '95년', "'05년도". '10년 전', '20년간', '90년생', '80년대', '10년 넘게'는 연도가 아니다
SHORT_YEAR = re.compile(
    r"(?<![\d~])'?(\d{2})\s*년(?!\s*(?:전|후|뒤|간|동안|생|대|째|만|마다|이상|이하|이내|미만|차|넘|넘게|넘는|넘은|가까이|정도|쯤|남짓))"
)
SHORT_YEAR_PIVOT = 50  # 50 미만은 20xx, 이상은 19xx ('45년' → 2045 추계, '95년' → 1995)
PROJECTION = re.compile(r"앞으로|예측|전망|미래|추계|장래|될까|될지|예상")
# '귀화한 사람 말고', '외국인 빼고', '서울 제외하고'
NEGATION_WORDS = r"(?:말고|빼고|빼면|뺀|제외하고|제외한|제외하면|제외|제하고|아닌|아니고|아니라|이외의|이외에|외의)"
NEGATION = re.compile(
    r"([가-힣A-Za-z0-9]+(?:\s+[가-힣A-Za-z0-9]+)?)\s*(?:은|는|을|를|이|가)?\s*" + NEGATION_WORDS
)
# 나열한 것을 한꺼번에 빼는 말: '서울이랑 부산 말고', '서울, 부산, 대구 빼고', '귀화자나 결혼이민자 말고'
NEG_LINK = r"(?:이랑|랑|하고|과|와|이나|나|및|또는)"
NEG_LINK_WORDS = {"및", "또는", "그리고", "혹은"}  # 띄어 쓰는 잇는 말
NEG_CLAUSE_WORDS = {"단", "다만", "하지만", "그런데", "근데", "대신"}  # 앞 문장과 끊는 말
# 나열 항목 끝에 붙은 잇는 말 ('서울이랑' → '서울'). 떼고 두 글자 이상 남을 때만
NEG_LINK_TAIL = re.compile(NEG_LINK + r"$")
# 빼 달라는 말 앞의 단어 중 대상이 아닌 것.
# '한국'은 '한국국적을 가지지 않은 자' 표명에 있어 빼면 안 된다.
# 동사 부정('결혼 안 한', '귀화하지 않은', '소화기가 없는')은 같은 축의 반대 값이라 단어를 빼지 않고
# 사전('결혼 안 한' → 미혼)으로 푼다. 그래서 않은·안 한·없는은 여기 넣지 않는다
NEGATION_SKIP = {
    "사람", "것", "거", "분", "애들", "통계", "표", "자료", "이상", "이하", "미만",
    "한국", "우리나라", "국내", "중에", "중", "그", "이", "저", "다른", "같은",
}  # fmt: skip
# 국제결혼 표는 외국인 배우자 쪽으로 나뉜다
# ('외국인 아내의 국적별 혼인' / '외국인 남편의 국적별 혼인').
# '외국인 여자와 결혼한 한국 남자' = 외국인 아내. 결혼·이혼 질문에서만 본다
MARRIAGE = re.compile(r"결혼|혼인|이혼|배우자|부부|신랑|신부")
FOREIGN_WIFE = re.compile(
    r"(?:외국인?|다문화)\s*(?:아내|부인|여자|여성|신부|와이프)|(?:한국인?|내국인)\s*(?:남편|남자|남성|신랑)"
)
FOREIGN_HUSBAND = re.compile(
    r"(?:외국인?|다문화)\s*(?:남편|남자|남성|신랑)|(?:한국인?|내국인)\s*(?:아내|부인|여자|여성|신부|와이프)"
)
# '시군구별', '구별', '시군별'. '가구별'의 '구별'은 지역이 아니므로 앞 글자가 한글이면 안 된다
# '우리 구', '구 단위' → 시군구, '동 단위' → 읍면동. '동네'는 읍면동이 아니라 시군구로 본다
# (동네 주민이 찾는 자료는 대개 시군구 표, 팀원 검색 실험 10/31)
LEVEL_WORDS = [
    ("읍면동", re.compile(r"읍면동|(?<![가-힣])(?:읍|면|동)별|(?<![가-힣])(?:읍|면|동)\s*단위")),
    (
        "시군구",
        re.compile(
            r"시군구|시·군·구|(?<![가-힣])(?:구|시군|군)별|(?<![가-힣])(?:구|군)\s*단위"
            r"|우리\s*(?:구|군)(?:(?![가-힣])|(?=(?:로|에서|에|는|은|의|가|를|도)(?![가-힣])))|동네"
        ),
    ),
    ("시도", re.compile(r"시도|시·도")),
]
MALE = re.compile(r"남자|남성|남아")
FEMALE = re.compile(r"여자|여성|여아")
BOTH_SEX = re.compile(r"남녀|성별")
TOTAL_SEX = re.compile(r"남녀\s*(?:합계|전체|합산)|성별\s*(?:합계|전체)")
SEX_VALUES = {"남", "여", "남자", "여자", "남성", "여성", "계", "전체"}
REGION_CLASS = re.compile(
    r"(?:시도|시군구|읍면동|지역|행정구역|시·도|시·군·구|구|시군|군|읍|면|동)별?"
)
AGE = re.compile(r"\d+\s*세")
# 표에서 고를 값이 아니라 표의 값으로 계산 · 분석할 내용 ('성별 비율', '상위 3개국', '시점별').
# '증가율' · '평균연령'처럼 그 자체가 항목 이름인 말은 넣지 않는다
ANALYSIS = re.compile(
    r"비율|비중|순위|상위\s*\d*\s*(?:개국|개)?|하위\s*\d*\s*(?:개국|개)?|추이|변화|시점별|시계열|"
    r"가장\s*(?:많은|높은|낮은|적은)"
)
# '여성 가구주'는 가구주의 성별 분류이지 성별 조건이 아니다 (검색 규칙 4장)
HOUSEHOLDER_SEX = re.compile(r"(?:남자|남성|여자|여성)\s*가구주")


def empty_slots(question: str) -> dict[str, Any]:
    return {
        "question": question.strip(),
        "topic": question.strip(),
        "target": None,
        "conditions": {
            "classes": [],
            "items": [],
            "sex": None,
            "region": None,
            "region_level": None,
            "year": None,
            "year_end": None,
        },
        "query_type": "find_table",
        "parse_error": False,
        "projection": False,  # 장래 추계를 찾는 질문
        "exclude": [],  # 빼 달라고 한 단어 ('귀화한 사람 말고' → ['귀화한'])
        "llm_exclude": [],  # HCX 가 준 exclude 원본 (correct 가 검증해 exclude 에 합친다)
        "spouse": None,  # '외국인 아내' / '외국인 남편'
        # 연령 집단 이름 ('노인' → {"expr": "노인", "lo": 65, "hi": None, "basis": ...})
        "age": None,
        "search_query": None,  # HCX 재작성 검색어 (임베딩 평균용)
        "time_ref": None,  # 상대시점 ('작년' → {"expr": "작년", "year": 2025, "open_end": False})
    }


async def _chat(question: str) -> str:
    result = await HcxClient().chat(
        MODEL,
        [
            {"role": "system", "content": SLOT_SYSTEM},
            {"role": "user", "content": question},
        ],
        thinking={"effort": "none"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=512,
        temperature=0.1,
        repetitionPenalty=1.1,
    )
    return result.content


def _json_object(text: str) -> dict[str, Any] | None:
    """HCX 출력 속 첫 JSON 객체 (코드 블록이나 설명 문장이 붙어 와도 읽는다)"""
    decoder, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            return obj
        i += 1
    return None


async def extract(question: str, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    """질문 → 슬롯. trace(dict)를 주면 HCX 원본 출력을 담는다 (요청 로그용)"""
    raws = []
    parsed = None
    try:
        for _ in range(2):  # JSON 형식이 틀리면 1회 재시도
            raws.append(await _chat(question))
            parsed = _json_object(raws[-1])
            if parsed is not None:
                break
    except HcxError as e:  # HCX 장애·호출 제한이어도 원문 규칙으로 검색은 이어간다
        raws.append(f"HcxError: {e}")
    if trace is not None:
        trace["hcx_raw"] = raws
    if parsed is None:
        slots = empty_slots(question)
        slots["parse_error"] = True
        return correct(question, slots)
    return correct(question, normalize(question, parsed))


def normalize(question: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """HCX 출력을 정해진 형식으로 맞춘다 (빠진 키, 잘못된 타입, 빈 문자열 → null)"""
    slots = empty_slots(question)
    cond = parsed.get("conditions") if isinstance(parsed.get("conditions"), dict) else {}
    slots["topic"] = _text(parsed.get("topic")) or question.strip()
    slots["search_query"] = _text(parsed.get("search_query"))
    slots["target"] = _text(parsed.get("target"))
    slots["llm_exclude"] = _texts(parsed.get("exclude"))  # correct() 가 질문 원문과 대조해 받는다
    if parsed.get("query_type") in QUERY_TYPES:
        slots["query_type"] = parsed["query_type"]
    c = slots["conditions"]
    c["classes"] = _texts(cond.get("classes"))
    c["items"] = _texts(cond.get("items"))
    c["sex"] = cond.get("sex") if cond.get("sex") in ("전체", "남", "여") else None
    c["region"] = _text(cond.get("region"))
    c["region_level"] = (
        cond.get("region_level") if cond.get("region_level") in REGION_LEVELS else None
    )
    c["year"] = _year(cond.get("year"))
    c["year_end"] = _year(cond.get("year_end"))
    return slots


def correct(question: str, slots: dict[str, Any]) -> dict[str, Any]:
    """질문 원문과 맞춰 슬롯을 고친다. 고친 내용은 slots["fixes"]에 남긴다"""
    c = slots["conditions"]
    fixes = []

    def fix(key: str, value: Any, reason: str) -> None:
        if c[key] != value:
            fixes.append({"key": key, "from": c[key], "to": value, "reason": reason})
            c[key] = value

    # 연도: 질문에 쓰인 연도만 인정한다 (네 자리, '95년' 같은 두 자리)
    today = date.today()
    years = sorted({int(y) for y in YEAR.findall(question)} | short_years(question))
    slots["time_ref"] = None
    if not years and (
        ref := timeref.recent_period(question, today) or timeref.resolve(question, today)
    ):
        # 상대시점("작년", "2년 전부터")은 LLM이 아니라 오늘 날짜로 코드가 푼다. 끝이 열린 기간은
        # 최신까지
        years = [ref.year]
        slots["time_ref"] = {"expr": ref.expr, "year": ref.year, "open_end": ref.open_end}
        fix("year", ref.year, ref.describe(today))
        fix("year_end", None, ref.describe(today))
    else:
        fix("year", years[0] if years else None, "질문의 연도")
        fix("year_end", years[-1] if len(years) > 1 else None, "질문의 기간")
    # 장래 추계: 올해 이후 연도이거나 예측 표현
    slots["projection"] = bool(PROJECTION.search(question) or (years and years[-1] > today.year))
    # 빼 달라는 대상: 규칙(negated_words) + HCX exclude 중 질문에 그 낱말이 있고 뒤에 빼는 말이 오는 것
    rule_words = negated_words(question)
    llm_words = [w for w in slots.get("llm_exclude") or [] if llm_excluded_ok(question, w)]
    slots["exclude"] = list(dict.fromkeys(rule_words + [w for w in llm_words if not _covered(w, rule_words)]))
    if dropped := [w for w in slots.get("llm_exclude") or [] if w not in llm_words]:
        fixes.append({"key": "exclude", "from": dropped, "to": None, "reason": "질문에 없거나 빼는 말이 뒤따르지 않음"})
    # 연령 집단 이름("노인", "청년")은 법령·통계 정의 사전으로 구간을 정한다. 숫자 구간이 있으면
    # 그쪽이 우선
    age = agegroup.resolve(question)
    slots["age"] = (
        {"expr": age.expr, "lo": age.lo, "hi": age.hi, "basis": age.basis} if age else None
    )

    # 성별: 한 성별만 말했으면 그 성별, 남녀로 나눠 보려면 성별 분류, 말이 없으면 null
    if HOUSEHOLDER_SEX.search(question) and "가구주 성별" not in c["classes"]:
        c["classes"].append("가구주 성별")
    plain = HOUSEHOLDER_SEX.sub("", question)
    # 배우자 역할쌍의 남녀 단어('외국인 여자')는 배우자를 가리키지 성별 조건이 아니다
    slots["spouse"] = spouse_of(question)
    if slots["spouse"]:
        plain = FOREIGN_HUSBAND.sub("", FOREIGN_WIFE.sub("", plain))
    male, female = bool(MALE.search(plain)), bool(FEMALE.search(plain))
    if TOTAL_SEX.search(plain):
        fix("sex", "전체", "남녀 합계 명시")
    elif male != female:
        fix("sex", "남" if male else "여", "한 성별만 언급")
    elif (male and female) or BOTH_SEX.search(plain):
        fix("sex", None, "남녀를 나눠 보는 질문")
        if "성별" not in c["classes"]:
            c["classes"].append("성별")
    elif c["sex"] is not None:
        fix("sex", None, "성별 언급 없음")

    # 지역 이름 → 정식 시도명
    region = c["region"]
    if region and region.replace(" ", "") in SIDO_ALIAS:
        fix("region", SIDO_ALIAS[region.replace(" ", "")], "시도 정식 이름")
    # '우리 구로 이사 오는': '구로'는 '우리 구' + 조사 '로'다. HCX 가 '구로'(구로구)로
    # 읽으면 지운다 (질문에 '구로구'가 따로 있으면 그대로)
    if (
        c["region"]
        and (m := OUR_DISTRICT.search(question))
        and c["region"].replace(" ", "") in (m[1] + (m[2] or ""), m[1])
        and f"{c['region']}구" not in question
    ):
        fix("region", None, "'우리 구'의 조사를 지역 이름으로 읽음")
    # '서울 말고 시도별 인구': 빼 달라고 한 지역을 HCX 가 지역 슬롯에 넣었으면 지운다
    excluded_regions = {SIDO_ALIAS.get(w.rstrip("은는을를이가"), w) for w in slots["exclude"]}
    if c["region"] and c["region"] in excluded_regions:
        fix("region", None, "빼 달라고 한 지역")

    # 지역 수준: '~별' 표현과 지역 이름이 요구하는 수준 중 더 세밀한 쪽 ('부산 구별' → 시군구)
    level = next((lv for lv, pattern in LEVEL_WORDS if pattern.search(question)), None)
    if c["region"] and (named := _level_of(c["region"])):
        level = _deeper(level, named)
    if level is None and c["region_level"] is not None:
        fix("region_level", None, "지역 수준 언급 없음")
    elif level is not None:
        fix("region_level", level, "질문의 지역 수준")

    # 대상: 성별 단어는 sex가, 연령 범위('65세 이상')는 분류 값이라 items가 맡는다
    target = slots["target"]
    if target in SEX_VALUES:
        slots["target"] = None
        fixes.append({"key": "target", "from": target, "to": None, "reason": "성별은 sex"})
    elif (
        target
        and slots["spouse"]
        and (FOREIGN_WIFE.search(target) or FOREIGN_HUSBAND.search(target))
    ):
        slots["target"] = None
        fixes.append({"key": "target", "from": target, "to": None, "reason": "배우자는 spouse"})
    elif target and AGE.search(target):
        slots["target"] = None
        c["items"] = [*c["items"], target]
        fixes.append({"key": "target", "from": target, "to": None, "reason": "연령은 items"})

    # 계산 · 분석 표현은 표의 조건이 아니다 (외부 질문 '상위 3개국 ✘', '성별 비율 ✘', '시점별 ✘').
    # 빼고 slots["analysis"]에 남겨 답변에 '직접 계산' 안내를 단다. 남는 말이 있으면 그 말만
    # ('성별 비율' → 분류 '성별')
    analysis: list[str] = []
    for key in ("classes", "items"):
        kept = []
        for v in c[key]:
            if not ANALYSIS.search(v):
                kept.append(v)
                continue
            analysis.append(v)
            rest = re.sub(r"\s+", " ", ANALYSIS.sub(" ", v)).strip(" 의및와과")
            if rest in ("성별", "성"):
                if "성별" not in c["classes"]:
                    c["classes"] = [*c["classes"], "성별"]
            elif len(re.sub(r"[^가-힣A-Za-z0-9]", "", rest)) >= 2:
                kept.append(rest)
        fix(key, kept, "계산 · 분석 표현은 조건이 아님")
    slots["analysis"] = analysis

    # 분류·값 목록: 지역·성별은 위 슬롯이 맡고, 주제 자체는 조건이 아니다
    fix(
        "classes",
        [v for v in c["classes"] if not REGION_CLASS.fullmatch(v)],
        "지역 분류는 지역 수준",
    )
    fix(
        "items",
        [v for v in c["items"] if v not in SEX_VALUES and v != slots["topic"]],
        "성별 값·주제 제외",
    )

    slots["fixes"] = fixes
    return slots


def short_years(question: str) -> set[int]:
    """'95년' → 1995, '05년도' → 2005. '최근 10년'·'지난 20년간'은 기간이라 연도가 아니다"""
    question = timeref.RECENT_N_YEARS.sub(" ", question)
    return {
        (2000 if int(yy) < SHORT_YEAR_PIVOT else 1900) + int(yy)
        for yy in SHORT_YEAR.findall(question)
    }


NEG_AFTER = re.compile(NEGATION_WORDS + r"|빼|제외")
NEG_WINDOW = 20  # 낱말 뒤 이 글자 수 안에 빼는 말이 있어야 HCX exclude 를 받는다


def llm_excluded_ok(question: str, word: str) -> bool:
    """HCX 가 준 exclude 낱말 검증: 질문에 그대로 있고, 그 뒤 가까이에 빼는 말이 온다.
    연도처럼 LLM 이 질문에 없는 대상을 추측해 넣거나, 찾으려는 대상('귀화자가 아닌 외국인'의
    '외국인')을 잘못 넣으면 정답 표가 감점되므로 버린다"""
    w = word.strip()
    if len(w) < 2 or w in NEGATION_SKIP:
        return False
    for m in re.finditer(re.escape(w), question):
        if NEG_AFTER.search(question[m.end() : m.end() + NEG_WINDOW]):
            return True
    return False


def _covered(word: str, words: list[str]) -> bool:
    """규칙이 이미 뺀 낱말과 같은지 ('결혼이민자' ≈ 규칙의 '결혼이민자를')"""
    return any(r.startswith(word) or word.startswith(r) for r in words)


def _strip_link(word: str) -> str:
    """나열을 잇는 말을 뗀다 ('서울이랑' → '서울', '귀화자나' → '귀화자'). 두 글자 이상 남을 때만"""
    m = NEG_LINK_TAIL.search(word)
    return word[: m.start()] if m and m.start() >= 2 else word


def negated_words(question: str) -> list[str]:
    """'귀화한 사람 말고 결혼이민자' → ['귀화한'].
    나열도 한꺼번에 뺀다: '서울이랑 부산 말고' → ['서울', '부산'], '서울, 부산, 대구 빼고' → 셋 다,
    '서울 경기 인천 빼고'처럼 띄어 쓴 나열은 모두 시도 이름일 때만 앞으로 늘린다"""
    words = []
    for m in NEGATION.finditer(question):
        phrase = m.group(1).split()
        # 바로 앞 한두 단어(기존 규칙) 앞으로, 잇는 말·쉼표로 이어진 나열 항목을 거슬러 올라간다
        before = re.findall(r"[가-힣A-Za-z0-9]+|[,·/]", question[: m.start(1)])
        regionish = all(_strip_link(w).rstrip("은는을를이가") in SIDO_ALIAS for w in phrase)
        linked = False
        # '유학생 및 결혼이민자', '유학생 또는 결혼이민자': 따로 쓴 잇는 말은 쉼표처럼 다룬다
        if phrase and phrase[0] in NEG_LINK_WORDS:
            phrase.pop(0)
            linked = True
        # '…외국인 주민, 단 유학생은 제외': '단·다만'으로 시작하면 앞 쉼표는 나열이 아니라 문장 경계
        if phrase and phrase[0] in NEG_CLAUSE_WORDS:
            phrase.pop(0)
            before = []
        while before:
            tok = before.pop()
            if tok in ",·/" or tok in NEG_LINK_WORDS:
                linked = True
                continue
            if linked or _strip_link(tok) != tok or (regionish and tok in SIDO_ALIAS):
                phrase.insert(0, tok)
                linked = False
                continue
            break
        after = question[m.end() :]
        words += [
            w for w in (_strip_link(w) for w in phrase)
            if w not in NEGATION_SKIP and len(w) >= 2
            # 숫자는 빼는 대상이 아니다 ('65세 이상 말고'). 단 '1인가구'·'2인 가구'는 낱말이다
            and (not re.search(r"\d", w) or re.fullmatch(r"\d+인[가-힣]*", w))
            # 빼는 말 뒤에 같은 낱말이 또 나오면 공통 수식어다 ('외국인 남편 말고 외국인 아내'의 '외국인')
            and w.rstrip("은는을를이가") not in after
        ]  # fmt: skip
    return list(dict.fromkeys(words))


def spouse_of(question: str) -> str | None:
    """결혼·이혼 질문의 외국인 배우자 쪽: '외국인 아내' / '외국인 남편' / None"""
    if not MARRIAGE.search(question):
        return None
    wife, husband = bool(FOREIGN_WIFE.search(question)), bool(FOREIGN_HUSBAND.search(question))
    if wife == husband:  # 둘 다 없거나 둘 다 있으면 정하지 않는다
        return None
    return "외국인 아내" if wife else "외국인 남편"


OUR_DISTRICT = re.compile(r"우리\s*(구|군)(로|에서|에|는|은|의|가|를|도)?(?![가-힣])")


def _level_of(region: str) -> str | None:
    """지역 이름이 요구하는 지역 수준 ('부산광역시' → 시도, '해운대구' → 시군구)"""
    last = region.split()[-1]
    if region in SIDO and len(region.split()) == 1:
        return "시도"
    if last.endswith(("읍", "면", "동")):
        return "읍면동"
    if last.endswith(("시", "군", "구")):
        return "시군구"
    return None


def _deeper(a: str | None, b: str) -> str:
    """두 지역 수준 중 더 세밀한 쪽"""
    if a is None:
        return b
    return max(a, b, key=REGION_LEVELS.index)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if value in ("", "null", "없음") else value


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for v in value if (t := _text(v))]


def _year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "2024년 부산 시군구별 혼인 귀화자 수"
    trace: dict[str, Any] = {}
    print(json.dumps(asyncio.run(extract(question, trace)), ensure_ascii=False, indent=2))
    print("\nHCX 원본:", trace["hcx_raw"])
