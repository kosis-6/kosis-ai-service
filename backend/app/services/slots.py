import asyncio
import json
import re
import sys
from datetime import date
from typing import Any

from app.prompts.search import SLOT_SYSTEM, SLOT_SYSTEM_GEMMA
from app.services import agegroup, llm_calls, search_llm, synonyms, timeref, understand_merged
from app.services.hcx_client import HcxError

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
for _name, _aliases in SIDO.items():
    if _name.endswith(("특별시", "광역시", "특별자치시")) and _aliases[0] != "광주":
        SIDO_ALIAS.setdefault(_aliases[0] + "시", _name)

COUNT_UNIT = r"(?:명|가구|개월|개|건|원|곳|채|톤|평|달러|회|번|위|배|시간|일|마리|권|호)"
LATIN_UNIT = r"(?:%|㎡|㎢|km|kg|ha|m²)"
COMPARE_WORD = r"(?:이상|이하|미만|초과|이내|넘|정도|가량|남짓|안팎|내외|쯤|보다|가까이)"
COUNT_SUFFIX = r"(?:당|씩|짜리|규모|수준|대|째)"
COUNT_JOSA = r"(?:이나|에서|으로|에게|까지|부터|보다|[이을은의도가인를에만로과와])"
UNIT_END = rf"(?:{COUNT_SUFFIX}?{COUNT_JOSA}?(?![가-힣])|{COMPARE_WORD})"
COUNT_PREFIX = r"(?:[만천여]\s*){0,2}"
ATTACHED_COUNT = rf"(?:{COUNT_PREFIX}{COUNT_UNIT}{UNIT_END}|\s*{LATIN_UNIT}|여(?![가-힣]))"
SPACED_COUNT = (
    rf"\s+{COUNT_PREFIX}"
    rf"(?:명{UNIT_END}|{COUNT_UNIT}(?:이|을|은|가|를)?\s*{COMPARE_WORD})"
)
COUNT_RANGE = rf"\s*(?:~|-|에서|부터)\s*\d[\d,]*\s*{COUNT_PREFIX}(?:{COUNT_UNIT}|{LATIN_UNIT})"
BARE_COMPARE = r"\s*(?:이상|이하|미만|초과|넘)"
COHORT_TAIL = r"\s*년(?:대(?!비)|생)"
NOT_YEAR_TAIL = (
    rf"(?!\d|\s*세대|{ATTACHED_COUNT}|{SPACED_COUNT}|{COUNT_RANGE}|{BARE_COMPARE}|{COHORT_TAIL})"
)
YEAR = re.compile(rf"(?<!\d)((?:19|20)\d{{2}}){NOT_YEAR_TAIL}")
SHORT_YEAR = re.compile(
    r"(?<![\d~])'?(\d{2})\s*년(?!\s*(?:전|후|뒤|간|동안|생|대|째|만|마다|이상|이하|이내|미만|차|넘|넘게|넘는|넘은|가까이|정도|쯤|남짓))"
)
DECADE_PART = (
    r"(?:\s*(초반|중반|후반|말기|초|말)"
    r"(?=$|[^가-힣]|에|의|부터|까지|은|는|도|을|를|이|가|과|와|쯤|무렵))?"
)
DECADE = re.compile(rf"(?<!\d)((?:19|20)\d0)\s*년대(?!비|생){DECADE_PART}")
SHORT_DECADE = re.compile(rf"(?<![\d~])'?(\d0)\s*년대(?!비|생){DECADE_PART}")
DECADE_PARTS = {
    "초반": (0, 3), "초": (0, 3), "중반": (4, 6), "후반": (7, 9), "말": (7, 9), "말기": (7, 9),
}  # fmt: skip
BIRTH_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*년(대)?생")
SHORT_BIRTH_YEAR = re.compile(r"(?<![\d~])'?(\d{2})\s*년(대)?생")
SHORT_YEAR_PIVOT = 50
PROJECTION = re.compile(r"앞으로|예측|전망|미래|추계|장래|될까|될지|예상")
NEGATION_WORDS = (
    r"(?:말고|빼고|빼면|뺀|제외하고|제외한|제외하면|제외|제하고|아닌|아니고|아니라|"
    r"이외의|이외에|외의)"
)
NEGATION = re.compile(
    r"([가-힣A-Za-z0-9]+(?:\s+[가-힣A-Za-z0-9]+)?)\s*(?:은|는|을|를|이|가)?\s*" + NEGATION_WORDS
)
NEG_LINK = r"(?:이랑|랑|하고|과|와|이나|나|및|또는)"
NEG_LINK_WORDS = {"및", "또는", "그리고", "혹은"}
NEG_CLAUSE_WORDS = {"단", "다만", "하지만", "그런데", "근데", "대신"}
NEG_LINK_TAIL = re.compile(NEG_LINK + r"$")
NEGATION_SKIP = {
    "사람", "것", "거", "분", "애들", "통계", "표", "자료", "이상", "이하", "미만",
    "한국", "우리나라", "국내", "중에", "중", "그", "이", "저", "다른", "같은",
}  # fmt: skip
MARRIAGE = re.compile(r"결혼|혼인|이혼|배우자|부부|신랑|신부")
FOREIGN_WIFE = re.compile(
    r"(?:외국인?|다문화)\s*(?:아내|부인|여자|여성|신부|와이프)|(?:한국인?|내국인)\s*(?:남편|남자|남성|신랑)"
)
FOREIGN_HUSBAND = re.compile(
    r"(?:외국인?|다문화)\s*(?:남편|남자|남성|신랑)|(?:한국인?|내국인)\s*(?:아내|부인|여자|여성|신부|와이프)"
)
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
BOTH_SEX = re.compile(r"남녀|(?<![구특])성별")
TOTAL_SEX = re.compile(r"남녀\s*(?:합계|전체|합산)|(?<![구특])성별\s*(?:합계|전체)")
SEX_VALUES = {"남", "여", "남자", "여자", "남성", "여성", "계", "전체"}
REGION_CLASS = re.compile(
    r"(?:시도|시군구|읍면동|지역|행정구역|시·도|시·군·구|구|시군|군|읍|면|동)별?"
)
AGE = re.compile(r"\d+\s*세")
ANALYSIS = re.compile(
    r"비율|비중|순위|상위\s*\d*\s*(?:개국|개)?|하위\s*\d*\s*(?:개국|개)?|추이|변화|시점별|시계열|"
    r"가장\s*(?:많은|높은|낮은|적은)"
)
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
        "projection": False,
        "exclude": [],
        "llm_exclude": [],
        "spouse": None,
        "age": None,
        "search_query": None,
        "time_ref": None,
        "decades": [],
        "birth_years": [],
    }


@llm_calls.staged("slots")
async def _chat(question: str) -> str:

    def request(llm: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        system = SLOT_SYSTEM_GEMMA if llm == "gemma" else SLOT_SYSTEM
        messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        return messages, {
            "thinking": {"effort": "none"},
            "topP": 0.8,
            "topK": 0,
            "maxCompletionTokens": 512,
            "temperature": 0.1,
            "repetitionPenalty": 1.1,
        }

    result, _ = await search_llm.ask(MODEL, request)
    return result.content


def _json_object(text: str) -> dict[str, Any] | None:
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
    if (kept := understand_merged.take(question)) is not None:
        if trace is not None:
            trace["hcx_raw"] = ["(질문 이해에서 함께 뽑음)"]
        return kept
    raws = []
    parsed = None
    try:
        for _ in range(2):
            raws.append(await _chat(question))
            parsed = _json_object(raws[-1])
            if parsed is not None:
                break
    except HcxError as e:
        raws.append(f"HcxError: {e}")
    if trace is not None:
        trace["hcx_raw"] = raws
    if parsed is None:
        slots = empty_slots(question)
        slots["parse_error"] = True
        return correct(question, slots)
    return correct(question, normalize(question, parsed))


def normalize(question: str, parsed: dict[str, Any]) -> dict[str, Any]:
    slots = empty_slots(question)
    cond = parsed.get("conditions") if isinstance(parsed.get("conditions"), dict) else {}
    slots["topic"] = _text(parsed.get("topic")) or question.strip()
    slots["search_query"] = _text(parsed.get("search_query"))
    slots["target"] = _text(parsed.get("target"))
    slots["llm_exclude"] = _texts(parsed.get("exclude"))
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
    c = slots["conditions"]
    fixes = []

    def fix(key: str, value: Any, reason: str) -> None:
        if c[key] != value:
            fixes.append({"key": key, "from": c[key], "to": value, "reason": reason})
            c[key] = value

    today = date.today()
    spans = decades(question, today)
    years = sorted(
        {int(y) for y in YEAR.findall(question)}
        | short_years(question)
        | {y for span in spans for y in span}
    )
    slots["time_ref"] = None
    if not years and (
        ref := timeref.recent_period(question, today) or timeref.resolve(question, today)
    ):
        years = [ref.year]
        slots["time_ref"] = {"expr": ref.expr, "year": ref.year, "open_end": ref.open_end}
        fix("year", ref.year, ref.describe(today))
        fix("year_end", None, ref.describe(today))
    else:
        fix("year", years[0] if years else None, "질문의 연도")
        fix("year_end", years[-1] if len(years) > 1 else None, "질문의 기간")
    slots["decades"] = [list(span) for span in spans]
    slots["birth_years"] = sorted(birth_years(question))
    slots["projection"] = bool(PROJECTION.search(question) or (years and years[-1] > today.year))
    rule_words = negated_words(question)
    llm_words = [w for w in slots.get("llm_exclude") or [] if llm_excluded_ok(question, w)]
    slots["exclude"] = list(
        dict.fromkeys(rule_words + [w for w in llm_words if not _covered(w, rule_words)])
    )
    if dropped := [w for w in slots.get("llm_exclude") or [] if w not in llm_words]:
        fixes.append(
            {
                "key": "exclude",
                "from": dropped,
                "to": None,
                "reason": "질문에 없거나 빼는 말이 뒤따르지 않음",
            }
        )
    age = agegroup.resolve(question)
    slots["age"] = (
        {"expr": age.expr, "lo": age.lo, "hi": age.hi, "basis": age.basis} if age else None
    )
    ref = c["year_end"] or c["year"] or today.year
    if not age and (born := birth_spans(question)) and (bands := birth_ages(born, ref)):
        slots["age"] = {
            "expr": birth_expr(question),
            "lo": bands[0][0],
            "hi": bands[-1][1],
            "bands": [list(b) for b in bands],
            "basis": f"{ref}년 기준 만 나이",
        }

    if HOUSEHOLDER_SEX.search(question) and "가구주 성별" not in c["classes"]:
        c["classes"].append("가구주 성별")
    plain = HOUSEHOLDER_SEX.sub("", question)
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

    region = c["region"]
    if region and region.replace(" ", "") in SIDO_ALIAS:
        fix("region", SIDO_ALIAS[region.replace(" ", "")], "시도 정식 이름")
    if our_district_misread(question, c["region"]):
        fix("region", None, "'우리 구'의 조사를 지역 이름으로 읽음")
    excluded_regions = {SIDO_ALIAS.get(w.rstrip("은는을를이가"), w) for w in slots["exclude"]}
    if c["region"] and c["region"] in excluded_regions:
        fix("region", None, "빼 달라고 한 지역")

    level = next((lv for lv, pattern in LEVEL_WORDS if pattern.search(question)), None)
    if c["region"] and (named := _level_of(c["region"])):
        level = _deeper(level, named)
    if level is None and c["region_level"] is not None:
        fix("region_level", None, "지역 수준 언급 없음")
    elif level is not None:
        fix("region_level", level, "질문의 지역 수준")

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
    age = agegroup.single_age(question)
    if age and not any(agegroup.span(v) or AGE_VALUE.search(v) for v in c["items"]):
        fix("items", [*c["items"], age], f"질문의 나이 {age}")

    slots["fixes"] = fixes
    return slots


def short_years(question: str) -> set[int]:
    question = timeref.RECENT_N_YEARS.sub(" ", question)
    return {
        (2000 if int(yy) < SHORT_YEAR_PIVOT else 1900) + int(yy)
        for yy in SHORT_YEAR.findall(question)
    }


def _full_year(yy: str) -> int:
    return (2000 if int(yy) < SHORT_YEAR_PIVOT else 1900) + int(yy)


def decades(question: str, today: date | None = None) -> list[tuple[int, int]]:
    today = today or date.today()
    found = [(int(y), part) for y, part in DECADE.findall(question)]
    rest = DECADE.sub(" ", question)
    found += [(_full_year(yy), part) for yy, part in SHORT_DECADE.findall(rest)]
    spans = set()
    for decade, part in found:
        lo, hi = DECADE_PARTS.get(part, (0, 9))
        start, end = decade + lo, decade + hi
        if start <= today.year < end:
            end = today.year
        spans.add((start, end))
    return sorted(spans)


def birth_spans(question: str) -> list[tuple[int, int]]:
    found = [(int(y), bool(dae)) for y, dae in BIRTH_YEAR.findall(question)]
    rest = BIRTH_YEAR.sub(" ", question)
    found += [(_full_year(yy), bool(dae)) for yy, dae in SHORT_BIRTH_YEAR.findall(rest)]
    return sorted({(y, y + 9 if dae else y) for y, dae in found})


def birth_years(question: str) -> set[int]:
    return {y for lo, hi in birth_spans(question) for y in range(lo, hi + 1)}


def birth_age(spans: list[tuple[int, int]], ref_year: int) -> tuple[int, int] | None:
    bands = birth_ages(spans, ref_year)
    return bands[0] if len(bands) == 1 else None


def birth_ages(spans: list[tuple[int, int]], ref_year: int) -> list[tuple[int, int]]:
    bands = sorted((max(0, ref_year - hi), ref_year - lo) for lo, hi in spans if lo <= ref_year)
    merged = []
    for lo, hi in bands:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def birth_expr(question: str) -> str:
    found = BIRTH_YEAR.search(question) or SHORT_BIRTH_YEAR.search(question)
    return found.group(0).strip() if found else ""


def age_label(lo: int, hi: int) -> str:
    return f"{lo}세" if lo == hi else f"{lo}~{hi}세"


NEG_AFTER = re.compile(NEGATION_WORDS + r"|빼|제외")
NEG_WINDOW = 20


def llm_excluded_ok(question: str, word: str) -> bool:
    w = word.strip()
    if len(w) < 2 or w in NEGATION_SKIP:
        return False
    for m in re.finditer(re.escape(w), question):
        if NEG_AFTER.search(question[m.end() : m.end() + NEG_WINDOW]):
            return True
    return False


def _covered(word: str, words: list[str]) -> bool:
    return any(r.startswith(word) or word.startswith(r) for r in words)


def _strip_link(word: str) -> str:
    m = NEG_LINK_TAIL.search(word)
    return word[: m.start()] if m and m.start() >= 2 else word


def negated_words(question: str) -> list[str]:
    words = []
    for m in NEGATION.finditer(question):
        phrase = m.group(1).split()
        before = re.findall(r"[가-힣A-Za-z0-9]+|[,·/]", question[: m.start(1)])
        regionish = all(_strip_link(w).rstrip("은는을를이가") in SIDO_ALIAS for w in phrase)
        linked = False
        if phrase and phrase[0] in NEG_LINK_WORDS:
            phrase.pop(0)
            linked = True
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
            and (not re.search(r"\d", w) or re.fullmatch(r"\d+인[가-힣]*", w))
            and w.rstrip("은는을를이가") not in after
        ]  # fmt: skip
    return list(dict.fromkeys(words))


def spouse_of(question: str) -> str | None:
    if not MARRIAGE.search(question):
        return None
    wife, husband = bool(FOREIGN_WIFE.search(question)), bool(FOREIGN_HUSBAND.search(question))
    if wife == husband:
        return None
    return "외국인 아내" if wife else "외국인 남편"


AGE_VALUE = re.compile(r"\d+\s*(?:세|살|대)")
OUR_DISTRICT = re.compile(r"우리\s*(구|군)(로|에서|에|는|은|의|가|를|도)?(?![가-힣])")
DISTRICT_TAGS = ("NNG", "NR", "NNB")
_SIDO_HEAD = re.compile(
    "^(?:" + "|".join(sorted({*SIDO_ALIAS, *SIDO_ALIAS.values()}, key=len, reverse=True)) + ")"
)


def our_district_misread(question: str, region: str | None) -> bool:
    if not region:
        return False
    if synonyms.morph_backend() == "kiwi":
        return _misread_by_morph(question, region)
    return _misread_by_regex(question, region)


def _district_stem(region: str) -> tuple[str, str]:
    plain = region.replace(" ", "")
    base = _SIDO_HEAD.sub("", plain) or plain
    return base, (base[:-1] if len(base) > 1 and base.endswith(("구", "군")) else base)


def _misread_by_regex(question: str, region: str) -> bool:
    if not (m := OUR_DISTRICT.search(question)):
        return False
    base, stem = _district_stem(region)
    if stem not in (m[1] + (m[2] or ""), m[1]):
        return False
    full = base if base != stem else f"{stem}{m[1]}"
    return full not in question


def _misread_by_morph(question: str, region: str) -> bool:
    toks = synonyms.tokenize(question)
    our = None
    for a, b in zip(toks, toks[1:], strict=False):
        if (a.form, a.tag) == ("우리", "NP") and b.form in ("구", "군") and b.tag in DISTRICT_TAGS:
            our = b
            break
    if our is None:
        return False
    base, stem = _district_stem(region)
    josa = next((t.form for t in toks if t.start == our.end and t.tag.startswith("J")), "")
    if stem not in (our.form, our.form + josa):
        return False
    proper = {t.form for t in toks if t.tag == "NNP"}
    return not ({base, stem, f"{stem}{our.form}"} & proper)


def _level_of(region: str) -> str | None:
    last = region.split()[-1]
    if region in SIDO and len(region.split()) == 1:
        return "시도"
    if last.endswith(("읍", "면", "동")):
        return "읍면동"
    if last.endswith(("시", "군", "구")):
        return "시군구"
    return None


def _deeper(a: str | None, b: str) -> str:
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
