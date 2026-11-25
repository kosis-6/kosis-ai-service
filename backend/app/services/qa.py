import copy
import json
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.stats_db import StatsTableMissing
from app.prompts import understand_merged as merged_prompt
from app.prompts.qa import PARSE_SYSTEM
from app.services import (
    agegroup,
    analysis,
    ask_followup,
    cross_indicator,
    flow,
    indicators,
    llm_calls,
    llm_json,
    load_queue,
    lookup,
    monthref,
    search_llm,
    slots,
    table_cache,
    timeref,
    understand_merged,
)
from app.services.hcx_client import HcxClient, HcxError
from app.services.josa import josa, ro
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
UNDERSTAND_MAX_TOKENS = 1024


@llm_calls.staged("understand")
async def _chat(prompt, system, temperature=0.1, trace=None):
    result = await HcxClient().chat(
        MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        thinking={"effort": "none"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=UNDERSTAND_MAX_TOKENS,
        temperature=temperature,
        repetitionPenalty=1.1,
    )
    if trace is not None:
        trace["understand_usage"] = dict(result.usage)
        trace["understand_max_completion_tokens"] = UNDERSTAND_MAX_TOKENS
    return result.content


UNDERSTAND_CACHE = MemoryCache("understand", maxsize=1024, ttl=3600)


CACHE_TAIL = re.compile(r"[\s?？!！.。~]+$")


def _cache_key(question, latest):
    text = CACHE_TAIL.sub("", re.sub(r"\s+", " ", question).strip())
    return (text, date.today().isoformat(), latest)


async def understand(question, trace=None):
    lo, hi = await run_in_threadpool(indicators._data_years)
    use_cache = get_settings().question_cache
    key = _cache_key(question, hi)
    if use_cache and (hit := UNDERSTAND_CACHE.get(key)) is not MISSING:
        if trace is not None:
            trace["hcx_raw"], trace["understand_cached"] = hit["raw"], True
        flow.add("질문 이해", "캐시", _understood(hit["parsed"]))
        return copy.deepcopy(hit["parsed"])
    merged = None
    if get_settings().understand_merged == "gemma":
        merged = await _understand_merged(question, hi, trace)
    if merged is not None:
        text, parsed = merged
        llm = "Gemma(합침)"
    else:
        system = PARSE_SYSTEM.replace("{INDICATORS}", _indicator_list())
        system = system.replace("{LATEST}", str(hi))
        text = await _chat(question, system, trace=trace)
        if trace is not None:
            trace["hcx_raw"] = text
        objects = [o for o in _json_objects(YEAR_ARITH.sub(": null", text)) if "type" in o]
        if not objects:
            flow.add("질문 이해", "parse_error", "HCX 출력에서 JSON을 읽지 못함")
            return {"type": "parse_error", "raw": text}
        parsed = objects[0]
        if len({json.dumps(o.get("sex"), ensure_ascii=False) for o in objects}) > 1:
            parsed["sex"] = "계"
        llm = "HCX"
    result = await run_in_threadpool(correct, question, parsed)
    flow.add("질문 이해", llm, _understood(result))
    if use_cache:
        UNDERSTAND_CACHE.set(key, {"raw": text, "parsed": copy.deepcopy(result)})
    return result


MERGED_MAX_TOKENS = 512


async def _understand_merged(question, latest, trace=None):
    if not search_llm.is_gemma() or search_llm.falling_back():
        return None
    system = merged_prompt.system(_indicator_list(), date.today(), latest)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    try:
        with llm_calls.stage("understand"):
            result = await search_llm.GemmaClient(retry_waits=()).chat(
                MODEL,
                messages,
                thinking={"effort": "none"},
                topP=0.8,
                topK=0,
                maxCompletionTokens=MERGED_MAX_TOKENS,
                temperature=0.1,
                repetitionPenalty=1.1,
                responseFormat={"type": "json_object"},
            )
    except HcxError as e:
        logger.warning("합친 질문 이해(Gemma) 실패 → HCX 질문 이해: %s", e)
        return None
    text = result.content
    objects = _json_objects(text)
    obj = objects[0] if objects else llm_json.loads(text)[0]
    numeric, search = understand_merged.split(obj) if isinstance(obj, dict) else (None, None)
    if numeric is None:
        logger.warning("합친 질문 이해(Gemma) 출력을 읽지 못함 → HCX 질문 이해")
        return None
    if trace is not None:
        trace["hcx_raw"], trace["understand_llm"] = text, "gemma_merged"
        trace["understand_usage"] = dict(result.usage)
    if search is not None:
        try:
            slots_out = await run_in_threadpool(
                lambda: slots.correct(question, slots.normalize(question, search))
            )
            understand_merged.keep(question, slots_out)
        except Exception:
            logger.exception("합친 질문 이해의 조건 추출 몫 정리 실패")
    return text, numeric


def _understood(parsed):
    fixes = parsed.get("corrections") or []
    tail = f" · 코드 보정: {'; '.join(map(str, fixes[:3]))}" if fixes else ""
    return flow.summary_of(parsed) + tail


def _json_objects(text):
    decoder, out, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _indicator_list():
    out = []
    for name, sp in indicators.specs().items():
        desc = sp.get("desc")
        out.append(f"{name}({sp['unit']}{': ' + desc if desc else ''})")
    return "; ".join(out)


GENDER_WORDS = {"여": re.compile(r"여성|여자"), "남": re.compile(r"남성|남자")}
HOUSEHOLD_MEMBER_GENDER = re.compile(r"(?:여성|여자|남성|남자)\s*(?:이|가)\s*(?:있|없)는\s*가구")
SEX_WORD = r"(?<![구특])성별"
SEX_SPLIT = re.compile(rf"남녀|{SEX_WORD}|(남성|남자).*(여성|여자)|(여성|여자).*(남성|남자)")
SEX_TOTAL = re.compile(
    rf"(?:남녀|{SEX_WORD}|(?:남성|남자).*(?:여성|여자)|(?:여성|여자).*(?:남성|남자))"
    r"\s*(?:인구\s*)?(?:합계|전체|합산|합쳐|합치|합한|모두|통틀어)"
    rf"|{SEX_WORD}\s*(?:구분\s*없이|상관\s*없이)"
)
SEX_IMPLIED = re.compile(
    r"남편|신랑|아버지|아빠|부친|아들|(?<!청)소년|할아버지|남아"
    r"|아내|부인|와이프|신부|어머니|엄마|모친|딸|소녀|할머니|여아|미혼모|산모|임산부|임신부"
)
NO_REGION_TYPES = {"rank", "other", "parse_error"}

AGE_RANGE = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)?\s*[~\-]\s*(\d+)\s*(?:세|살)")
AGE_UNDER = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)\s*(?:미만|도?\s*안\s*(?:된|되는))")
AGE_UNDER_EQ = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)\s*이하")
AGE_SPAN = re.compile(
    r"(?:만\s*)?(\d{1,3})\s*(?:세|살)?\s*"
    r"(?:(?:에서|부터)\s*(?:만\s*)?(\d{1,3})\s*(?:세|살)|(?:와|과|하고)\s*(?:만\s*)?(\d{1,3})\s*(?:세|살)\s*사이)"
)
AGE_DECADE_SPAN = re.compile(r"(\d+)\s*대\s*(?:에서|부터)\s*(\d+)\s*대(?!\s*(광역시|도시))")
AGE_DECADE_OVER = re.compile(r"(\d+)\s*대\s*이상")
AGE_DECADE_RANGE = re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*대(?!\s*(광역시|도시))")
AGE_DECADE_PART = re.compile(r"(\d+)\s*대\s*(초반|중반|후반)")
DECADE_PART = {"초반": (0, 3), "중반": (4, 6), "후반": (7, 9)}
AGE_DECADE = re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))")
REGION_GROUPS = {
    "비수도권": [
        "부산",
        "대구",
        "광주광역시",
        "대전",
        "울산",
        "세종",
        "강원",
        "충북",
        "충남",
        "전북",
        "전남",
        "경북",
        "경남",
        "제주",
    ],
    "수도권 외": [
        "부산",
        "대구",
        "광주광역시",
        "대전",
        "울산",
        "세종",
        "강원",
        "충북",
        "충남",
        "전북",
        "전남",
        "경북",
        "경남",
        "제주",
    ],
    "수도권 밖": [
        "부산",
        "대구",
        "광주광역시",
        "대전",
        "울산",
        "세종",
        "강원",
        "충북",
        "충남",
        "전북",
        "전남",
        "경북",
        "경남",
        "제주",
    ],
    "지방 인구": [
        "부산",
        "대구",
        "광주광역시",
        "대전",
        "울산",
        "세종",
        "강원",
        "충북",
        "충남",
        "전북",
        "전남",
        "경북",
        "경남",
        "제주",
    ],
    "수도권": ["서울", "인천", "경기"],
    "강남3구": ["서울 강남구", "서울 서초구", "서울 송파구"],
    "강남 3구": ["서울 강남구", "서울 서초구", "서울 송파구"],
    "강남4구": ["서울 강남구", "서울 서초구", "서울 송파구", "서울 강동구"],
    "강남 4구": ["서울 강남구", "서울 서초구", "서울 송파구", "서울 강동구"],
    "충청권": ["대전", "세종", "충북", "충남"],
    "호남권": ["광주광역시", "전북", "전남"],
    "영남권": ["부산", "대구", "울산", "경북", "경남"],
    "동남권": ["부산", "울산", "경남"],
    "부울경": ["부산", "울산", "경남"],
    "대경권": ["대구", "경북"],
    "영남": ["부산", "대구", "울산", "경북", "경남"],
    "호남": ["광주광역시", "전북", "전남"],
    "충청 지역": ["대전", "세종", "충북", "충남"],
    "충청지역": ["대전", "세종", "충북", "충남"],
    "마용성": ["서울 마포구", "서울 용산구", "서울 성동구"],
    "노도강": ["서울 노원구", "서울 도봉구", "서울 강북구"],
    "금관구": ["서울 금천구", "서울 관악구", "서울 구로구"],
    "지방 광역시": ["부산", "대구", "광주광역시", "대전", "울산"],
    "지방광역시": ["부산", "대구", "광주광역시", "대전", "울산"],
    "특례시": ["경기 수원시", "경기 고양시", "경기 용인시", "경남 창원시(통합)", "경기 화성시"],
    "6대 광역시": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "5대 광역시": ["부산", "대구", "인천", "광주광역시", "대전"],
    "광역시 전체": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "모든 광역시": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "광역시 모두": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "9개 도": ["경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"],
    "9개도": ["경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"],
}
_HAP = r"합(?!계\s*출[산생])"
SUM_WORDS = re.compile(
    r"합계(?!\s*출[산생])|합친|합쳐|합하면|합치면|합산|총\s*몇|더하면|총합|총\s*인원"
    rf"|(?:모두|전부)\s*(?:몇|더|해서|{_HAP})|(?<![가-힣])다\s*(?:해서|더해|{_HAP})|통틀|누적"
)
COUNT_ASK = re.compile(r"몇\s*(명|건|가구|곳|개)|인구는|인구가|인구\s*수|수는|수가|건수")
PCT_ASK = re.compile(r"%|퍼센트|비율|비중|몇\s*프로")
COMPOSITION_WORDS = re.compile(r"구성|비중|비율|%|퍼센트|나누면|몇\s*프로")
BY_REGION = re.compile(r"(구|시군구|시도|지역|시)\s*(별|마다)|\d+\s*개\s*(구|시군구)\s*중")
BOTH_ENDS = re.compile(
    r"(높|많)\S*\s*\S*\s*(랑|와|과|하고)\s*.*(낮|적)|(낮|적)\S*\s*\S*\s*(랑|와|과|하고)\s*.*(높|많)"
)
NATIONAL_NAMES = {"우리나라", "한국", "국내", "대한민국"}
SUB_AREAS = {
    "송도": "인천 연수구",
    "청라": "인천 서구",
    "영종": "인천 중구",
    "판교": "경기 성남시 분당구",
    "분당": "경기 성남시 분당구",
    "일산": "경기 고양시",
    "동탄": "경기 화성시",
    "광교": "경기 수원시 영통구",
    "위례": "경기 성남시 수정구, 하남시, 서울 송파구",
    "마곡": "서울 강서구",
    "목동": "서울 양천구",
    "잠실": "서울 송파구",
    "1기 신도시": "경기 성남시 분당구, 고양시 일산동구·일산서구, 안양시 동안구, 군포시, 부천시",
    "운정": "경기 파주시",
    "다산": "경기 남양주시",
    "별내": "경기 남양주시",
    "미사": "경기 하남시",
    "평촌": "경기 안양시 동안구",
    "산본": "경기 군포시",
    "중동": "경기 부천시",
    "여의도": "서울 영등포구",
    "센텀": "부산 해운대구",
    "서면": "부산 부산진구",
    "둔산": "대전 서구",
}
SUB_AREA_NOT = {
    "일산": ("화",),
    "미사": ("일",),
    "다산": ("가정", "가구", "모", "장려", "정책", "여성"),
    "서면": ("으로", "신청", "동의", "제출", "조사"),
    "중동": ("출신", "국가", "국적", "사람", "전쟁", "지역"),
}


def find_sub_area(question):
    for area, sigungu in SUB_AREAS.items():
        tail = "|".join(["[구시군]", *map(re.escape, SUB_AREA_NOT.get(area, ()))])
        if re.search(r"(?<!\d)" + re.escape(area) + r"(?!\s*(?:" + tail + r"))", question):
            return area, sigungu
    return None


YEAR_EXTREME = {
    "min": re.compile(r"(가장|제일)\s*(낮|적)\S*(\s+\S+){0,2}\s*해(\s|$|[?가는를]|\d)"),
    "max": re.compile(r"(가장|제일)\s*(높|많)\S*(\s+\S+){0,2}\s*해(\s|$|[?가는를]|\d)"),
}
STREAK = re.compile(r"연속")
REBOUND = re.compile(r"다시\s*(오르|늘|증가|올라)|반등")
PER_YEAR = re.compile(r"해마다|매년|연평균|1년에|한 해에")
PREV_YEAR = re.compile(r"(작년|전년|지난해)\s*(보다|대비|에 비해)")
RATIO_ASK = re.compile(r"몇\s*배")
SURVEY_ASK = [
    (
        "장래인구추계",
        re.compile(r"장래\s*인구\s*추계|장래\s*추계|추계\s*인구|추계\s*(?:값|치|기준|자료|로)"),
    ),
    (
        "인구총조사",
        re.compile(
            r"인구\s*(?:주택\s*)?총조사|총조사|센서스|외국인\s*(?:까지\s*)?(?:포함|합쳐|넣어|더해)"
        ),
    ),
]
BIRTH_HOW_MANY = re.compile(
    r"(?:애|아이|아기|애기)(?:들)?(?:을|를)?\s*(?:얼마나|몇\s*명(?:씩)?|많이|적게)\s*(?:을|를)?"
    r"\s*(?:안\s*)?낳"
    r"|출산(?:을)?\s*(?:얼마나|많이)"
)
BIRTH_TOPIC = ("저출산", "저출생", "출산", "출생", "출산율", "출생률")
TFR_MEANING = indicators.spec("합계출산율")["definition"]
BIRTH_PAIR_NOTE = (
    f"{TFR_MEANING}. '얼마나 낳는지'는 합계출산율로 답하고, "
    "그해 태어난 아이 수(출생아 수)를 함께 보여 드립니다"
)
BIRTH_PER_WOMAN = re.compile(r"한\s*명(?:이|당)|평생")
BIRTH_CLEAR = re.compile(r"한\s*명(?:이|당)|평생|태어|낳았|낳은")
LEVEL_CITY = re.compile(r"전국\s*(의\s*)?시\s*(중|가운데)|시\s*단위")
ASCENDING = re.compile(r"꼴찌|최하위|가장\s*(낮|적)|제일\s*(낮|적)")
DESCENDING = re.compile(r"(높|많)(은|게|을)|상위|1등|1위|top", re.I)
DECREASE = re.compile(r"낮|적은|적게|감소|줄|유출|빠져")
TOP_N = re.compile(r"(상위|하위)\s*(\d+)|(\d+)\s*(곳|개|위까지)(?!\s*\S*\s*중)")
RECENT = timeref.RECENT_N_YEARS
UNIT_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
FEW_YEARS = r"요\s*몇\s*년|몇\s*년\s*(새|사이|동안)|근\s*몇\s*년"
LATEST = re.compile(rf"최근|최신|현재|요즘|요새|지금|{FEW_YEARS}")
THESE_DAYS = re.compile(rf"요즘|요새|{FEW_YEARS}")
LATEST_KINDS = ("value", "compare", "rank")
CHANGE_ASK = re.compile(r"추이|변화|변동|흐름|증감|추세|늘|줄|증가|감소|동안|사이|년간|그래프")
SPAN = re.compile(
    r"((?:19|20)\d{2})\s*년?\s*(대비|부터|에서|이랑|랑|하고|과|와|및)\s*((?:19|20)\d{2})\s*년"
)
YEAR_NUMBER = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
YEAR = re.compile(r"((?:19|20)\d{2})\s*년(?!대(?!비)|생)")
YEAR_WORDS = re.compile(r"(?<![청소노중장성])년|때|당시|해(?![가-힣])")
HAE_ATTACHED = ("그", "첫", "이듬", "다음")
HAE_SPACED = (*HAE_ATTACHED, "이", "한", "같은", "어느", "몇")


def _year_word(question):
    for m in YEAR_WORDS.finditer(question):
        word, head = m.group(0), question[: m.start()]
        last = head[-1:] if head else ""
        if word == "해" and "가" <= last <= "힣" and not head.endswith(HAE_ATTACHED):
            continue
        if word == "해" and last == " " and head.rstrip().split(" ")[-1] not in HAE_SPACED:
            continue
        tail = head.rstrip()[-1:]
        if word == "때" and "가" <= tail <= "힣" and (ord(tail) - 0xAC00) % 28 == 8:
            continue
        return True
    return False


COMMON_WORD_REGIONS = frozenset({"고령", "장수", "영양", "이천", "예산", "수영"})


def _says_year(question):
    return bool(
        YEAR.search(question)
        or ask_followup.has_explicit_year(question)
        or _year_word(question)
        or timeref.resolve(question)
    )


SINCE = re.compile(r"((?:19|20)\d{2})\s*년\s*(이후|이래|부터)(?!.*까지)")
UNTIL = re.compile(r"((?:19|20)\d{2})\s*년\s*까지")
NATIONWIDE = re.compile(
    r"전국|우리\s*나라|대한민국|한국\s*(?:전체|의|은|는|인구|사람)"
    r"|국내(?!\s*(?:이동|인구\s*이동|출생(?!\s*아)"
    r"|에서\s*(?:태어난|출생한)\s*(?:다문화\s*가구\s*)?자녀))"
)
WHY_WORDS = re.compile(r"왜|이유|원인|때문|탓|효과|영향")
CAUSAL = re.compile(r"효과가?\s*(있|없|컸)|효과적|원인|왜\s|영향을?\s*(줬|주|미쳤|끼쳤)")
UNSUPPORTED_MEASURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|킬로그램|킬로|키로|그램)|\d+(?:\.\d+)?\s*(?:cm|센티)|"
    r"\d+\s*년\s*반|\d+\s*개월|(?:결혼|혼인|동거|거주|근속|재직)\s*기간|체중|몸무게|키가|"
    r"임신\s*(?:기간|주수|\d+\s*주)|\d+\s*주\s*(?:차|에서|부터|\s*~|\s*사이|\s*만에|\s*미만|\s*이상)"
)
PREDICT = re.compile(
    r"앞으로|향후|미래에?|전망|예측|예상(?!보다)|"
    r"(오를|내릴|늘|줄|늘어날|줄어들|증가할|감소할|떨어질|올라갈|내려갈|회복할|반등할)\s*(까|지)"
)
GUESS_TAIL = re.compile(r"(?:것|거)\s*같|(?:될|할)\s*(?:거|것)\s*래")
FUTURE_WORDS = re.compile(r"내년|내후년|앞으로|향후|장차|\d+\s*년\s*(?:뒤|후)")
DIRECTION_WORDS = re.compile(r"더\s|다시|계속|여전히")


def asks_guess(question: str) -> bool:
    if GUESS_TAIL.search(question):
        return True
    for i, ch in enumerate(question):
        if ch != "까" or i == 0:
            continue
        prev = question[i - 1]
        if "가" <= prev <= "힣" and (ord(prev) - 0xAC00) % 28 == 8 and prev not in "일":
            return True
    return False


RANK_ASK = re.compile(
    r"(가장|제일|꼴찌|최하위)\S*\s*(\S+\s*){0,3}?(시군구|시도|지역|곳|구|시)(는|가|은|이|를|\s|$|\?)"
    r"|(?:비중|비율)이?\s*(?:높|낮)은\s*(?:시군구|시도|지역|곳)(?:은|는|이|가|\s|$|\?)"
)
BIRTHS_PER_WOMAN = re.compile(
    r"(?:여자|여성|엄마|가임\s*여성)?\s*(?:한|1)\s*명\s*(?:당|이|이서|마다)|평균|평생|일생|몇\s*명\s*씩"
)
PER_WOMAN_BIRTH = re.compile(r"(낳|출산)")
BABIES_BORN = re.compile(r"(?:아이|아기|애|신생아)\S*\s*몇\s*명\S*\s*(?:\S+\s*){0,2}낳")
BARE_FOREIGNER = re.compile(
    r"(?<![가-힣])외국인(?!\s*(?:주민|아내|남편|며느리|사위|배우자|근로자|노동자|유학생|관광객|과의|와의|과|와\s|등록|비율|비중))"
)
NOT_BARE_FOREIGNER = re.compile(
    r"불법\s*체류|체류|등록|결혼|혼인|이민|귀화|유학|근로|국적|아내|남편|이혼|출생|다문화|비율|비중"
    r"|퍼센트|%|프로|입국|출국|들어왔|들어온|나갔|인구\s*조사|총조사|중에?\s*외국인"
)
INDICATOR_PATTERNS = [
    (re.compile(r"(아이|아기|신생아)\S*\s*(\S+\s*){0,3}태어"), "출생아 수"),
    (re.compile(r"결혼(을|한)?\s*(\S+\s*){0,2}(많이|적게)"), "혼인 건수"),
    (
        re.compile(
            r"(태어|출생)\S*\s*(\S+\s*){0,4}?(돌아가|사망|죽)"
            r"|(돌아가|사망|죽)\S*\s*(\S+\s*){0,4}?(태어|출생)"
        ),
        "자연증가 건수",
    ),
    (
        re.compile(
            r"(일할|일하는|생산\s*가능|생산\s*연령)\s*(나이\s*)?(인구|사람)\S*\s*"
            r"(?:(?:100|백)\s*명\s*)?당\s*(노인|어르신|고령)"
        ),
        "노년부양비",
    ),
    (
        re.compile(
            r"(노인|어르신|고령)\S*\s*(\S+\s*){0,2}부양"
            r"|(젊은|일할|일하는|생산\s*가능|생산\s*연령)\s*(나이\s*)?(인구|사람)?\S*\s*"
            r"(\S+\s*){0,3}?(노인|어르신|고령)\S*\s*(부양|부담)"
        ),
        "노년부양비",
    ),
    (re.compile(r"(아이|어린이|유소년|아동)\S*\s*(\S+\s*){0,2}부양"), "유소년부양비"),
    (
        re.compile(
            r"(노인|어르신|고령\S*)\s*(수|인구)?\s*(가|이)\s*(아이|어린이|유소년|애)\S*\s*"
            r"(수|인구)?\s*의\s*몇\s*배"
            r"|(아이|어린이|유소년|애)\S*\s*보다\s*(노인|어르신|고령\S*)\S*\s*(\S+\s*){0,2}몇\s*배"
        ),
        "노령화지수",
    ),
    (
        re.compile(r"(나이|연령)\s*순\S*\s*(줄|세우|정렬).*(가운데|중간)|(가운데|중간)\s*나이"),
        "중위연령",
    ),
    (re.compile(r"인구\s*(에\s*비해|대비|당)\S*\s*.*(태어|출생)"), "조출생률"),
    (re.compile(r"(이사\s*온|들어온|전입)\S*.*(떠난|나간|전출)\S*.*(빼|뺀|차감)"), "순이동"),
    (
        re.compile(r"(어르신|노인|고령자|노령)\S*\s*(인구\s*)?(몇\s*)?(비율|비중|%|퍼센트|프로)"),
        "고령인구 비율",
    ),
]
MORE_SPECIFIC = {
    "조출생률": {"출생아 수"},
    "셋째 이상 출생아 수": {"출생아 수"},
    "다문화 출생아 수": {"출생아 수"},
    "첫째아 평균 출산연령": {"모의 평균 출산연령", "출산순위별 출생 구성"},
}
CHANGE_RANK = re.compile(
    r"(가장|제일)\s*많이\s*(줄어|줄었|감소|빠졌|늘어|늘었|증가)|(감소|증가)\s*폭이?\s*(가장|제일)?\s*(큰|많)"
)
DECREASE_WORDS = re.compile(r"줄어|줄었|감소|빠졌")
OTHER_AXIS = re.compile(
    r"(국적|나라|국가|교통\s*수단|이동\s*사유|전입\s*사유)\s*(별|중|가|이|마다)?"
)
AXIS_REGION = re.compile(r"시도|시군구|지역|도시|광역시|어느\s*(시|구|군|도)")
YEAR_ASKED = re.compile(r"(해|연도|년도|때)\s*(는|가|를|은|가\s*언제|$|\?)|언제")
PEOPLE_COUNT = re.compile(
    r"몇\s*(명|분)|얼마나\s*(살|계|있|돼|되|줄|늘)|(수|인구)\s*(는|가|를|합|얼마|좀)|사람|분\s*(이|들)"
)
OTHER_PEOPLE = re.compile(
    r"외국|등록|이사|전입|전출|이동|통근|통학|결혼|혼인|이혼|사망|돌아가|태어|출생|낳|가구|유학|귀화|"
    r"근로|이민|입국|출국|취업|학생(?!\s*나이)|"
    r"혼자|홀로|독거|장애|아파트|주택|단독|연립|다세대|오피스텔|거처|셋방|기숙사"
)
FOCUS_WORDS = {
    "일자리": "직업",
    "직장": "직업",
    "직업": "직업",
    "가족": "가족",
    "주택": "주택",
    "교육": "교육",
    "첫째": "첫째",
    "둘째": "둘째",
    "셋째": "셋째 이상",
    "결혼이민": "결혼이민자",
    "유학생": "유학생",
    "근로자": "외국인근로자",
    "귀화한 자녀": "귀화·인지 및 외국국적 자녀",
    "한국 출생": "국내출생 자녀",
}
STATS_WORDS = re.compile(
    r"인구|출생|출산|사망|혼인|결혼|이혼|이사|전입|전출|이동|외국인|다문화|가구|주민|아이|자녀|국적|귀화"
    r"|태어|돌아가"
    r"|몇\s*(명|건|가구|분)"
)


def extract_ages(question):
    bands = []
    for _, _, band in age_spans(agegroup.native_to_digits(question)):
        if band not in bands:
            bands.append(band)
    return bands


AGE_LIST_JOIN = re.compile(r"(?:[·,]|이랑|하고|또는|및|랑|[과와])\s*$")


def resolved_ages(question: str) -> list[list[int | None]]:
    text = agegroup.native_to_digits(question)
    spans = [(a, b, tuple(band), True) for a, b, band in age_spans(text)]
    spans += [(a, b, (ref.lo, ref.hi), False) for a, b, ref in agegroup.spans(text)]
    groups = [[]]
    end = 0
    for start, stop, band, numeric in sorted(spans, key=lambda span: span[0]):
        if groups[-1] and AGE_LIST_JOIN.search(text[end:start]):
            groups.append([])
        groups[-1].append((band, numeric))
        end = max(end, stop)
    bands = {}
    for group in groups:
        use_numeric = any(numeric for _, numeric in group)
        bands.update(dict.fromkeys(band for band, numeric in group if numeric == use_numeric))
    return [list(band) for band in bands]


def age_spans(question):
    found, covered = [], []

    def add(m, band):
        if not any(a < m.end() and m.start() < b for a, b in covered):
            covered.append(m.span())
            found.append((m.start(), m.end(), band))

    for m in agegroup.AGE_OVER_UNDER.finditer(question):
        add(m, [agegroup.lower_age(m[1], m[2]), int(m[3]) - (m[4] == "미만")])
    for m in AGE_RANGE.finditer(question):
        add(m, [int(m[1]), int(m[2])])
    for m in AGE_SPAN.finditer(question):
        lo, hi = int(m[1]), int(m[2] or m[3])
        if lo < hi:
            add(m, [lo, hi])
    for m in AGE_DECADE_SPAN.finditer(question):
        if int(m[1]) % 10 == 0 and int(m[2]) % 10 == 0 and int(m[1]) < int(m[2]):
            add(m, [int(m[1]), int(m[2]) + 9])
    for m in agegroup.AGE_OVER.finditer(question):
        add(m, [agegroup.lower_age(m[1], m[2]), None])
    for m in AGE_UNDER.finditer(question):
        add(m, [0, int(m[1]) - 1])
    for m in agegroup.AGE_NOT_YET.finditer(question):  # "스무 살 안 됐는데": 20세 미만
        add(m, [0, int(m[1]) - 1])
    for m in AGE_UNDER_EQ.finditer(question):
        add(m, [0, int(m[1])])
    for m in AGE_DECADE_RANGE.finditer(question):
        if int(m[1]) % 10 == 0 and int(m[2]) % 10 == 0:
            add(m, [int(m[1]), int(m[2]) + 9])
    for m in AGE_DECADE_OVER.finditer(question):
        if int(m[1]) % 10 == 0:
            add(m, [int(m[1]), None])
    for m in AGE_DECADE_PART.finditer(question):
        if int(m[1]) % 10 == 0:
            lo, hi = DECADE_PART[m[2]]
            add(m, [int(m[1]) + lo, int(m[1]) + hi])
    for m in AGE_DECADE.finditer(question):
        if int(m[1]) % 10 == 0:
            add(m, [int(m[1]), int(m[1]) + 9])
    return sorted(found, key=lambda x: x[0])


def _pick_indicator(question, names):
    if len(names) == 1:
        return next(iter(names))
    rest = names - {"총인구"}
    if len(rest) == 1:
        return next(iter(rest))
    if PCT_ASK.search(question):
        ratio = {
            n for n in rest if lookup.kind_of(indicators.specs()[n]) in ("ratio", "composition")
        }
        if len(ratio) == 1:
            return next(iter(ratio))
    return None


def _both_sides(current: str | None, name: str, question: str) -> bool:
    sp = indicators.spec(current) if current else None
    formula = (sp or {}).get("formula") or ""
    if "−" not in formula or "÷" in formula:
        return False
    stems = [x.strip() for x in formula.split("−")]
    stem = re.sub(r"자?\s*수$", "", name).strip()
    if not stem or stem not in stems:
        return False
    text = re.sub(r"\s+", "", question)
    aliases = indicators._alias_map()
    for other in (x for x in stems if x != stem):
        words = [
            re.sub(r"\s*(수|몇|이\s*몇|들\s*몇)$", "", a)
            for n, al in aliases.items()
            if n.startswith(other)
            for a in [n, *al]
        ]
        if not any(len(w) >= 2 and re.sub(r"\s+", "", w) in text for w in words):
            return False
    return True


def _ratio_sibling(question, parsed, fix):
    current = indicators.canonical_name(parsed.get("indicator") or "")
    sp = indicators.spec(current) if current else None
    if sp is None or lookup.kind_of(sp) != "count":
        return None
    if not PCT_ASK.search(question) or CHANGE_ASK.search(question) or "전국" in question:
        return None
    base = re.sub(r"\s*수$", "", current)
    for sibling in (f"{base} 비율", f"{base} 비중"):
        if sibling in indicators.specs():
            fix("indicator", sibling, "비율(몇 프로)을 물음")
            return sibling
    return None


def survey_said(question):
    return next((name for name, pat in SURVEY_ASK if pat.search(question or "")), None)


def without_survey(question):
    for _, pat in SURVEY_ASK:
        question = pat.sub(" ", question)
    return question


def survey_for(question, parsed):
    said = survey_said(question)
    sp = indicators.spec(parsed.get("indicator")) if said else None
    if not sp or len(parsed.get("indicators") or []) > 1:
        return None
    return said if said in indicators.alt_surveys(sp) else None


def survey_missing(question, parsed):
    said = survey_said(question)
    named = said and re.search(r"총조사|센서스|추계", question)
    sp = indicators.spec(parsed.get("indicator")) if named else None
    if not sp or len(parsed.get("indicators") or []) > 1:
        return None
    if said in indicators.alt_surveys(sp):
        return None
    own = indicators.source_surveys(sp)
    return said if own and said not in own else None


def set_survey(question, parsed):
    asked = {
        k: v
        for k, v in (parsed.get("asked") or {}).items()
        if k not in ("survey", "survey_missing")
    }
    if survey := survey_for(question, parsed):
        asked["survey"] = survey
    elif missing := survey_missing(question, parsed):
        asked["survey_missing"] = missing
    if asked:
        parsed["asked"] = asked
    else:
        parsed.pop("asked", None)
    return parsed


YEAR_ARITH = re.compile(r":\s*(?:19|20)\d{2}\s*[-+]\s*\d{1,3}(?=\s*[,}])")
PERIOD_KINDS = ("recent", "span", "compare_ago", "span_from", "until_now")


def _word_years(parsed):
    """_apply_time 이 푼 낱말 시점(작년 · 재작년 …)의 연도들 (중복 없이)"""
    return {
        r["year"]
        for r in parsed.get("time_refs") or []
        if r.get("kind") == "word" and r.get("year")
    }


def _apply_time(question, parsed, fix, today):
    """상대 기간은 코드가 질문에서 직접 알아보고(timeref.find: 표현 · 종류 · N) 연도도
    규칙표(timeref.period)가 센다. LLM은 오늘이 언제인지 모르고 산수도 틀린다:
    "2020년이랑 작년"의 작년을 2024로, "10년 동안"의 끝을 2024로, '최근 5년'을 6개 해로 적었다.
    여기서 고치는 것은 지금까지 코드가 풀지 못한 모양뿐이다.
      기간(최근 N년 · N년 동안 · N년 전보다 · ○년부터 N년 동안 · 지금까지): 시작 · 끝
      숫자 연도 + 낱말 시점인 추이("2018년이랑 작년"): 두 해를 시작 · 끝으로
      낱말 시점 둘("작년이랑 재작년"): 두 해를 시작 · 끝으로 (추이 2점)
      N년 뒤: 그 해
    작년 · N년 전 한 시점은 아래 timeref.resolve 가 지금처럼 푼다.
    상대 기간을 풀었는데 질문에 숫자 연도가 없으면, HCX가 직접 채운 시작 · 끝은 버린다"""
    refs = timeref.find(question)
    if not refs:
        _drop_unsaid_years(question, parsed, fix, set())
        return
    end = indicators._data_years()[1]
    said = sorted({int(y) for y in YEAR.findall(question)})
    kind_now, resolved, touched = parsed.get("type"), [], set()

    def put(key, value, why):
        touched.add(key)
        if parsed.get(key) != value:
            fix(key, value, why)

    for ref in refs:
        got = timeref.period(ref, today, end)
        resolved.append({"expr": ref["expr"], "kind": ref["kind"], "n": ref.get("n"), **got})
        why = f"'{ref['expr']}'"
        if ref["kind"] in PERIOD_KINDS:
            if "start" in got:
                put("start_year", got["start"], f"{why} = {got['start']}년부터")
            if "end" in got:
                shown = f"{got['end']}년까지" if got["end"] else "자료 마지막 해까지"
                put("end_year", got["end"], f"{why} = {shown}")
        elif "year" in got and len(said) == 1 and kind_now == "trend" and ref["kind"] != "later":
            lo, hi = sorted((said[0], got["year"]))
            put("start_year", lo, f"{why} = {got['year']}년 (올해 {today.year} 기준)")
            put("end_year", hi, f"{why} = {got['year']}년 (올해 {today.year} 기준)")
        elif ref["kind"] == "later" and not said and kind_now != "trend":
            put("year", got["year"], f"{why} = {got['year']}년 (올해 {today.year} 기준)")
    words = sorted({r["year"] for r in resolved if r.get("kind") == "word" and r.get("year")})
    if len(words) >= 2 and not said:
        # "작년이랑 재작년 비교": 낱말 시점이 둘이면 두 해를 시작 · 끝으로 (추이 2점). 전에는
        # 두 해를 다 풀어 놓고도 쓰지 않아 HCX의 year 하나만 남고 추이 기본 창으로 답했다 (11/24)
        put("start_year", words[0], f"낱말 시점 {len(words)}개 = {words[0]}년부터")
        put("end_year", words[-1], f"낱말 시점 {len(words)}개 = {words[-1]}년까지")
        if kind_now == "value":
            fix("type", "trend", "두 시점을 견줌")
    if not said and not slots.short_years(question):  # "22년"처럼 두 자리로 말한 연도는 둔다
        keys = ["start_year", "end_year"]
        if kind_now == "trend" and any(r["kind"] in PERIOD_KINDS for r in refs):
            keys.append("year")
        for key in keys:
            if key not in touched and parsed.get(key):
                fix(key, None, "질문에 숫자로 없는 연도 (상대 기간은 코드가 센다)")
    _drop_unsaid_years(question, parsed, fix, touched)
    parsed["time_refs"] = resolved


def _drop_unsaid_years(question, parsed, fix, touched):
    said = {int(y) for y in YEAR_NUMBER.findall(question)} | slots.short_years(question)
    if not said:
        return
    for key in ("year", "start_year", "end_year"):
        value = parsed.get(key)
        if key not in touched and isinstance(value, int) and value not in said:
            fix(key, None, "질문에 없는 연도")


def _blank_to_none(parsed, fix):
    for key in ("year", "start_year", "end_year"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip().isdigit():
            value = parsed[key] = int(value.strip())
        real = isinstance(value, int) and not isinstance(value, bool) and value > 0
        if value is not None and not real:
            fix(key, None, "빈 값 정리")
    if parsed.get("sex") == "":
        fix("sex", "계", "빈 값 정리")


def correct(question, parsed, today=None):
    today = today or date.today()
    kind = parsed.get("type")
    fixes = []

    def fix(key, value, reason):
        fixes.append(f"{key}: {parsed.get(key)!r} → {value!r} ({reason})")
        parsed[key] = value

    _blank_to_none(parsed, fix)
    name = parsed.get("indicator")
    if name and indicators.spec(name) is None:
        bare = UNIT_SUFFIX.sub("", name)
        if bare != name and indicators.spec(bare) is not None:
            fix("indicator", bare, "지표 이름 뒤의 단위 표기")

    undone_pct = False
    if kind == "causal" and not (CAUSAL.search(question) or WHY_WORDS.search(question)):
        kind = "value"
        fix("type", kind, "원인을 묻는 말이 없음")
        undone_pct = bool(PCT_ASK.search(question))
    if kind == "other" and CAUSAL.search(question):
        fix("type", "causal", "원인·효과를 묻는 질문")
        kind = "causal"
    elif kind == "other" and STATS_WORDS.search(question):
        kind = "compare" if len(parsed.get("regions") or []) > 1 else "value"
        if COMPOSITION_WORDS.search(question) and re.search(rf"남녀|{SEX_WORD}|연령", question):
            kind = "composition"
        fix("type", kind, "통계를 묻는 질문")
        if indicators.spec(parsed.get("indicator")) is None:
            parsed["unsupported"] = True

    ages = extract_ages(question)
    if ages:
        parsed["ages"] = ages
        if note := decade_part_note(question):
            parsed["age_note"] = note
    elif (group := agegroup.resolve(question)) and not (group.lo == 0 and _about_births(parsed)):
        parsed["age_group"] = {"expr": group.expr, "range": group.label(), "basis": group.basis}
        ages = [[group.lo, group.hi]]
        parsed["ages"] = ages
    elif born := slots.birth_spans(question):
        said = [int(y) for y in YEAR.findall(question)]
        parsed["born"] = {"expr": slots.birth_expr(question), "spans": [list(b) for b in born]}
        bands = slots.birth_ages(born, max(said) if said else today.year - 1)
        ages = [list(b) for b in bands] or None
        parsed["ages"] = ages
    if parsed.get("sex") not in (None, "", "계", "남", "여"):
        fix("sex", "계", "성별 값 정리")
    if parsed.get("level") in ("구", "시군구별", "구별", "군", "시군"):
        fix("level", "시군구", "지역 수준 정리")
    if sub := find_sub_area(question):
        parsed["sub_area"] = list(sub)
    merged = _merge_regions(parsed.get("regions") or [], question)
    if merged != (parsed.get("regions") or []):
        fix("regions", merged, "시도와 시군구를 한 지역으로")
    regions = [r for r in parsed.get("regions") or [] if r not in NATIONAL_NAMES]
    if not regions and kind not in ("other", "parse_error") and not NATIONWIDE.search(question):
        found = [r for r in indicators.find_regions(question) if r != "전국"]
        if found:
            fix("regions", found, "질문의 지역 이름")

    if kind not in ("other", "parse_error"):
        names = {name for *_, name in indicators.find_in_text(question)}
        if not names - {"총인구"}:
            names |= {
                name
                for pattern, name in INDICATOR_PATTERNS
                if pattern.search(question)
                and not (name == "총인구" and OTHER_PEOPLE.search(question))
            }
            if BABIES_BORN.search(question) or (
                PER_WOMAN_BIRTH.search(question) and BIRTHS_PER_WOMAN.search(question)
            ):
                per_woman = BIRTHS_PER_WOMAN.search(question)
                names |= {"합계출산율" if per_woman else "출생아 수"}
            if not names - {"총인구"} and (ages or parsed.get("age_group")):
                if (
                    PEOPLE_COUNT.search(question)
                    and not PCT_ASK.search(question)
                    and not OTHER_PEOPLE.search(question)
                ):
                    names |= {"총인구"}
        for specific, general in MORE_SPECIFIC.items():
            if specific in names:
                names -= general
        if (
            not names
            and BARE_FOREIGNER.search(question)
            and not NOT_BARE_FOREIGNER.search(question)
        ):
            names = {"외국인주민 수"}
        name = _pick_indicator(question, names)
        current = indicators.canonical_name(parsed.get("indicator") or "")
        if name and current != name:
            said = name == "총인구" and current is None and _says_other_indicator(question, parsed)
            if _both_sides(current, name, question):
                names = names | {current}
            elif not said and (
                name != "총인구" or current is None or (ages and not PCT_ASK.search(question))
            ):
                fix("indicator", name, "질문의 지표 표현")
                parsed.pop("unsupported", None)
        if sibling := _ratio_sibling(question, parsed, fix):
            names = names | {sibling}
        _guard_indicator(question, parsed, names, fix)
        _age_population(question, parsed, fix)
        current = indicators.canonical_name(parsed.get("indicator") or "")
        if current and (phrase := indicators.excluded_phrase(current, question)):
            fix(
                "indicator",
                parsed.get("term") or phrase,
                josa(f"'{current}'", "과와") + " 다른 통계",
            )
            parsed["unsupported"] = True
        kind = parsed.get("type")
        chosen = indicators.canonical_name(parsed.get("indicator") or "")
        parsed["grounded"] = (chosen in names) if chosen else None
        plain = _plain_people_question(question) or (
            _both_sexes(question) and not OTHER_PEOPLE.search(question)
        )
        if chosen == "총인구" and parsed["grounded"] is False and plain:
            parsed["grounded"] = True
        if chosen == "총인구" and population_modifier(question):
            parsed["grounded"] = False
        chosen_spec = indicators.spec(chosen) if chosen else None
        if undone_pct and chosen_spec and lookup.kind_of(chosen_spec) == "count":
            parsed["grounded"] = False

    sp = indicators.spec(parsed.get("indicator"))
    is_comp = sp is not None and lookup.kind_of(sp) == "composition"
    if is_comp and kind not in ("composition", "causal"):
        fix("type", "composition", "구성비 지표")
    elif kind in ("value", "rank") and BY_REGION.search(question) and not is_comp:
        if kind == "value" or (parsed.get("regions") and not parsed.get("target")):
            if kind == "value":
                fix("type", "rank", "지역별 목록")
            _region_list(question, parsed, fix)
    elif kind == "composition" and not is_comp:
        population = sp is None or sp["name"] == "총인구"
        if not (population and COMPOSITION_WORDS.search(question) and not ages):
            new = "rank" if BY_REGION.search(question) else "value"
            fix("type", new, "구성·비중을 묻지 않음")
            if new == "rank":
                _region_list(question, parsed, fix)
    kind = parsed.get("type")

    _apply_time(question, parsed, fix, today)
    if (mref := monthref.find(question)) and kind in (None, "value", "other"):
        # "1월부터 3월까지" · "상반기": 달 묶음은 코드가 읽는다 (lookup.month_sum). 값 질문만
        fix("months", [mref["m0"], mref["m1"]], f"'{mref['expr']}' = 달 묶음 합")
    _read_question(question, parsed, fix)

    gender_question = HOUSEHOLD_MEMBER_GENDER.sub("가구", question)
    genders = [sex for sex, pattern in GENDER_WORDS.items() if pattern.search(gender_question)]
    if len(genders) == 1 and (parsed.get("sex") in (None, "", "계") or gender_question != question):
        fix("sex", genders[0], "질문의 성별 표현")
    both = len(genders) == 2 or (not genders and SEX_SPLIT.search(gender_question))
    if parsed.get("sex") in ("남", "여"):
        if both:
            fix("sex", "계", "남녀를 함께 물음")
        elif not genders and not SEX_IMPLIED.search(gender_question):
            fix("sex", "계", "성별을 말하지 않음")
    if both and kind != "composition" and not SEX_TOTAL.search(gender_question):
        parsed["split_sex"] = True
        if kind == "compare" and len(parsed.get("regions") or []) == 1:
            fix("type", "value", "한 지역의 남녀 비교")
    if kind == "composition" and parsed.get("by") != "sex" and SEX_SPLIT.search(gender_question):
        fix("by", "sex", "질문의 남녀 구성 표현")

    _full_region_names(question, parsed, fix)
    _ground_regions(question, parsed, fix)

    if NATIONWIDE.search(question) and kind not in NO_REGION_TYPES:
        regions = parsed.get("regions") or []
        if not regions or all(r in NATIONAL_NAMES for r in regions):
            fix("regions", ["전국"], "질문의 전국 표현")

    years = {int(y) for y in YEAR.findall(question)}
    spans = slots.decades(question, today)
    has_abs_year = bool(years or spans)
    single = len(years) == 1 and not SPAN.search(question) and not SINCE.search(question)
    if single and parsed.get("type") not in ("trend", "other", "parse_error"):
        year = years.pop()
        if parsed.get("year") != year:
            fix("year", year, "질문의 연도")

    if not has_abs_year and (ref := timeref.resolve(question, today)):
        if ref.open_end:
            if parsed.get("start_year") != ref.year:
                fix("start_year", ref.year, ref.describe(today))
            if parsed.get("end_year"):
                fix("end_year", None, "'부터'는 최신 연도까지")
            if kind == "value":
                fix("type", "trend", "기간의 변화를 물음")
                kind = "trend"
        elif kind not in ("trend", "other", "parse_error") and parsed.get("year") != ref.year:
            fix("year", ref.year, ref.describe(today))
        elif (
            kind == "trend"
            and not CHANGE_ASK.search(question)
            and not SPAN.search(question)
            and len(_word_years(parsed)) < 2
        ):
            # 한 시점('작년에')만 말하고 변화를 묻지 않았는데 HCX가 추이로 읽었다
            # ("작년에 이사 들어온 사람이 나간 사람보다 많아?" → 2019~2024 추이, D-123 11/24).
            # 낱말 시점이 둘("작년에 … 재작년이랑 비교하면")이면 _apply_time 의 시작 · 끝을 둔다
            fix("type", "value", "한 시점만 물음 (변화를 묻는 말 없음)")
            kind = "value"
            if parsed.get("year") != ref.year:
                fix("year", ref.year, ref.describe(today))
    if spans and kind in ("value", "trend", "compare"):
        said = {int(y) for y in YEAR.findall(question)}
        start = min([lo for lo, _ in spans] + list(said))
        end = max([hi for _, hi in spans] + list(said))
        if kind != "trend":
            fix("type", "trend", "연대는 기간")
            kind = "trend"
        if parsed.get("start_year") != start:
            fix("start_year", start, "질문의 연대")
        if parsed.get("end_year") != end:
            fix("end_year", end, "질문의 연대")
        if parsed.get("year"):
            fix("year", None, "질문의 연대")
    born = slots.birth_years(question) - {int(y) for y in YEAR.findall(question)}
    born_keys = [k for k in ("year", "start_year", "end_year") if parsed.get(k) in born]
    for key in born_keys:
        fix(key, None, "'년생'은 출생 연도 → 자료 마지막 해")
    if not born_keys and not has_abs_year and not _says_year(question) and parsed.get("year"):
        fix("year", None, "연도를 말하지 않음 → 자료 마지막 해")

    regions = parsed.get("regions") or []
    if len(regions) > 1 and not parsed.get("share_of"):
        if parsed.get("type") == "value" and parsed.get("aggregate") != "sum":
            fix("type", "compare", "지역이 여럿인데 합계를 묻지 않음")
        elif (
            parsed.get("type") == "compare"
            and parsed.get("aggregate") == "sum"
            and not parsed.get("group")
        ):
            fix("type", "value", "여러 지역의 합계를 물음")

    names = asked_indicators(question)
    if (
        len(names) <= 1
        and parsed.get("type") in ("value", "trend", "compare")
        and (other := unlisted_measure(question))
    ):
        parsed["also_asked"] = other
        parsed["grounded"] = False
        rest = question.replace(other, " ")
        if (
            (parsed.get("ages") or parsed.get("age_group"))
            and _says_age(other)
            and not _says_age(rest)
        ):
            parsed["also_asked_ages"] = {k: parsed.get(k) for k in ("ages", "age_group")}
            if note := parsed.pop("age_note", None):
                parsed["also_asked_ages"]["age_note"] = note
            fix("ages", None, "연령은 함께 물은 지표의 조건")
            parsed["age_group"] = None
    if len(names) > 1 and parsed.get("type") in ("value", "trend", "compare"):
        parsed["indicators"] = names
    elif (
        BIRTH_PER_WOMAN.search(question)
        and re.search(r"낳", question)
        and parsed.get("indicator") in (None, "합계출산율", "출생아 수", "조출생률", *BIRTH_TOPIC)
    ):
        if parsed.get("indicator") != "합계출산율":
            fix("indicator", "합계출산율", "'한 명이 몇 명 낳는지'는 합계출산율")
        parsed["term"] = "합계출산율"
        parsed["grounded"] = True
        parsed.setdefault("asked", {})["birth_explain"] = True
    elif BIRTH_HOW_MANY.search(question) and not BIRTH_CLEAR.search(question):
        if parsed.get("indicator") in (None, "합계출산율", "출생아 수", "조출생률", *BIRTH_TOPIC):
            fix("indicator", "합계출산율", "'얼마나 낳아'는 합계출산율 + 출생아 수")
            parsed["term"] = "합계출산율"
            parsed["grounded"] = True
            parsed.pop("unsupported", None)
            if parsed.get("type") in (None, "other", "parse_error"):
                fix("type", "value", "'얼마나 낳아'")
            if parsed.get("type") in ("value", "trend", "compare"):
                parsed["indicators"] = ["합계출산율", "출생아 수"]
                parsed.setdefault("asked", {})["birth_pair"] = True

    _cross(question, parsed, fix)
    _change_rank(question, parsed, fix)
    _region_mean(question, parsed, fix)
    other_axis = OTHER_AXIS.search(question) and not AXIS_REGION.search(question)
    if parsed.get("type") == "rank" and other_axis:
        parsed["rank_axis"] = OTHER_AXIS.search(question)[1]
        parsed["grounded"] = False
    _one_region_years(parsed, fix)
    many_regions = len(parsed.get("regions") or []) > 1
    if parsed.get("months") and (parsed.get("type") != "value" or many_regions):
        fix("months_each", [list(parsed["months"][:2])], "달 범위를 지역 비교 · 순위에도")
        parsed["months"] = None
    each = months_asked(question)
    if not parsed.get("months") and each and _month_points_ok(question, parsed):
        parsed["months_each"] = each
        if len(each) > 1 and many_regions:
            if parsed.get("type") != "compare":
                fix("type", "compare", "달마다 지역끼리 견줌")
        elif len(each) > 1 and parsed.get("type") in ("compare", "trend"):
            fix("type", "value", "달마다 값 (달끼리 견줌)")
        elif many_regions and parsed.get("type") == "value":
            fix("type", "compare", "여러 지역의 달 값")
        elif parsed.get("type") == "trend" and not parsed.get("start_year"):
            fix("type", "value", "한 해의 달")
    if fixes:
        parsed["corrections"] = fixes
    return parsed


def _one_region_years(parsed, fix):
    """한 지역의 두 연도 비교를 추이로 해석해 화면에도 기간을 표시한다."""
    y0, y1 = parsed.get("start_year"), parsed.get("end_year")
    if (
        parsed.get("type") == "compare"
        and len(parsed.get("regions") or []) == 1
        and y0
        and y1
        and y0 != y1
        and not parsed.get("split_sex")
        and not parsed.get("indicators")
        and not parsed.get("group")
    ):
        fix("type", "trend", "한 지역의 두 연도 비교는 그 기간의 추이")


def _change_rank(question, parsed, fix):
    if parsed.get("cross") or not (m := CHANGE_RANK.search(question)):
        return
    if YEAR_ASKED.search(question):
        return
    many = parsed.get("top") or AXIS_REGION.search(question)
    if parsed.get("type") not in ("rank", "compare", "value", None) and not (
        parsed.get("type") == "trend" and many
    ):
        return
    parsed["rank_by"] = "decrease" if DECREASE_WORDS.search(m[0]) else "increase"
    if parsed.get("type") != "rank":
        fix("type", "rank", "변화량이 큰 지역을 물음")
    names = {name for *_, name in indicators.find_in_text(question)}
    if names == {"총인구"} and parsed.get("indicator") != "총인구":
        fix("indicator", "총인구", "'인구'의 변화량")
        parsed["grounded"] = True


REGION_MEAN = re.compile(
    r"(시도|광역|시군구|구|군|지역|도시)\s*(별|들|마다)?\s*(의\s*)?평균(?!\s*(과|보다|이랑|대비))"
)


def _region_mean(question, parsed, fix):
    if parsed.get("cross") or parsed.get("rank_by") or not (m := REGION_MEAN.search(question)):
        return
    if parsed.get("type") not in ("value", "rank", "compare", None):
        return
    regions = [r for r in parsed.get("regions") or [] if r != "전국"]
    sido = parsed.get("sido")
    level = "시도" if m[1] in ("시도", "광역") or not (regions or sido) else "시군구"
    parsed["region_mean"] = True
    if parsed.get("type") != "rank":
        fix("type", "rank", "여러 지역 값의 평균")
    if parsed.get("level") != level:
        fix("level", level, "평균할 지역 수준")
    parsed["top"] = None
    if level == "시군구" and not sido and regions:
        fix("sido", regions[0], "평균할 범위")
    if level == "시도" and sido:
        fix("sido", None, "시도 평균")
    if parsed.get("regions"):
        fix("regions", [], "지역 평균")


def _mean_of_regions(q):
    r = _compute({**q, "region_mean": False, "top": None, "target": None})
    rows = [x for x in r.get("ranking") or [] if x.get("value") is not None]
    if r.get("status") != "ok" or not rows:
        return r
    mean = sum(x["value"] for x in rows) / len(rows)
    within = (q.get("regions") or [None])[0] or q.get("sido")
    if within:
        within = (indicators.resolve_region(within) or {}).get("display") or within
    scope = f"{within} " if within else ""
    unit_name = {"시도": "시도", "시": "시"}.get(q.get("level") or "시도", "시군구")
    out = {k: v for k, v in r.items() if k not in ("ranking", "order", "level", "total_regions")}
    out.update(
        value=round(mean, 1 if r.get("kind") == "count" else 2),
        kind="value" if r.get("kind") == "count" else r.get("kind"),
        region=f"{scope}{len(rows)}개 {unit_name} 평균",
        formula=f"{len(rows)}개 {unit_name} 값의 합 ÷ {len(rows)} (단순 평균)",
    )
    return out


PEOPLE_INDICATOR = re.compile(rf"인구|사람|주민|남녀|{SEX_WORD}|연령|나이")
CROSS_DYNAMIC = "cross_dynamic"


def _about_births(parsed):
    name = indicators.canonical_name(parsed.get("indicator") or "") or ""
    return "출생" in name


def _cross(question, parsed, fix):
    found = cross_indicator.detect(question) or cross_indicator.detect_partial(question)
    if found is None or parsed.get("type") in ("causal", "composition"):
        return
    if found.official:
        if parsed.get("indicator") != found.official:
            fix("indicator", found.official, f"{found.formula}의 공식 지표")
    else:
        fix("indicator", found.name, f"두 지표를 나눈 값 ({found.formula})")
        parsed["cross"] = found.as_dict()
    if parsed.get("type") in (None, "other", "parse_error"):
        fix("type", "trend" if parsed.get("start_year") else "value", "두 지표를 나눈 값")
    elif parsed.get("type") == "compare" and len(parsed.get("regions") or []) <= 1:
        fix("type", "value", "'대비'는 두 지표를 나눈 것 (비교할 지역이 하나)")
    parsed["term"] = found.official or found.name
    parsed["indicators"] = None
    parsed["grounded"] = True
    parsed.pop("unsupported", None)
    if (found.plus or found.form == "sum") and not parsed.get("group"):
        parsed.pop("aggregate", None)


AND_WORDS = re.compile(r"^(와|과|하고|랑|이랑|및|,|그리고|,그리고|와함께|과함께)$")


PLAIN_POPULATION = {
    "총", "전체", "주민등록", "우리나라", "한국", "국내", "전국", "남자", "여자", "남성", "여성",
    "남녀", "이상", "이하", "미만", "대", "세", "살", "노인", "고령", "노년", "청년", "청소년",
    "유소년", "어린이", "아동", "생산연령", "중장년", "현재", "작년", "올해", "지난해", "의",
    "수도권", "비수도권", "광역시", "특별시", "시군구", "시도", "지역", "구", "군", "시", "도",
    "나이", "연령", "연령대", "만",
    "합쳐서", "합친", "합하면", "더한", "중", "모두", "전부", "다", "사는",
    "개", "곳", "군데",
}  # fmt: skip
POPULATION_NOUN = re.compile(r"([가-힣]+)\s*인구")


PEOPLE_WORD = re.compile(r"사람|주민")
PLAIN_PEOPLE_WORD = re.compile(
    r"^(?:사람들?|주민들?|인구수?|몇|몇명|명(?:이야|이에요|입니까|인가요|인지|이나)?|수|얼마|얼마나|"
    r"총|전체|모두|다|더|제일|가장|어디|어느|쪽|중|누가|"
    r"순|순서|순위|위|등|상위|하위|곳|군데|개|구|시|군|도|동|읍|면|지역|도시|시도|시군구|전국|우리나라|"
    r"한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+|top\d*|"
    r"살아요?|사는|살고|살지|사나요?|삽니까|많아요?|많은|많이|많나요?|많습니까|많을까|"
    r"적어요?|적은|적게|적나요?|있어요?|있나요?|있는|있습니까|돼요?|되나요?|되지|됩니까|되는지|"
    r"알려줘|알려|줘|보여줘|비교해줘|비교해|비교|차이|현재|지금|요즘|최근)$",
    re.IGNORECASE,
)
PEOPLE_PARTICLES = (
    "에서는", "에서", "에는", "으로", "보다", "까지", "부터", "이랑", "하고", "이나",
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "랑", "도", "로", "만", "나",
)  # fmt: skip


def _plain_token(token):
    cut = [token[: -len(p)] for p in PEOPLE_PARTICLES if token.endswith(p) and token != p]
    forms = [token, *cut]
    for form in forms:
        if PLAIN_PEOPLE_WORD.match(form) or indicators.find_regions(form) == [form]:
            return True
        if form not in COMMON_WORD_REGIONS and len(form) >= 2:
            if indicators.resolve_region(form).get("status") == "ok":
                return True
    return False


def _plain_people_question(question):
    if not PEOPLE_WORD.search(question) or OTHER_PEOPLE.search(question):
        return False
    text = YEAR.sub(" ", question)
    for ref in timeref.find(question):
        text = text.replace(ref["expr"], " ")
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", text)
    return bool(tokens) and all(_plain_token(t) for t in tokens)


def population_modifier(question):
    if group := agegroup.resolve(question):
        question = question.replace(group.expr, " ")
    for word in REGION_GROUPS:
        if _group_in(word, question):
            question = question.replace(word, " ")
    if sub := find_sub_area(question):
        question = question.replace(sub[0], " ")
    for m in POPULATION_NOUN.finditer(question):
        words = {m[1], re.sub(r"(의|은|는|이|가|에서|에|별)$", "", m[1]) or m[1]}
        if any(w in PLAIN_POPULATION or re.search(r"\d", w) for w in words):
            continue
        if any(
            indicators.find_regions(w) or agegroup.resolve(w) or _short_region(w) for w in words
        ):
            continue
        return min(words, key=len)
    return None


def _short_region(word):
    if len(word) < 2 or word in COMMON_WORD_REGIONS:
        return False
    try:
        return indicators.resolve_region(word)["status"] in ("ok", "ambiguous_region")
    except StatsTableMissing:
        return False


MEASURE_JOIN = re.compile(
    r"(?<=[가-힣0-9])(?:이랑|랑|하고|와|과)\s+|\s*(?:(?<!\d),|,(?!\d))\s*|\s+(?:및|그리고)\s+"
)
MEASURE_TAIL = re.compile(r"(?:수|율|비율|비중|인구|가구|건수|세대)$")
ASK_TAIL = re.compile(r"\s+(?:알려|보여|얼마|몇|각각|비교|궁금|어때|좀|같이|함께).*$")


def unlisted_measure(question):
    parts = [p for p in MEASURE_JOIN.split(question.strip()) if p and p.strip()]
    if len(parts) < 2:
        return None
    listed = [bool(indicators.find_in_text(p)) for p in parts]
    if not any(listed):
        return None
    for part, has in zip(parts, listed, strict=True):
        if has:
            continue
        phrase = re.sub(r"(?:은|는|이|가|을|를|도)$", "", ASK_TAIL.sub("", part).strip(" ?.!"))
        words = [w for w in phrase.split() if not YEAR.search(w) and not indicators.find_regions(w)]
        phrase = " ".join(words)
        if len(indicators._norm(phrase)) >= 2 and MEASURE_TAIL.search(indicators._norm(phrase)):
            return phrase
    return None


def _says_age(text):
    return bool(extract_ages(text) or agegroup.resolve(text))


SHARED_WHEN = re.compile(r"^(?:(?:19|20)\d{2}년?|작년|올해|지난해|재작년|최근)")


def measure_question(question, measure):
    parts = [p.strip() for p in MEASURE_JOIN.split(question.strip()) if p and p.strip()]
    key = indicators._norm(measure)
    own = next((p for p in parts if key in indicators._norm(p)), measure)
    shared = [
        w
        for p in parts
        if p != own and indicators.find_in_text(p)
        for w in p.split()
        if SHARED_WHEN.match(w) or indicators.find_regions(w) or _short_region(w)
    ]
    return " ".join([*shared, own])


def asked_indicators(question):
    spans = sorted(indicators.find_in_text(question))
    text = indicators._norm(question)
    names = [spans[0][2]] if spans else []
    for (_, end, _), (start, _, name) in zip(spans, spans[1:], strict=False):
        if not AND_WORDS.match(text[end:start]):
            return []
        names.append(name)
    names = list(dict.fromkeys(names))
    return names[:MAX_INDICATORS] if len(names) > 1 else []


def _ground_regions(question, parsed, fix):
    if parsed.get("group"):
        return
    text = indicators._norm(question)
    found = set()
    for name in indicators.find_regions(question):
        reg = indicators.resolve_region(name)
        for c in [reg] if reg["status"] == "ok" else []:
            found |= {c["region_code"], c.get("sido_code")}
        for cand in reg.get("candidates", []):
            found.add(indicators._norm(cand))
    for r in parsed.get("regions") or []:
        if r == "전국" or r in NATIONAL_NAMES or indicators._norm(r) in text:
            continue
        reg = indicators.resolve_region(r)
        codes = {reg.get("region_code"), indicators._norm(reg.get("display", ""))}
        if reg["status"] != "ok" or codes & found:
            continue
        short = re.sub(
            r"(특별자치시|특별자치도|특별시|광역시|시|군|구|도)$", "", reg.get("region_name") or ""
        )
        named = found - {None}
        same_sido = not named or reg.get("sido_code") in named
        spans = indicators.find_in_text(question)
        hits = [m.start() for m in re.finditer(re.escape(short), text)] if len(short) >= 2 else []
        in_indicator = bool(hits) and all(any(a <= i < b for a, b, _ in spans) for i in hits)
        if hits and same_sido and not in_indicator and short not in COMMON_WORD_REGIONS:
            if not named and indicators.resolve_region(short)["status"] == "ambiguous_region":
                fix(
                    "regions",
                    [short if x == r else x for x in parsed["regions"]],
                    "짧은 이름이 여러 지역 → 되묻기",
                )
            continue
        parsed["ungrounded_region"] = r
        return


def _full_region_names(question, parsed, fix):
    regions = parsed.get("regions") or []
    if not regions or parsed.get("group"):
        return
    asked = indicators.find_regions(question)
    out = []
    for r in regions:
        short = indicators._norm(r)
        longer = [n for n in asked if n != short and n.startswith(short)]
        if short not in asked and len(longer) == 1:
            if indicators.resolve_region(longer[0]).get("status") == "ok":
                r = longer[0]
        out.append(r)
    if out != regions:
        fix("regions", out, "질문의 정식 지역 이름")


def _merge_regions(regions, question=None):
    out = []
    for name in regions:
        if out:
            kind = indicators.resolve_region(out[-1]).get("kind")
            if kind in ("시도", "시군구"):
                both = indicators.resolve_region(f"{out[-1]} {name}")
                ok = both["status"] == "ok" and both["kind"] != "시도"
                if kind == "시군구":
                    ok = ok and both["kind"] == "일반구" and _next_to(question, out[-1], name)
                if ok:
                    out[-1] = f"{out[-1]} {name}"
                    continue
        out.append(name)
    return out


def _next_to(question, city, gu):
    if question is None:
        return True
    stem = re.sub(r"시$", "", indicators._norm(city.split()[-1]))
    pattern = re.escape(stem) + "시?" + re.escape(indicators._norm(gu))
    return re.search(pattern, indicators._norm(question)) is not None


AGE_DEFINED = {
    "고령인구 비율": ([(65, None)], PCT_ASK, None),
    "유소년인구 비율": ([(0, 14)], PCT_ASK, None),
    "노령화지수": ([(0, 14), (65, None)], re.compile(r"몇\s*배|대비"), (0, 14)),
}
PEOPLE_ASK = re.compile(r"인구|몇\s*(명|분)|사람|인원")
OTHER_UNITS = re.compile(r"가구|세대|몇\s*(곳|개|건|집)")
POPULATION_WORDS = re.compile(r"인구|인원|사람|수|명|분|몇|들|[\s()·,~\-]")
AGE_PATTERNS = (
    agegroup.AGE_OVER_UNDER, AGE_RANGE, AGE_SPAN, agegroup.AGE_OVER, AGE_UNDER, AGE_UNDER_EQ,
    AGE_DECADE_SPAN, AGE_DECADE_RANGE, AGE_DECADE_OVER, AGE_DECADE_PART, AGE_DECADE,
)  # fmt: skip


def _age_bands(question):
    bands = {tuple(b) for b in extract_ages(question)}
    return bands | {(r.lo, r.hi) for r in agegroup.resolve_all(question)}


def _band_spans(question):
    spans = [(a, b, tuple(band)) for a, b, band in age_spans(question)]
    spans += [(a, b, (r.lo, r.hi)) for a, b, r in agegroup.spans(question)]
    return sorted(spans)


BASE_MARK = re.compile(r"\s*(?:이|가|은|는|들)?\s*(?:보다|대비|에\s*비해|에\s*비하면)")


def _base_band(question):
    for _a, b, band in _band_spans(question):
        if BASE_MARK.match(question, b):
            return band
    return None


def _age_grounded(question, name):
    rule = AGE_DEFINED.get(name)
    if rule is None:
        return False
    bands, ask, base = rule
    if not ask.search(question) or not set(bands) <= _age_bands(question):
        return False
    said = _base_band(question)
    return base is None or said is None or said == base


COUNT_FILLER = frozenset(
    "다 모두 전부 총 합치면 합하면 더하면 대략 약 대충 중 중에 중에서 사는 살고 있는 계신 사시는"
    " 남자 여자 남성 여성 남자애 여자애 사람 인구 주민 인원".split()
)
PARTICLES = re.compile(r"(?:은|는|이|가|을|를|도|의|에|에서|랑|이랑|하고|와|과|들)$")


def _counts_people(question, parsed):
    bands = _band_spans(question)
    m = next((x for x in PEOPLE_ASK.finditer(question) if bands and x.start() >= bands[0][1]), None)
    if m is None:
        return bool(parsed.get("age_group"))
    spans = [s for s in bands if s[1] <= m.start()]
    regions = parsed.get("regions") or []

    def allowed(word):
        bare = PARTICLES.sub("", word) or word
        return any(w in COUNT_FILLER or any(w in r for r in regions) for w in (word, bare))

    if not all(
        allowed(w) for w in re.findall(r"[가-힣A-Za-z]+", question[spans[-1][1] : m.start()])
    ):
        return False
    head = question[: spans[0][0]]
    return all(allowed(w) for w in re.findall(r"([가-힣A-Za-z]+)\s*중(?:에|에서)?\s", head + " "))


def _asks_age_population(question, parsed):
    return bool(
        parsed.get("ages")
        and not PCT_ASK.search(question)
        and not RATIO_ASK.search(question)
        and not OTHER_UNITS.search(question)
        and (PEOPLE_ASK.search(question) or parsed.get("age_group"))
        and _counts_people(question, parsed)
    )


def _population_label(label, regions=()):
    s = label
    for r in regions:
        s = s.replace(r, "")
    for pattern in AGE_PATTERNS:
        s = pattern.sub("", s)
    s = re.sub(r"\d+\s*(세|살)", "", s)
    for word in sorted((w for words, *_ in agegroup.GROUPS for w in words), key=len, reverse=True):
        s = s.replace(word, "")
    return POPULATION_WORDS.sub("", s) == ""


def _age_population(question, parsed, fix):
    if indicators.spec(parsed.get("indicator")) is not None and not parsed.get("unsupported"):
        return
    labels = [x for x in (parsed.get("indicator"), parsed.get("term")) if x]
    regions = parsed.get("regions") or []
    if not _asks_age_population(question, parsed):
        return
    if not all(_population_label(x, regions) for x in labels):
        return
    fix("indicator", "총인구", "연령 구간 인구 수를 물음")
    parsed.pop("unsupported", None)


def _says_other_indicator(question, parsed):
    if parsed.get("ages") or parsed.get("age_group"):
        return False
    q = indicators._norm(question)
    for phrase in (parsed.get("term"), parsed.get("indicator")):
        if not phrase or not isinstance(phrase, str):
            continue
        p = indicators._norm(phrase)
        if len(p) < 2 or p not in q or re.search(r"\d{4}", p):
            continue
        if extract_ages(phrase) or agegroup.resolve(phrase):
            continue
        if any(n == "총인구" for *_, n in indicators.find_in_text(phrase)):
            continue
        if indicators.find_regions(phrase):
            continue
        return True
    return False


def _guard_indicator(question, parsed, names, fix):
    sp = indicators.spec(parsed.get("indicator"))
    if sp is None:
        return
    k = lookup.kind_of(sp)
    rest = indicators._norm(question)
    for start, end, name in indicators.find_in_text(question):
        if name == sp["name"]:
            rest = rest[:start] + " " * (end - start) + rest[end:]
    asks_count = COUNT_ASK.search(rest) and not PCT_ASK.search(rest)
    ungrounded = (
        sp["method"] == "computed"
        and sp["name"] != "총인구"
        and sp["name"] not in names
        and not _age_grounded(question, sp["name"])
    )
    if (k in ("ratio", "index") and asks_count) or ungrounded:
        if _asks_age_population(question, parsed):
            fix("indicator", "총인구", "연령 구간 인구 수를 물음")
        elif parsed.get("type") == "composition" and "총인구" in names:
            fix("indicator", "총인구", "인구 구성을 물음")
            parsed.pop("unsupported", None)
        elif _both_sexes(question) and not OTHER_PEOPLE.search(question):
            fix("indicator", "총인구", "남녀 인구를 견줌")
        else:
            term = parsed.get("term") or parsed.get("indicator")
            fix("indicator", term, "질문에 근거가 없는 지표")
            parsed["unsupported"] = True


def _both_sexes(question):
    genders = [sex for sex, pattern in GENDER_WORDS.items() if pattern.search(question)]
    return len(genders) == 2 or (not genders and bool(SEX_SPLIT.search(question)))


def _region_list(question, parsed, fix):
    regions = parsed.get("regions") or []
    level = "시도" if re.search(r"시도\s*(별|마다)", question) else "시군구"
    if parsed.get("level") != level:
        fix("level", level, "지역별 목록")
    if level == "시군구":
        found = indicators.find_regions(question)
        names = [*found, *regions, parsed.get("sido")]
        sido = next(
            (n for n in names if n and indicators.resolve_region(n).get("kind") == "시도"), None
        )
        if sido and sido != parsed.get("sido"):
            fix("sido", sido, "지역별 목록의 범위")
        if regions:
            fix("regions", [], "지역별 목록")
    if re.search(r"(별|마다)", question) and parsed.get("top"):
        fix("top", None, "지역별 목록은 전체")


GROUP_TAIL = re.compile(r"(?:의|에서|에|은|는|이|가|로|으로|도|만|지역|인구)?(?![가-힣])")


def _group_in(word, question):
    if not word.endswith((" 외", " 밖")):
        return word in question
    return any(GROUP_TAIL.match(question, m.end()) for m in re.finditer(re.escape(word), question))


def _read_question(question, parsed, fix):
    kind = parsed.get("type")
    for word, members in REGION_GROUPS.items():
        if _group_in(word, question):
            fix("regions", members, f"'{word}'")
            parsed["group"] = word
            break
    if SUM_WORDS.search(question) or (parsed.get("group") and kind == "value"):
        parsed["aggregate"] = "sum"
    if parsed.get("group") and kind == "compare":
        parsed.pop("aggregate", None)

    if CAUSAL.search(question) and kind not in ("causal", "composition", "other"):
        fix("type", "causal", "원인·효과를 묻는 질문")
        kind = "causal"
    if (m := UNSUPPORTED_MEASURE.search(question)) and kind not in ("other", "parse_error"):
        parsed["unsupported_condition"] = m[0].strip()
    guess = asks_guess(question)
    if guess:
        parsed["guess"] = True
    if PREDICT.search(question) and kind not in ("other", "parse_error"):
        lo, hi = indicators._data_years()
        years = [y for y in (parsed.get("year"), parsed.get("end_year")) if y]
        if not any(y > hi for y in years):
            parsed["prediction"] = True
    if (
        guess
        and kind not in ("other", "parse_error")
        and FUTURE_WORDS.search(question)
        and DIRECTION_WORDS.search(question)
    ):
        parsed["prediction"] = True
    if (m := SPAN.search(question)) and kind in ("value", "trend", "causal"):
        start, end = sorted((int(m[1]), int(m[3])))
        if kind == "value":
            fix("type", "trend", "두 연도 사이 변화")
        if (parsed.get("start_year"), parsed.get("end_year")) != (start, end):
            fix("start_year", start, "질문의 기간")
            fix("end_year", end, "질문의 기간")

    asked = {}
    for mode, pattern in YEAR_EXTREME.items():
        if pattern.search(question):
            asked["extreme"] = mode
    if STREAK.search(question):
        asked["streak"] = "negative" if re.search(r"유출|빠져|감소|줄", question) else "positive"
        if not SPAN.search(question) and not SINCE.search(question):
            for key in ("start_year", "end_year"):
                if parsed.get(key):
                    fix(key, None, "연속 기간은 수록 기간 전체에서 셈")
    if REBOUND.search(question):
        asked["rebound"] = True
    if PER_YEAR.search(question):
        asked["per_year"] = True
    by_year = {"extreme", "streak", "rebound", "per_year"} & set(asked)
    if m := RECENT.search(question):
        asked["recent"] = int(m[2])
    elif LATEST.search(question) and not YEAR.search(question) and not timeref.resolve(question):
        asked["latest"] = True
        if THESE_DAYS.search(question):
            asked["these_days"] = True
        if kind == "trend" and not by_year and not CHANGE_ASK.search(question):
            fix("type", "value", "최근 값을 물음 (변화를 묻지 않음)")
            for key in ("start_year", "end_year"):
                if parsed.get(key):
                    fix(key, None, "최근 값을 물음")
            kind = "value"
        if kind in LATEST_KINDS and parsed.get("year"):
            fix("year", None, "최근 값은 자료 마지막 해")
        elif kind == "trend":
            for key in ("start_year", "end_year", "year"):
                if parsed.get(key):
                    fix(key, None, "최근 추이는 자료 마지막 해까지")
    elif (
        kind in LATEST_KINDS
        and not by_year
        and not YEAR.search(question)
        and not _says_year(question)
        and not timeref.resolve(question)
        and not SPAN.search(question)
        and not SINCE.search(question)
        and indicators.spec(parsed.get("indicator")) is not None
    ):
        asked["latest"] = True
    elif (
        kind == "trend"
        and not by_year
        and not _says_year(question)
        and not SPAN.search(question)
        and not SINCE.search(question)
        and not ask_followup.FROM_PAST.search(question)
    ):
        for key in ("start_year", "end_year", "year"):
            if parsed.get(key):
                fix(key, None, "기간을 말하지 않음 → 자료 마지막 해까지 최근 6개 연도")
    if ask_followup.FROM_PAST.search(question) and not SPAN.search(question):
        asked["from_start"] = True
        if parsed.get("start_year"):
            fix("start_year", None, "'과거부터'는 수록 첫해부터")
        until = int(m[1]) if (m := UNTIL.search(question)) else None
        if parsed.get("end_year") != until:
            fix("end_year", until, "질문의 끝 연도" if until else "'과거부터'는 최신까지")
        if kind != "trend":
            fix("type", "trend", "과거부터의 변화를 물음")
            kind = "trend"
    fixed_span = any(r.get("kind") == "span_from" for r in parsed.get("time_refs") or [])
    if (m := SINCE.search(question)) and not SPAN.search(question) and not fixed_span:
        asked["since"] = int(m[1])
        if parsed.get("start_year") != int(m[1]):
            fix("start_year", int(m[1]), "질문의 시작 연도")
        if parsed.get("end_year"):
            fix("end_year", None, "'이후'는 최신 연도까지")
    if (by_year or (asked.get("recent") and parsed.get("aggregate"))) and kind != "trend":
        fix("type", "trend", "연도별 변화를 물음")
    if PREV_YEAR.search(question) and parsed.get("type") in ("value", "trend", "compare"):
        year = parsed.get("year") or parsed.get("end_year")
        if year:
            if parsed.get("type") != "trend":
                fix("type", "trend", "전년 대비를 물음")
            fix("start_year", year - 1, "전년 대비")
            fix("end_year", year, "전년 대비")
    if RATIO_ASK.search(question):
        asked["ratio"] = True
    if survey := survey_for(question, parsed):
        asked["survey"] = survey
    elif missing := survey_missing(question, parsed):
        asked["survey_missing"] = missing
    focus = [label for word, label in FOCUS_WORDS.items() if word in question]
    if focus:
        asked["focus"] = list(dict.fromkeys(focus))
    if asked:
        parsed["asked"] = asked

    if (
        parsed.get("type") in ("value", "compare")
        and RANK_ASK.search(question)
        and not by_year
        and len(parsed.get("regions") or []) <= 1
    ):
        fix("type", "rank", "가장 높은·낮은 지역을 물음")
        level = None
        if "시도" in question:
            level = "시도"
        elif re.search(r"시군구|구\b", question):
            level = "시군구"
        if level and parsed.get("level") != level:
            fix("level", level, "질문의 지역 수준")
        if parsed.get("regions") and level == "시군구":
            fix("sido", parsed["regions"][0], "순위 범위")
        fix("regions", [], "순위")
    if parsed.get("type") == "rank":
        if LEVEL_CITY.search(question) and parsed.get("level") != "시":
            fix("level", "시", "전국 시 중 순위")
        if ASCENDING.search(question) and not parsed.get("ascending"):
            fix("ascending", True, "가장 낮은 순위")
        elif (
            parsed.get("ascending")
            and DESCENDING.search(question)
            and not DECREASE.search(question)
        ):
            fix("ascending", False, "높은 순위")
        if parsed.get("indicator") == "순이동" and re.search(r"빠져나간|유출", question):
            if not parsed.get("ascending"):
                fix("ascending", True, "순유출이 큰 순 = 순이동이 낮은 순")
        if re.search(r"몇\s*위", question):
            regions = parsed.get("regions") or [
                r for r in indicators.find_regions(question) if r != "전국"
            ]
            if regions:
                parsed["target"] = regions[0]
                parsed["top"] = None
    if parsed.get("type") == "rank" and BOTH_ENDS.search(question) and parsed.get("top"):
        fix("top", None, "가장 높은 곳과 낮은 곳을 함께 물음")
        if parsed.get("ascending"):
            fix("ascending", False, "높은 순으로 보여줌")
    if (m := TOP_N.search(question)) and parsed.get("type") in ("rank", "composition", "trend"):
        n = int(m[2] or m[3])
        if parsed.get("top") != n and n < 50:
            fix("top", n, "질문의 개수")

    if parsed.get("type") == "compare" and "전국" in question:
        regions = parsed.get("regions") or []
        if "전국" not in regions:
            fix("regions", [*regions, "전국"], "전국 평균과 비교")
    sp = indicators.spec(parsed.get("indicator"))
    if (
        sp is not None
        and lookup.kind_of(sp) == "count"
        and "전국" in question
        and PCT_ASK.search(question)
        and parsed.get("type") in ("value", "compare")
    ):
        regions = [r for r in parsed.get("regions") or [] if r != "전국"]
        regions = regions or [r for r in indicators.find_regions(question) if r != "전국"]
        if regions and (parsed["type"] == "value" or len(regions) == 1):
            if parsed["type"] == "compare":
                fix("type", "value", "전국 중 한 지역의 비중을 물음")
            fix("regions", regions[:1], "전국 대비 비중")
            parsed["share_of"] = "전국"


MAX_TREND_REGIONS = 20
MAX_INDICATORS = 3


def born_ages(q, latest):
    born = q["born"]
    ref = q.get("end_year") or q.get("year") or latest
    bands = slots.birth_ages([tuple(b) for b in born["spans"]], ref)
    if not bands:
        return None
    q["ages"] = [list(b) for b in bands]
    q["age_group"] = {
        "expr": born["expr"],
        "range": " · ".join(slots.age_label(*b) for b in bands),
        "basis": f"{ref}년 기준 만 나이",
    }
    if q.get("type") == "trend":
        q["age_group"].update(
            range="조회 연도별 만 나이", basis="각 연도에서 출생연도를 뺀 만 나이"
        )
    return bands


def basis_note(sp, term):
    terms = {indicators._norm(t) for t in sp.get("basis_terms") or []}
    return sp.get("basis_note") if indicators._norm(term) in terms else None


def age_basis_note(group):
    basis = group["basis"].replace(" (", " · ").replace(")", "")
    return f"{josa(repr_q(group['expr']), '은는')} {ro(group['range'])} 계산했습니다 ({basis})"


def decade_part_note(question):
    """'20대 후반'처럼 초반·중반·후반으로 말한 연령의 계산 기준."""
    said = []
    for m in AGE_DECADE_PART.finditer(agegroup.native_to_digits(question)):
        if int(m[1]) % 10 == 0:
            lo, hi = DECADE_PART[m[2]]
            band = f"{int(m[1]) + lo}~{int(m[1]) + hi}세"
            said.append(f"{josa(repr_q(m[0]), '은는')} {ro(band)} 계산했습니다")
    if not said:
        return None
    return f"{', '.join(said)} (초반 0~3세 · 중반 4~6세 · 후반 7~9세)"


NO_SEX_SPLIT = frozenset({"성비", "세대당 인구", "인구밀도"})


def _splits_by_sex(q):
    if q.get("type") not in ("value", "trend", "compare") or q.get("sex") in ("남", "여"):
        return False
    sp = indicators.spec(q.get("indicator"))
    if sp is None or sp["name"] in NO_SEX_SPLIT:
        return False
    if sp.get("method") == "lookup":
        return any(s.get("by_sex") for s in sp["sources"]) and not lookup.needs_split(sp, "계")
    return True


MONTH_SPAN = re.compile(r"(\d{1,2})\s*월\s*(?:부터|에서|~|-|∼)\s*(\d{1,2})\s*월")
MONTH_ONE = re.compile(r"(?<![\d개])(\d{1,2})\s*월(?!\s*(?:말|별|부터|에서))")
HALF = {"상반기": (1, 6), "하반기": (7, 12)}
QUARTER = re.compile(r"([1-4])\s*분기")


def months_asked(question):
    out = []
    for m in MONTH_SPAN.finditer(question):
        a, b = int(m[1]), int(m[2])
        if 1 <= a <= b <= 12:
            out.append((a, b))
    rest = MONTH_SPAN.sub(" ", question)
    for word, span in HALF.items():
        if word in rest:
            out.append(span)
    for m in QUARTER.finditer(rest):
        q = int(m[1])
        out.append((q * 3 - 2, q * 3))
    for m in MONTH_ONE.finditer(rest):
        if 1 <= (a := int(m[1])) <= 12:
            out.append((a, a))
    return [list(x) for x in dict.fromkeys(out)]


MONTH_POINT = re.compile(r"\d{1,2}\s*월\s*(?:말|기준|현재|\d+\s*일)")


def _month_points_ok(question, parsed):
    if MONTH_POINT.search(question):
        return False
    sp = indicators.spec(parsed.get("indicator") or "")
    return not (sp and sp.get("method") == "computed")


def _month_label(m0, m1):
    return f"{m0}월" if m0 == m1 else f"{m0}~{m1}월"


def _with_months(q):
    months = q.get("months_each") or []
    if len(months) > 1:
        parts = [compute({**q, "months_each": [m]}) for m in months]
        return _combine(parts, by="month")
    m0, m1 = months[0]
    token = lookup.MONTHS.set((m0, m1))
    try:
        result = compute(
            {
                **q,
                "months_each": None,
                "months": None,
                "asked": {**(q.get("asked") or {}), "latest": False},
            }
        )
    finally:
        lookup.MONTHS.reset(token)
    for r in _leaves(result):
        if r.get("year"):
            r["as_of"] = f"{r['year']}년 {_month_label(m0, m1)}"
            r["month_label"] = _month_label(m0, m1)
            r["months"] = [m0, m1]
            summed = "을 더한 값" if m1 > m0 else ""
            r.setdefault("notes", []).append(
                f"KOSIS 월별 자료의 {_month_label(m0, m1)} 값{summed}입니다"
            )
    return result


def compute(q):
    if q.get("months_each"):
        return _with_months(q)
    if q.get("months") and q.get("dynamic_spec") and not q["dynamic_spec"].get("monthly_sources"):
        return _with_months({**q, "months_each": [list(q["months"][:2])]})
    if (born := q.get("born")) and len(born["spans"]) > 1:
        spans = []
        for lo, hi in sorted(born["spans"]):
            if spans and lo <= spans[-1][1] + 1:
                spans[-1][1] = max(hi, spans[-1][1])
            else:
                spans.append([lo, hi])
        if len(spans) > 1:
            return _combine(
                [
                    compute(
                        {
                            **q,
                            "born": {
                                "expr": f"{lo}~{hi}년생" if lo != hi else f"{lo}년생",
                                "spans": [[lo, hi]],
                            },
                        }
                    )
                    for lo, hi in spans
                ]
            )
    if q.get("split_sex") and _splits_by_sex(q):
        return _combine([compute({**q, "sex": s, "split_sex": False}) for s in ("남", "여")])
    result = _compute(q)
    asked = q.get("asked") or {}
    if asked.get("birth_explain") and result.get("status") == "ok":
        for r in _leaves(result):
            if TFR_MEANING not in r.setdefault("notes", []):
                r["notes"].insert(0, TFR_MEANING)
    if (
        asked.get("latest")
        and not q.get("year")
        and (result.get("status") == "out_of_range" or _monthly_empty(result))
    ):
        retry = _compute({**q, "asked": {**asked, "latest": False}})
        if retry.get("status") == "ok":
            result = retry
    elif asked.get("latest") and not q.get("year") and result.get("as_of") and "ranking" in result:
        annual = _compute({**q, "asked": {**asked, "latest": False}})
        if (annual.get("total_regions") or 0) > (result.get("total_regions") or 0):
            annual["skipped_month"] = result["as_of"]
            result = annual
    if (missing := asked.get("survey_missing")) and result.get("status") == "ok":
        sp = indicators.spec(q.get("indicator"))
        own = " · ".join(indicators.source_surveys(sp)) if sp else ""
        if own:
            result["lead"] = (
                f"{josa(sp['name'], '은는')} {ro(own)}만 공표되어 {missing} 기준 값은 없습니다."
            )
            result["lead_basis"] = f"{own} 기준"
    if survey := asked.get("survey"):
        _add_default_value(q, result)
        if result.get("status") != "ok" and (reason := result.get("reason")):
            if not reason.startswith(survey):
                result["reason"] = f"{survey} {reason}"
    _add_latest_note(q, result)
    _add_definition_note(result)
    if result.get("sub_future") and not q.get("guess"):
        result["status"] = "out_of_range"
    return result


def _monthly_empty(result):
    if result.get("status") != "ok":
        return False
    leaves = [r for r in _leaves(result) if r.get("as_of")]
    vals = [
        v.get("value", v.get("count"))
        for r in leaves
        for v in (r.get("values") or r.get("ranking") or r.get("parts") or [r])
    ]
    return bool(leaves) and all(v is None for v in vals)


def _add_latest_note(q, result):
    if result.get("status") != "ok" or q.get("prediction"):
        return
    asked_year = q.get("year") and not q.get("year_filled")
    for r in _leaves(result):
        if r.get("series"):
            if q.get("end_year"):
                continue
            last = r["series"][-1]
            when = last.get("as_of") or f"{last['year']}년"
            name = r.get("indicator") or "이"
            note = f"{name} 자료는 {when}이 가장 최근입니다"
        elif asked_year:
            continue
        elif r.get("as_of") or r.get("year"):
            note = f"가장 최근 자료인 {_when(r)} 기준으로 답했습니다"
            if month := r.get("skipped_month"):
                note = (
                    f"가장 최근 연간 자료인 {_when(r)} 기준으로 답했습니다. "
                    f"최신 달({month}) 자료는 행정구역 개편으로 일부 지역이 달라 쓰지 않았습니다"
                )
            elif r.get("as_of"):
                r["notes"] = [n for n in r.get("notes", []) if "KOSIS 월별 자료의" not in n]
        else:
            continue
        if note not in (notes := r.setdefault("notes", [])):
            notes.append(note)


def _add_definition_note(result):
    if result.get("status") != "ok":
        return
    for r in _leaves(result):
        if r.get("method") == "dynamic" or r.get("status", "ok") != "ok":
            continue
        sp = indicators.spec(r.get("indicator"))
        if not sp or not (definition := sp.get("definition")):
            continue
        notes = r.setdefault("notes", [])
        if not any(definition in n for n in notes):
            notes.insert(0, definition)


def _add_default_value(q, result):
    if result.get("status") != "ok" or result.get("value") is None or q.get("type") != "value":
        return
    asked = {k: v for k, v in (q.get("asked") or {}).items() if k not in ("survey", "latest")}
    other = _compute({**q, "asked": asked, "year": result.get("year")})
    if other.get("status") != "ok" or other.get("value") is None:
        return
    when = f"{result['year']}년 " if result.get("year") else ""
    value = _val(other["value"], other.get("unit", ""))
    result.setdefault("notes", []).append(f"{when}주민등록인구 기준으로는 {value}입니다")


CALC_ERROR_TEXT = "계산하는 중에 문제가 생겨 답하지 못했습니다. 잠시 후 다시 질문해 주세요."


def _calc_error(e: Exception) -> dict:
    error_id = uuid4().hex[:8]
    logger.error("계산 중 오류 [%s]", error_id, exc_info=e)
    return {"status": "error", "reason": CALC_ERROR_TEXT, "error_id": error_id}


def _compute(q):
    lookup.refresh_if_reloaded()
    kind = q.get("type")
    ind = q.get("indicator") or ""
    regions = q.get("regions") or []
    sex = q.get("sex") or "계"
    ages = [tuple(a) for a in q.get("ages") or []] or None
    lo, hi = indicators._data_years()
    year = q.get("year") or hi
    if q.get("born"):
        ages = born_ages(q, hi)
        if not ages and kind != "trend":
            return {
                "status": "missing_data",
                "reason": "조회 연도에는 해당 출생 집단이 아직 태어나지 않았습니다.",
            }

    if kind == "parse_error":
        return {
            "status": "parse_error",
            "reason": "질문을 이해하지 못했습니다. 지역·연도·지표를 넣어 다시 질문해 주세요.",
        }
    if kind == "other":
        return {
            "status": "not_supported_question",
            "reason": "지역별 인구통계 질문에만 답할 수 있습니다.",
            "supported": list(indicators.specs()),
        }

    names = q.get("indicators") or []
    if len(names) > 1 and kind in ("value", "trend", "compare"):

        def each(extra=None):
            return [
                compute({**q, "indicators": None, "indicator": n, "term": n, **(extra or {})})
                for n in names
            ]

        parts = each()
        ok = [p for p in parts if p.get("status") == "ok"]
        if not ok:
            return parts[0]
        if kind == "value" and not q.get("year") and (year := _common_year(ok)):
            asked = {**(q.get("asked") or {}), "latest": False}
            parts = each({"year": year, "asked": asked})
            ok = [p for p in parts if p.get("status") == "ok"]
            if not ok:
                return parts[0]
        whole = len(ok) == len(parts)
        if q.get("aggregate") == "sum" and kind == "value" and whole:
            if total := _sum_indicators(ok, q):
                return total
        result = _combine(ok, by="indicator")
        if (q.get("asked") or {}).get("birth_pair"):
            ok[0].setdefault("notes", []).insert(0, BIRTH_PAIR_NOTE)
        missed = [n for n, p in zip(names, parts, strict=False) if p.get("status") != "ok"]
        if missed:
            ok[0].setdefault("notes", []).append(
                josa(", ".join(missed), "은는") + " 이 조건으로 답할 수 없어 뺐습니다"
            )
        return result

    refusal = (
        "causal_question"
        if kind == "causal"
        else "prediction_question"
        if q.get("prediction")
        else None
    )
    if refusal:
        if not (q.get("dynamic_spec") or indicators.spec(ind)) or q.get("unsupported"):
            return _limit_result(refusal, None)
        kind = "trend" if regions else "rank"
        q = {**q, "prediction": False, "start_year": None, "end_year": None}
        q["year"] = None if kind == "trend" else q.get("year")

    if area := q.get("sub_area"):
        return {
            "status": "unknown_region",
            "region": area[0],
            "reason": f"'{area[0]}'처럼 동·생활권 단위 자료는 없습니다.",
            "alternative": area[1],
        }
    if bad := q.get("ungrounded_region"):
        return {
            "status": "unknown_region",
            "region": bad,
            "reason": "질문의 지역을 시도·시군구로 확인하지 못했습니다.",
        }
    if kind == "rank" and q.get("rank_by") in ("decrease", "increase"):
        return _rank_by_change(q, hi)
    if kind == "rank" and q.get("region_mean"):
        return _mean_of_regions(q)
    if q.get("cross"):
        if cond := q.get("unsupported_condition"):
            return {
                "status": "band_mismatch",
                "indicator": ind,
                "reason": f"'{cond}' 조건으로 나뉜 자료가 없어 그 값을 낼 수 없습니다.",
            }
        if q["cross"].get("dynamic"):
            return {
                "status": CROSS_DYNAMIC,
                "indicator": ind,
                "reason": f"{josa(ind, '은는')} 지표 사전에 없는 값이 섞여 있어 "
                "통계표에서 찾아 계산해야 합니다.",
            }
        result = cross_indicator.compute(q, compute)
        for r in _leaves(result):
            if r.get("series"):
                _add_yearly_change(r)
                _add_trend_facts(r)
            r["asked"] = {**(q.get("asked") or {}), "top": q.get("top")}
        return result
    dyn = q.get("dynamic_spec") if _dynamic_fits(q) else None
    sp = dyn or (None if q.get("unsupported") else indicators.spec(ind))
    if sp and sp.get("monthly_sources") and lookup.MONTHS.get() is not None:
        sp = {**sp, "sources": sp["monthly_sources"]}
    if dyn and (dyn.get("ages_in_filters") or dyn.get("ignore_ages")):
        ages = None
    population = sp["name"] == "총인구" if sp else not ind or bool(PEOPLE_INDICATOR.search(ind))
    if kind == "composition" and population and not q.get("unsupported"):
        if not regions:
            return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
        by = "sex" if q.get("by") == "sex" else "age"
        try:
            if (q.get("asked") or {}).get("latest"):
                year = indicators.latest_year(regions[:1])
            return indicators.composition(regions[0], year, by, sex)
        except StatsTableMissing as e:
            if e.tbl_id:
                raise
            return _calc_error(e)
        except Exception as e:
            return _calc_error(e)

    if sp is None:
        term = q.get("term") or ind or None
        return {
            "status": "unknown_indicator",
            "indicator": term,
            "supported": list(indicators.specs()),
            **lookup.data_gap(term),
        }

    if (cond := q.get("unsupported_condition")) and not q.get("dynamic_spec"):
        where = " ".join(regions) if regions else "전국"
        return {
            "status": "band_mismatch",
            "indicator": sp["name"],
            "reason": f"{sp['name']} 자료는 '{cond}' 조건으로 나뉘어 있지 않아 "
            "그 값을 낼 수 없습니다.",
            "alternative": f"대신 조건 없는 {where} {sp['name']}는 확인할 수 있습니다",
        }

    survey = (q.get("asked") or {}).get("survey")
    if survey and not dyn and (alt := indicators.alt_spec(sp, survey)):
        if sex in ("남", "여") and not any(s.get("by_sex") for s in alt["sources"]):
            return {
                "status": "unknown_indicator",
                "indicator": alt["name"],
                "reason": f"{survey} {alt['name']} 자료는 성별로 나뉘어 있지 않아 "
                "질문한 성별의 값을 낼 수 없습니다.",
            }
        sp = alt
        if not any(s.get("age_dim") for s in alt["sources"]):
            ages = None
        if kind not in ("value", "trend", "compare", "rank"):
            kind = "value"

    def calc(rs):
        if sp["method"] == "lookup":
            return _compute_lookup(q, sp, kind, rs, year, sex, ages)
        return _compute_population(q, sp["name"], kind, rs, year, sex, ages, hi)

    try:
        if kind == "trend" and len(regions) > 1 and q.get("aggregate") != "sum":
            result = _combine([calc([r]) for r in regions[:MAX_TREND_REGIONS]], by="region")
            if result.get("status") == "ok" and len(regions) > MAX_TREND_REGIONS:
                result["split"][0].setdefault("notes", []).append(
                    f"지역이 많아 앞의 {MAX_TREND_REGIONS}곳만 연도별로 봤습니다"
                )
        elif kind == "trend" and len(regions) > 1:
            result = _sum_region_periods([calc([r]) for r in regions], q)
        else:
            result = calc(regions)
    except StatsTableMissing as e:
        if e.tbl_id:
            raise
        return _calc_error(e)
    except Exception as e:
        return _calc_error(e)

    if dyn:
        result["method"] = "dynamic"
    if result.get("status") == "unknown_indicator" and "gap" not in result:
        result["gap"] = "not_collected"
    for r in _leaves(result):
        if r.get("series"):
            _add_yearly_change(r)
            _add_trend_facts(r)
        r["asked"] = {**(q.get("asked") or {}), "top": q.get("top")}
    if result.get("status") == "out_of_range":
        result.setdefault("asked_year", q.get("end_year") or year)
    term = q.get("term")
    if result.get("status") == "ok" and term and indicators.canonical_name(term) == sp["name"]:
        if note := basis_note(sp, term):
            for r in _leaves(result):
                r["notes"] = [note] + r.get("notes", [])
        elif indicators._norm(term) != indicators._norm(sp["name"]):
            for r in _leaves(result):
                note = f"{josa(repr_q(term), '을를')} {ro(sp['name'])} 보고 답했습니다"
                r["notes"] = [note] + r.get("notes", [])
    if result.get("status") == "ok" and (group := q.get("age_group")) and group.get("basis"):
        note = age_basis_note(group)
        for r in _leaves(result):
            if note not in r.get("notes", []):
                r["notes"] = [note] + r.get("notes", [])
    if result.get("status") == "ok" and (note := q.get("age_note")):
        for r in _leaves(result):
            if note not in r.get("notes", []):
                r["notes"] = [note] + r.get("notes", [])
    if (group := q.get("group")) and result.get("status") == "ok" and "values" in result:
        names = ", ".join(v["region"] for v in result["values"])
        scope = f"{_josa(group, '은는')} {names} 기준입니다."
        result["answer_note"] = " ".join(filter(None, [scope, result.get("answer_note")]))
    if refusal:
        return _limit_result(refusal, result)
    return result


def _rank_by_change(q, hi):
    year = q.get("year") or hi
    n = (q.get("asked") or {}).get("recent") or 0
    start = year - (n - 1 if n > 1 else 1)
    base = {**q, "rank_by": None, "top": None, "type": "rank", "start_year": None, "end_year": None}
    base["asked"] = {k: v for k, v in (q.get("asked") or {}).items() if k != "recent"}
    now, before = compute({**base, "year": year}), compute({**base, "year": start})
    for part in (now, before):
        if part.get("status") != "ok":
            return part
    prev = {r["region"]: r.get("value") for r in before.get("ranking") or []}
    rows = [
        {"region": r["region"], "value": r["value"] - prev[r["region"]]}
        for r in now.get("ranking") or []
        if r.get("value") is not None and prev.get(r["region"]) is not None
    ]
    decrease = q["rank_by"] == "decrease"
    rows.sort(key=lambda r: r["value"], reverse=not decrease)
    top = q.get("top") or 3
    out = {k: v for k, v in now.items() if k not in ("ranking", "notes")}
    out.update(
        ranking=[{**r, "rank": i} for i, r in enumerate(rows[:top], start=1)],
        total_regions=len(rows),
        rank_by=q["rank_by"],
        change_from=start,
        order="감소 폭이 큰 순" if decrease else "증가 폭이 큰 순",
        base_indicator=now.get("indicator"),
        indicator=f"{now.get('indicator')} 변화량",
        notes=[f"{start}년 → {year}년 변화량으로 순위를 매겼습니다", *(now.get("notes") or [])],
        kind="value",
    )
    return out


LIMIT_REASONS = {
    "causal_question": "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 답할 수 없습니다.",
    "prediction_question": "앞으로의 변화는 지금까지의 통계로 예측해 답할 수 없습니다.",
}


def _limit_result(status, alternative):
    out = {"status": status, "reason": LIMIT_REASONS[status]}
    alt = _source_result(alternative) if alternative else {}
    if not alternative or alternative.get("status") != "ok" or not alt.get("source"):
        out["guide"] = (
            "궁금한 지표와 지역·기간을 넣어 다시 질문해 주세요 (예: 최근 10년 서울 출생아 수)."
        )
        return out
    name = alt.get("indicator") or "그 지표"
    where = alt.get("region") or ("시도별" if "ranking" in alt else "")
    what = (
        f"{where} {name}의 변화" if "series" in alt or "split" in alternative else f"{where} {name}"
    )
    out["guide"] = f"대신 {_josa(what.strip(), '은는')} 아래 통계표에서 확인할 수 있습니다."
    if status == "prediction_question":
        out["guide"] += " 미래 연도를 넣어 물으면 장래인구추계가 있는 지표는 추계값으로 답합니다."
    out.update({k: alt.get(k) for k in ("indicator", "region", "source", "basis", "tbl_id", "url")})
    return out


def _dynamic_fits(q):
    spec = q.get("dynamic_spec")
    if not spec or spec.get("name") != q.get("indicator"):
        return False
    if (after := spec.get("future_after")) is not None:
        years = [y for y in (q.get("year"), q.get("start_year"), q.get("end_year")) if y]
        if not years or min(years) <= after:
            return False
    norm = {"sex": lambda v: v or "계", "ages": lambda v: [list(a) for a in v] if v else None}
    return all(
        norm.get(k, lambda v: v)(q.get(k)) == norm.get(k, lambda v: v)(v)
        for k, v in (spec.get("fixed") or {}).items()
    )


def _value_instead(result, name):
    if result.get("status") == "ok":
        note = f"{_josa(name, '은는')} 나눠 볼 구성 항목이 없어 구성비 대신 값으로 답했습니다"
        for r in _leaves(result):
            r["notes"] = [*(r.get("notes") or []), note]
    return result


def _asked_years(q):
    if n := (q.get("asked") or {}).get("recent"):
        return n
    spans = [r.get("n") for r in q.get("time_refs") or [] if r.get("kind") == "span"]
    return spans[0] if spans and spans[0] else None


def _with_this_year(ind, region, sex, ages, end):
    try:
        _, spec = indicators._get_indicator(ind, ages)
        if spec is None or not indicators._years_of(spec)[2]:
            return end
        return max(end, indicators.latest_year([region], sex))
    except Exception:
        return end


def _compute_population(q, ind, kind, regions, year, sex, ages, hi):
    if ages and len(ages) > 1:
        return _combine(
            [_compute_population(q, ind, kind, regions, year, sex, [a], hi) for a in ages]
        )
    if kind == "composition":
        result = _compute_population(q, ind, "value", regions, year, sex, ages, hi)
        return _value_instead(result, ind)
    if kind in ("value", "trend", "compare") and not regions:
        regions = ["전국"]
    if kind == "compare" and len(regions) == 1:
        y0, y1 = q.get("start_year"), q.get("end_year")
        kind = "trend" if y0 and y1 and y0 != y1 else "value"
    if (q.get("asked") or {}).get("latest") and kind in ("value", "compare", "rank"):
        year = indicators.latest_year([] if kind == "rank" else regions, sex)
    if q.get("born") and kind != "trend":
        point = {**q, "year": year, "end_year": None}
        ages = born_ages(point, year)
        q["age_group"] = point.get("age_group")
    if kind == "value" and len(regions) > 1 and q.get("aggregate") == "sum":
        return _sum_regions(indicators.compare(ind, regions, year, sex, ages), q)
    if kind == "value":
        return indicators.value(ind, regions[0], year, sex, ages)
    if kind == "trend":
        asked = q.get("asked") or {}
        start, end = q.get("start_year") or hi - 5, q.get("end_year") or hi
        open_end = not q.get("end_year")
        if asked.get("recent"):
            start, end, open_end = hi - asked["recent"] + 1, hi, True
        elif asked.get("these_days") and not q.get("start_year"):
            start = hi - 4
        elif asked.get("from_start") and not q.get("start_year"):
            start = indicators._data_years()[0]
        if q.get("born"):
            return _cohort_trend(
                q, ind, start, end, lambda y, a: indicators.value(ind, regions[0], y, sex, a)
            )
        if open_end:
            end = _with_this_year(ind, regions[0], sex, ages, end)
            if (n := _asked_years(q)) and end > hi:
                out = indicators.trend(ind, regions[0], end - n + 1, end, sex, ages)
                series = out.get("series") or []
                if out.get("status") == "ok" and series and series[-1]["year"] == end:
                    return out
                end = hi
        return indicators.trend(ind, regions[0], start, end, sex, ages)
    if kind == "compare":
        return indicators.compare(ind, regions, year, sex, ages)
    if kind == "rank":
        return indicators.rank(
            ind,
            year,
            q.get("level") or "시도",
            q.get("sido"),
            q.get("top"),
            bool(q.get("ascending")),
            sex,
            ages,
            q.get("target"),
        )
    return {"status": "parse_error", "reason": f"알 수 없는 질문 유형: {kind}"}


def _compute_lookup(q, sp, kind, regions, year, sex, ages):
    if kind == "composition" and not any(s.get("parts_dim") for s in sp["sources"]):
        result = _compute_lookup(q, sp, "value", regions, year, sex, ages)
        return _value_instead(result, sp["name"])
    if (q.get("asked") or {}).get("latest") and kind in LATEST_KINDS and not q.get("year"):
        last = _lookup_span(sp, regions[0] if regions else "전국", sex)[1]
        if last != 9999:
            year = last
    if q.get("born") and kind != "trend":
        point = {**q, "year": year, "end_year": None}
        ages = born_ages(point, year)
        q["age_group"] = point.get("age_group")
    if not regions and kind in ("value", "trend", "compare", "composition"):
        if not sp.get("national", True):
            return {
                "status": "missing_region",
                "indicator": sp["name"],
                "reason": f"어느 지역의 {sp['name']}인지 질문에 없습니다. "
                "전국 합계는 지역 간 이동이 서로 상쇄되어 의미가 없습니다.",
            }
        regions = ["전국"]
    if kind == "compare" and len(regions) == 1:
        y0, y1 = q.get("start_year"), q.get("end_year")
        kind = "trend" if y0 and y1 and y0 != y1 else "value"
    if not any(s.get("by_sex") for s in sp["sources"]):
        sex = "계"
    if ages and len(ages) > 1:
        parts = [_compute_lookup(q, sp, kind, regions, year, sex, [a]) for a in ages]
        return _combine(parts)
    if lookup.needs_split(sp, sex) and kind != "composition":
        parts = [_compute_lookup(q, sp, kind, regions, year, s, ages) for s in ("남", "여")]
        return _combine(parts)

    if kind == "composition":
        return lookup.composition(sp, regions[0], year, sex, ages, q.get("top"))
    if kind == "value" and q.get("aggregate") == "sum" and len(regions) > 1:
        return _sum_regions(lookup.compare(sp, regions, year, sex, ages), q)
    if kind == "value" and q.get("share_of"):
        return _share_of(lookup.value(sp, regions[0], year, sex, ages), sp, year, sex, ages)
    if kind == "value" and q.get("months") and len(regions) == 1:
        # 달 묶음 합: 월별 출처가 있는 지표는 월별 값을 더한다. 없으면 연간 값에 그 사실을 적는다
        m0, m1 = q["months"][:2]
        if (r := lookup.month_sum(sp, regions[0], year, m0, m1, sex)) is not None:
            if r.get("status") == "ok":
                r.setdefault("as_of", f"{year}년 {monthref.label(m0, m1)}")
            return r
        r = lookup.value(sp, regions[0], year, sex, ages)
        if r.get("status") == "ok":
            r.setdefault("notes", []).append(
                f"{josa(sp['name'], '은는')} 월별 자료가 없어 {year}년 연간 값으로 답했습니다"
            )
        return r
    if kind == "value":
        return lookup.value(sp, regions[0], year, sex, ages)
    if kind == "trend":
        return _lookup_trend(q, sp, regions[0], year, sex, ages)
    if kind == "compare":
        return lookup.compare(sp, regions, year, sex, ages)
    if kind == "rank":
        return lookup.rank(
            sp,
            year,
            q.get("level") or "시도",
            q.get("sido"),
            q.get("top"),
            bool(q.get("ascending")),
            sex,
            ages,
            q.get("target"),
        )
    return {"status": "parse_error", "reason": f"알 수 없는 질문 유형: {kind}"}


def _lookup_trend(q, sp, region, year, sex, ages):
    asked = q.get("asked") or {}
    lo, hi = _lookup_span(sp, region, sex)
    start, end = timeref.lookup_trend_years(q, lo, hi, year or hi)
    notes = []
    if end < lo:
        return {
            "status": "out_of_range",
            "indicator": sp["name"],
            "reason": f"{sp['name']} 자료는 {lo}~{hi}년만 있습니다.",
            "available_years": [lo, hi],
            "asked_year": q.get("start_year") or end,
        }
    if start < lo:
        notes.append(f"{sp['name']} 자료가 {lo}년부터 있어 {lo}년부터 봤습니다")
        start = lo
    said_end = q.get("end_year") or q.get("year")
    if said_end and said_end > hi and not asked.get("recent"):
        notes.append(f"{sp['name']} 자료가 {hi}년까지 있어 {hi}년까지 봤습니다")
    if q.get("born"):
        result = _cohort_trend(
            q, sp["name"], start, end, lambda y, a: lookup.value(sp, region, y, sex, a)
        )
    else:
        result = lookup.trend(sp, region, start, end, sex, ages)
    if result.get("status") == "ok":
        result["notes"] = result["notes"] + notes
        if q.get("aggregate") == "sum" and result.get("kind") == "count":
            return _sum_years(result)
    return result


def _cohort_trend(q, indicator, start, end, value):
    rows, notes, template = [], [], None
    for year in range(start, end + 1):
        bands = slots.birth_ages([tuple(b) for b in q["born"]["spans"]], year)
        if not bands:
            continue
        point = value(year, bands)
        if point.get("status") != "ok":
            return point
        template = point
        rows.append({"year": year, "value": point.get("value")})
        notes.extend(point.get("notes") or [])
    valid = [r for r in rows if r["value"] is not None]
    if not valid or template is None:
        return {"status": "missing_data", "reason": "해당 출생 집단의 기간 자료가 없습니다."}
    first, last = valid[0], valid[-1]
    out = {k: v for k, v in template.items() if k not in ("value", "year")}
    out.update(
        indicator=f"{q['born']['expr']} {indicator}", series=rows, notes=list(dict.fromkeys(notes))
    )
    out["formula"] = "각 조회 연도에서 출생연도를 뺀 만 나이의 자료"
    change = {
        "from_year": first["year"],
        "to_year": last["year"],
        "diff": round(last["value"] - first["value"], 3),
        "diff_unit": "%p" if out.get("kind") == "ratio" else out.get("unit", ""),
    }
    if out.get("kind") == "count" and first["value"]:
        change.update(rate=round((last["value"] / first["value"] - 1) * 100, 2), rate_unit="%")
    out["change"] = change
    return out


def _lookup_span(sp, region, sex):
    reg = indicators.resolve_region(region)
    level = lookup.REGION_LEVEL.get(reg.get("kind"), "시군구") if reg["status"] == "ok" else "시도"
    spans = []
    for s in sp["sources"]:
        if s.get("national_only") and level != "전국":
            continue
        if lookup.LEVELS[s["level"]] < lookup.LEVELS[level]:
            continue
        eff = lookup._effective(s, sex if sex != "계" or not s.get("by_sex") else "남")
        if eff and None not in (span := lookup._years(eff)):
            spans.append(span)
    if not spans:
        return 0, 9999
    return min(a for a, _ in spans), max(b for _, b in spans)


def _combine(parts, by=None):
    for p in parts:
        if p.get("status") != "ok":
            return p
    out = {
        **{k: parts[0][k] for k in ("source", "basis", "tbl_id", "url") if k in parts[0]},
        "status": "ok",
        "split": parts,
    }
    if by:
        out["split_by"] = by
    return out


def _common_year(parts):
    leaves = [r for p in parts for r in _leaves(p)]
    if len({(r.get("year"), r.get("as_of")) for r in leaves}) <= 1:
        return None
    years = [r["year"] for r in leaves if r.get("year")]
    if any(r.get("as_of") for r in leaves):
        years.append(indicators._data_years()[1])
    return min(years) if years else None


def _sum_indicators(parts, q):
    leaves = [_leaves(p) for p in parts]
    if any(len(ls) != 1 for ls in leaves):
        return None
    rows = [ls[0] for ls in leaves]
    if len({(r.get("year"), r.get("as_of")) for r in rows}) > 1:
        return None
    if len({r.get("unit") for r in rows}) > 1 or any(r.get("value") is None for r in rows):
        return None
    if any(r.get("kind", "count") != "count" and r.get("unit") not in ("명", "건") for r in rows):
        return None
    names = [r.get("indicator") for r in rows]
    notes = list(dict.fromkeys(n for r in rows for n in r.get("notes", [])))
    return {
        **{
            k: rows[0][k]
            for k in ("source", "basis", "tbl_id", "url", "unit", "region", "year")
            if k in rows[0]
        },
        "status": "ok",
        "kind": "count",
        "indicator": " + ".join(names),
        "value": sum(r["value"] for r in rows),
        "members": [{"region": r.get("indicator"), "value": r["value"]} for r in rows],
        "extra_sources": [
            {k: r.get(k) for k in ("source", "basis", "tbl_id", "url")}
            for r in rows[1:]
            if r.get("tbl_id") != rows[0].get("tbl_id")
        ],
        "notes": notes,
    }


def _leaves(result):
    if not result.get("split"):
        return [result]
    return [leaf for p in result["split"] for leaf in _leaves(p)]


def _sum_regions(result, q):
    if result.get("status") != "ok":
        return result
    if result.get("kind", "count") != "count" and result.get("unit") not in ("명", "건"):
        return result
    values = result["values"]
    if any(v["value"] is None for v in values):
        missing = ", ".join(v["region"] for v in values if v["value"] is None)
        return {**result, "status": "missing_data", "reason": f"{missing} 자료가 없습니다."}
    name = q.get("group") or " + ".join(v["region"] for v in values)
    drop = ("values", "highest", "lowest", "gap", "gap_unit")
    out = {k: v for k, v in result.items() if k not in drop}
    out.update(
        region=name,
        value=sum(v["value"] for v in values),
        members=values,
    )
    if q.get("group"):
        members = ", ".join(v["region"] for v in values)
        out["notes"] = [f"{name}: {members}의 합계"] + out.get("notes", [])
    return out


def _sum_region_periods(parts, q):
    for p in parts:
        if p.get("status") != "ok":
            return p
    summed = all("members" in p and "value" in p for p in parts)
    if not summed or len({p.get("year") for p in parts}) > 1:
        return _combine(parts, by="region")
    values = [{"region": p.get("region"), "value": p["value"]} for p in parts]
    return _sum_regions({**parts[0], "values": values}, q)


def _sum_years(result):
    series = result["series"]
    out = {k: v for k, v in result.items() if k not in ("series", "change")}
    out.update(
        year=f"{series[0]['year']}~{series[-1]['year']}",
        value=sum(s["value"] for s in series),
        members=[{"region": f"{s['year']}년", "value": s["value"]} for s in series],
    )
    out["notes"] = [f"{series[0]['year']}~{series[-1]['year']}년({len(series)}개년) 합계"] + out[
        "notes"
    ]
    return out


def _share_of(result, sp, year, sex, ages):
    if result.get("status") != "ok" or result.get("value") is None:
        return result
    total = lookup.value(sp, "전국", year, sex, ages)
    if total.get("status") == "ok" and total.get("value"):
        result["share"] = {
            "base": "전국",
            "base_value": total["value"],
            "value": round(analysis.share(result["value"], total["value"]), 2),
        }
    return result


def _add_yearly_change(result):
    kind = result.get("kind") or indicators.INDICATORS[result["indicator"]]["kind"]
    for prev, cur in zip(result["series"], result["series"][1:], strict=False):
        if prev["value"] is None or cur["value"] is None:
            continue
        diff = analysis.change(prev["value"], cur["value"])
        cur["diff_from_prev"] = int(diff) if kind == "count" else round(diff, 3)
        if kind == "count" and prev["value"]:
            cur["rate_from_prev"] = round(analysis.growth_rate(prev["value"], cur["value"]), 2)


def _add_trend_facts(result):
    series = [s for s in result["series"] if s["value"] is not None]
    if len(series) < 2:
        return
    c = result["change"]
    years = series[-1]["year"] - series[0]["year"]
    if years:
        c["per_year"] = round(c["diff"] / years, 3)
    first, last = series[0]["value"], series[-1]["value"]
    if "cagr" in c:
        pass
    elif years and "rate" in c and c.get("diff_unit") != "%p" and first > 0 and last > 0:
        c["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
    ordered = sorted(series, key=lambda s: s["value"])
    result["lowest_years"] = [{"year": s["year"], "value": s["value"]} for s in ordered[:5]]
    result["highest_years"] = [{"year": s["year"], "value": s["value"]} for s in ordered[::-1][:5]]
    result["runs"] = {}
    for name, test in (("positive", lambda x: x > 0), ("negative", lambda x: x < 0)):
        end = next((i for i in range(len(series) - 1, -1, -1) if test(series[i]["value"])), None)
        if end is None:
            continue
        start = end
        while start > 0 and test(series[start - 1]["value"]):
            start -= 1
        result["runs"][name] = {
            "since": series[start]["year"],
            "until": series[end]["year"],
            "years": end - start + 1,
        }
    v = [s["value"] for s in series]
    turns = [series[i]["year"] for i in range(2, len(v)) if v[i] > v[i - 1] <= v[i - 2]]
    if turns:
        result["rebound_year"] = turns[-1]


SEX_WORDS = {"남": "남성 ", "여": "여성 "}

GAP_TEXT = {
    "not_implemented": "수집한 통계표({tables})에 관련 자료가 있지만 아직 답하는 기능이 없습니다. ",
    "not_collected": "아직 수집하지 않은 통계입니다. ",
}

REFUSAL_GUIDE = {
    "parse_error": "",
    "not_supported_question": "아래 지원 지표로 지역·연도를 넣어 질문해 주세요.",
    "missing_region": "지역을 넣어 다시 질문해 주세요.",
    "unknown_region": "시도나 시군구 이름으로 다시 질문해 주세요.",
    "ambiguous_region": "아래 후보 중 하나로 다시 질문해 주세요.",
    "out_of_range": "수록 기간 안의 연도로 다시 질문해 주세요.",
    "region_not_in_year": "그 지역이 있던 연도로 다시 질문해 주세요.",
    "missing_data": "다른 연도나 지역으로 질문해 주세요.",
    "no_region_level": "자료가 있는 지역 단위로 다시 질문해 주세요.",
    "no_joint_table": "조건을 하나씩 나눠 질문해 주세요.",
    "data_loading": (
        "몇 분 뒤 다시 질문해 주세요. 그 전에는 아래 KOSIS 통계표에서 직접 볼 수 있습니다."
    ),
    "error": "",
}


_josa, _ro = josa, ro


def repr_q(word):
    return f"'{word}'"


def _val(v, unit, full=False):
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if " " not in unit:
        return f"{v:,}{unit}"
    if full and "당 " in unit:
        base, tail = unit.rsplit("당 ", 1)
        base = re.sub(r"\d{4,}", lambda m: f"{int(m[0]):,}", base)
        return f"{base}당 {v:,}{tail}"
    return f"{v:,}"


def _tail(unit):
    return unit.rsplit("당 ", 1)[1] if " " in unit and "당 " in unit else unit


def _meaning(name, v):
    meaning = indicators.INDICATORS.get(name, {}).get("meaning")
    if not meaning or v is None:
        return ""
    return f" {meaning.format(value=_val(v, ''))}이라는 뜻입니다."


SEX_IN_NAME = re.compile(r"남편|아내|남자|여자|남성|여성")


def _subject(r):
    name = r.get("indicator", "")
    if SEX_IN_NAME.search(name):
        return name
    return SEX_WORDS.get(r.get("sex"), "") + name


NEGATIVE_MEANING = {
    "자연증가 건수": "사망자가 출생아보다 {value}명 더 많았습니다.",
    "순이동": "전출이 전입보다 {value}명 더 많은 순유출입니다.",
}


def _when(r):
    return r.get("as_of") or f"{r['year']}년"


def _value_text(r):
    name, unit = _subject(r), r.get("unit", "")
    if r["value"] is None:
        return f"{_when(r)} {r['region']}의 {_josa(name, '은는')} 자료가 없습니다."
    meaning = _meaning(r["indicator"], r["value"])
    value = _val(r["value"], unit, full=not meaning)
    text = f"{_when(r)} {r['region']}의 {_josa(name, '은는')} {value}입니다." + meaning
    negative = NEGATIVE_MEANING.get(r["indicator"])
    if negative and r["value"] < 0:
        text += " " + negative.format(value=f"{-r['value']:,}")
    if share := r.get("share"):
        text += f" {share['base']}({_val(share['base_value'], unit)})의 {share['value']}%입니다."
    return text


def _projection_text(r):
    return (
        f"{r['year']}년 {r['region']}의 {r['indicator']} 장래인구추계({r['scenario']}) 값은 "
        f"{_val(r['value'], r.get('unit', ''))}입니다. 실제 통계가 아닌 추계값입니다."
    )


def _trend_text(r):
    name, unit, c = _subject(r), r.get("unit", ""), r["change"]
    first, last = r["series"][0]["value"], r["series"][-1]["value"]
    text = (
        f"{r['region']}의 {_josa(name, '은는')} {c['from_year']}년 {_val(first, unit, full=True)}, "
        f"{c['to_year']}년 {_val(last, _tail(unit))}입니다. "
    )
    if c["diff"] == 0:
        return text + f"{c['from_year']}년과 같습니다."
    direction = "증가" if c["diff"] > 0 else "감소"
    text += f"{c['from_year']}년 대비 {_val(abs(c['diff']), c['diff_unit'])} {direction}했습니다"
    if "rate" in c:
        text += f"(증감률 {c['rate']:+}{c['rate_unit']})"
    text += "."
    steps = [
        (
            s["year"],
            "증가" if s["diff_from_prev"] > 0 else "감소" if s["diff_from_prev"] < 0 else "같음",
        )
        for s in r["series"]
        if "diff_from_prev" in s
    ]
    if len(r["series"]) > 2:
        if {d for _, d in steps} == {direction}:
            text += f" 이 기간 동안 {_josa(name, '은는')} 매년 {direction}했습니다."
        elif turn := _turning_year(steps):
            year, before, after = turn
            text += f" {_josa(name, '은는')} {year}년까지 {before}하다가 이후 {after}했습니다."
    return text + _trend_answer(r)


def _turning_year(steps):
    dirs = [d for _, d in steps]
    if "같음" in dirs:
        return None
    changes = [i for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1]]
    if len(changes) != 1:
        return None
    i = changes[0]
    return steps[i - 1][0], dirs[0], dirs[i]


def _trend_answer(r):
    asked, unit = r.get("asked") or {}, r.get("unit", "")
    name = _subject(r)
    out = []
    if mode := asked.get("extreme"):
        rows = r["lowest_years" if mode == "min" else "highest_years"][: asked.get("top") or 1]
        word = "낮았던" if mode == "min" else "높았던"
        if rows and r.get("kind") == "count":
            word = "적었던" if mode == "min" else "많았던"
        listed = ", ".join(f"{x['year']}년({_val(x['value'], unit)})" for x in rows)
        span = f"{r['series'][0]['year']}~{r['series'][-1]['year']}년 중"
        out.append(f"{span} {_josa(name, '이가')} 가장 {word} 해는 {listed}입니다.")
    if sign := asked.get("streak"):
        words = {"positive": "순유입", "negative": "순유출"}
        if r.get("indicator") not in ("순이동",):
            words = {"positive": "양수", "negative": "음수"}
        run = (r.get("runs") or {}).get(sign)
        last = r["series"][-1]
        if run is None:
            out.append(f"이 기간에 {words[sign]}인 해가 없습니다.")
        else:
            out.append(
                f"{run['since']}년부터 {run['until']}년까지 "
                f"{run['years']}년 연속 {words[sign]}입니다."
            )
            if run["until"] != last["year"]:
                value = _ro(_val(last["value"], unit))
                out.append(f"{last['year']}년은 {value} 이어지지 않았습니다.")
    if asked.get("rebound"):
        if r.get("rebound_year"):
            out.append(
                f"감소하다가 다시 오르기 시작한 가장 최근 해는 {r['rebound_year']}년입니다"
                "(전년보다 증가한 첫해 기준)."
            )
        else:
            out.append("이 기간에 감소하다가 다시 오른 해는 없습니다.")
    if asked.get("per_year") and "per_year" in r["change"]:
        per = r["change"]["per_year"]
        word = "늘었습니다" if per > 0 else "줄었습니다"
        if unit == "세":
            word = "늦어졌습니다" if per > 0 else "빨라졌습니다"
        out.append(
            f"해마다 평균 {_val(round(abs(per), 2), r['change']['diff_unit'])}씩 {word}"
            "((마지막 값 − 처음 값) ÷ 햇수)."
        )
    return (" " + " ".join(out)) if out else ""


def _times(high, low, key="region"):
    hi, lo = high["value"], low["value"]
    if not hi or not lo or hi <= 0 or lo <= 0:
        return ""
    if high.get(key) == "전국" or high.get(key) in NATIONAL_NAMES:
        hi, lo = lo, hi
    return f", {round(hi / lo, 2):g}배"


def _compare_text(r):
    name, unit = _subject(r), r.get("unit", "")
    valid = [v for v in r["values"] if v["value"] is not None]
    if len(valid) < 2:
        return f"{_when(r)} {name} 값이 있는 지역이 둘 이상이 아니어서 비교할 수 없습니다."
    high, low = valid[0], valid[-1]
    ratio = r.get("ratio") if (r.get("asked") or {}).get("ratio") else None
    if ratio:
        times = ratio["value"]
        tail = f"{times}배입니다." if times >= 1 else f"{times}배로 더 적습니다."
        by_region = {v["region"]: v["value"] for v in r["values"]}
        a, b = ratio["a"], ratio["b"]
        return (
            f"{_when(r)} {a}의 {_josa(name, '은는')} {b}의 {tail}"
            f" ({a} {_val(by_region[a], unit)}, {b} {_val(by_region[b], unit)})"
        )
    if r["gap"] == 0:
        return (
            f"{_when(r)} {_josa(name, '은는')} {_josa(high['region'], '과와')} "
            f"{_josa(low['region'], '이가')} 같습니다."
        )
    gap = _val(r["gap"], r["gap_unit"])
    if len(valid) == 2:
        times = _times(high, low)
        return (
            f"{_when(r)} {_josa(name, '은는')} {_josa(high['region'], '이가')} "
            f"{low['region']}보다 {gap} 높습니다"
            f"({high['region']} {_val(high['value'], unit)}, "
            f"{low['region']} {_val(low['value'], unit)}{times})."
        )
    return (
        f"{_when(r)} {_josa(name, '이가')} 가장 높은 지역은 "
        f"{high['region']}({_val(high['value'], unit)}), 가장 낮은 지역은 "
        f"{low['region']}({_val(low['value'], unit)})이며, 차이는 {gap}입니다."
    )


def _rank_text(r):
    name, unit = _subject(r), r.get("unit", "")
    items = r["ranking"]
    if not items:
        return f"{_when(r)} {name} 순위를 매길 자료가 없습니다."
    order = "낮은" if r["order"] == "낮은 순" else "높은"
    if unit in ("명", "건"):
        order = {"낮은": "적은", "높은": "많은"}[order]
    head = f"{_when(r)} {_josa(name, '이가')} 가장 {order} {r['level']}"
    if r.get("rank_by") in ("decrease", "increase"):
        moved = "줄어든" if r["rank_by"] == "decrease" else "늘어난"
        who = _josa(r.get("base_indicator") or name, "이가")
        when = f"{r['change_from']}년 → {r['year']}년" if r.get("change_from") else _when(r)
        head = f"{when} {who} 가장 많이 {moved} {r['level']}"
    if r.get("indicator") == "순이동" and r["order"] == "낮은 순":
        head = f"{_when(r)} 인구가 가장 많이 빠져나간(순유출이 가장 큰) {r['level']}"
    listed = ", ".join(f"{x['region']}({_val(x['value'], unit)})" for x in items)
    if target := r.get("target"):
        if target["rank"] is None:
            return f"{_when(r)} {target['region']}의 {name} 값이 없어 순위를 매길 수 없습니다."
        return (
            f"{_when(r)} {target['region']}의 {_josa(name, '은는')} "
            f"{_ro(_val(target['value'], unit))} "
            f"{r['level']} {r['total_regions']}곳 중 {r['order'].replace(' 순', '')} 순으로 "
            f"{target['rank']}위입니다."
        )
    if 1 < len(items) < r["total_regions"]:
        return f"{head} {len(items)}곳은 {listed}입니다."
    top = items[0]
    text = f"{_josa(head, '은는')} {top['region']}({_val(top['value'], unit)})입니다."
    if len(items) == r["total_regions"] > 1:
        last = items[-1]
        other = {"낮은": "높은", "높은": "낮은", "적은": "많은", "많은": "적은"}[order]
        other = f"가장 {other} 곳은"
        if r.get("indicator") == "순이동":
            other = "가장 많이 들어온(순유입이 가장 큰) 곳은"
            if r["order"] != "낮은 순":
                other = "가장 많이 빠져나간(순유출이 가장 큰) 곳은"
        text += f" {other} {last['region']}({_val(last['value'], unit)})입니다."
    return text


def _composition_text(r):
    if r.get("subject"):
        listed = ", ".join(f"{p['label']} {p['share']}%" for p in r["parts"])
        biggest = max(r["parts"], key=lambda p: p["share"])
        unit = r.get("count_unit", "명")
        focus = [p for p in r["parts"] if p["label"] in (r.get("asked") or {}).get("focus", [])]
        lead = " ".join(
            f"{_when(r)} {r['region']}의 {r['subject']} 중 {_josa(p['label'], '은는')} "
            f"{p['count']:,}{unit}, {p['share']}%입니다."
            for p in focus
        )
        return (lead + " " if lead else "") + (
            f"{_when(r)} {r['region']}의 {r['subject']} 구성(전체 {r['total']:,}{unit})은 "
            f"{listed}입니다. 가장 큰 비중은 {biggest['label']}입니다."
        )
    by = "성별" if r["by"] == "sex" else "연령대별"
    whom = SEX_WORDS.get(r.get("sex"), "")
    listed = ", ".join(f"{p['label']} {p['share']}%" for p in r["parts"])
    biggest = max(r["parts"], key=lambda p: p["share"])
    return (
        f"{_when(r)} {r['region']} {whom}인구 {r['total']:,}명의 {by} 구성은 {listed}입니다. "
        f"가장 큰 비중은 {biggest['label']}입니다."
    )


def _refusal_text(r):
    status = r.get("status")
    if status == "unknown_indicator":
        gap = GAP_TEXT.get(r.get("gap"), "")
        if r.get("related"):
            gap = gap.format(tables=", ".join(r["related"]))
        if r.get("reason"):
            return f"{r['reason']} {gap}아래 지원 지표로 다시 질문해 주세요."
        what = josa(f"'{r['indicator']}'", "은는") if r.get("indicator") else "질문하신 지표는"
        return f"{what} 아직 지원하지 않는 지표입니다. {gap}아래 지원 지표로 다시 질문해 주세요."
    if status == "missing_year":
        return f"{r['reason']} {ask_followup.guide(r)}"
    guide = r.get("guide") or REFUSAL_GUIDE.get(status, "")
    if status == "band_mismatch" and r.get("alternative"):
        again = " (구간을 바꿔 다시 질문해 주세요)" if r.get("available_bands") else "."
        guide = r["alternative"] + again
    if status == "unknown_region" and r.get("alternative"):
        guide = f"속한 시군구({r['alternative']})로 다시 질문해 주세요."
    if status == "out_of_range" and r.get("available_years"):
        lo, hi = r["available_years"]
        asked = r.get("asked_year")
        near = lo if isinstance(asked, int) and asked < lo else hi
        guide = f"{lo}~{hi}년 사이 연도로 다시 질문해 주세요 (가장 가까운 {near}년 자료가 있음)."
    parts = [r.get("reason") or "답변할 수 없는 질문입니다.", guide]
    return " ".join(p for p in parts if p)


def _region_trend_end(r):
    ends = [(p.get("region"), p["series"][-1]) for p in r["split"] if p.get("series")]
    if len(ends) < 2 or len({s["year"] for _, s in ends}) != 1:
        return ""
    vals = [(region, s["value"]) for region, s in ends if s["value"] is not None]
    if len(vals) < 2:
        return ""
    first = r["split"][0]
    unit = first.get("unit", "")
    year = ends[0][1]["year"]
    hi, lo = max(vals, key=lambda x: x[1]), min(vals, key=lambda x: x[1])
    if hi[1] == lo[1]:
        return f" {year}년에는 {len(vals)}곳이 모두 같습니다."
    many = first.get("kind") == "count"
    if len(vals) == 2:
        gap = round(hi[1] - lo[1], 3)
        diff_unit = lookup.diff_unit({"unit": unit, "kind": first.get("kind")})
        word = "많습니다" if many else "높습니다"
        return (
            f" {year}년에는 {_josa(hi[0], '이가')} {lo[0]}보다 "
            f"{_val(gap, diff_unit, full=True)} {word}."
        )
    top, bottom = ("가장 많고", "가장 적습니다") if many else ("가장 높고", "가장 낮습니다")
    return (
        f" {year}년에는 {_josa(f'{hi[0]}({_val(hi[1], unit)})', '이가')} {top}, "
        f"{_josa(f'{lo[0]}({_val(lo[1], unit)})', '이가')} {bottom}."
    )


def _split_text(r):
    parts = r["split"]
    text = " ".join(summary(p) for p in parts)
    if r.get("split_by") == "region":
        return text + _region_trend_end(r)
    vals = [p.get("value") for p in parts]
    if r.get("split_by") == "indicator":
        return text
    if len(parts) == 2 and None not in vals and all("value" in p for p in parts):
        a, b = parts
        if vals[0] != vals[1]:
            hi, lo = (a, b) if vals[0] > vals[1] else (b, a)
            gap = round(abs(vals[0] - vals[1]), 3)
            unit = lookup.diff_unit({"unit": hi["unit"], "kind": hi.get("kind")})
            who = hi.get("month_label") if r.get("split_by") == "month" else hi["indicator"]
            word = "많습니다" if hi.get("kind", "count") == "count" else "높습니다"
            same = a.get("indicator") == b.get("indicator")
            if same and {a.get("sex"), b.get("sex")} == {"남", "여"}:
                who = {"남": "남성", "여": "여성"}[hi["sex"]]
            times = _times({"value": max(vals)}, {"value": min(vals)})
            times = f" ({times[2:]})" if times else ""
            text += f" {_josa(who, '이가')} {_val(gap, unit, full=True)} 더 {word}{times}."
    return text


def summary(r):
    text = _summary(r)
    if r.get("lead") and text:
        basis = f"{r['lead_basis']} " if r.get("lead_basis") else ""
        return f"{r['lead']}\n{basis}{text}"
    return text


def _summary(r):
    status = r.get("status")
    if status == "ok" and r.get("split"):
        text = _split_text(r)
        return text
    if status == "ok":
        if "scenario" in r:
            text = _projection_text(r)
        elif "series" in r:
            text = _trend_text(r)
        elif "values" in r:
            text = _compare_text(r)
        elif "ranking" in r:
            text = _rank_text(r)
        elif "parts" in r:
            text = _composition_text(r)
        else:
            text = _value_text(r)
        if r.get("answer_note"):
            text += " " + r["answer_note"]
        return text
    if status in ("out_of_range", "prediction_question") and (
        r.get("suggestion", {}).get("status") == "ok"
    ):
        return f"{r['reason']} 대신 장래인구추계 값을 보여드립니다. " + _projection_text(
            r["suggestion"]
        )
    return _refusal_text(r)


def _num(v, unit):
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}{unit}" if len(unit) <= 2 else f"{v:,} ({unit})"


def _signed(v, unit):
    return f"{v:+,}{unit}"


def _body(r):
    unit = r.get("unit", "")
    name = r.get("indicator", "")
    if "split" in r:
        return [line for p in r["split"] for line in _body(p)]
    if "members" in r:
        lines = [f"{r['region']} {name} ({r['year']}년)"]
        lines += [f"- {m['region']}: {_num(m['value'], unit)}" for m in r["members"]]
        lines.append(f"- 합계: {_num(r['value'], unit)}")
        return lines
    if "series" in r:
        lines = [f"{r['region']} {name} ({r['sex']})"]
        prev_year = None
        for s in r["series"]:
            line = f"- {s['year']}년: {_num(s['value'], unit)}"
            if "diff_from_prev" in s:
                ch = _signed(s["diff_from_prev"], r["change"]["diff_unit"])
                if "rate_from_prev" in s:
                    ch += f", {_signed(s['rate_from_prev'], '%')}"
                base = "전년" if prev_year in (None, s["year"] - 1) else f"{prev_year}년"
                line += f" ({base} 대비 {ch})"
            lines.append(line)
            prev_year = s["year"]
        c = r["change"]
        total = _signed(c["diff"], c["diff_unit"])
        if "rate" in c:
            total += f", {_signed(c['rate'], c['rate_unit'])}"
        if (cagr := c.get("cagr")) is not None:
            total += f", 연평균 {_signed(cagr, '%')}"
        if (rel := c.get("relative_rate")) is not None:
            total += f" (비율 자체는 {abs(rel):g}% {'커짐' if rel >= 0 else '작아짐'})"
        lines.append(f"- {c['from_year']}년 → {c['to_year']}년 전체 변화: {total}")
        return lines
    if "values" in r:
        lines = [f"{_when(r)} {name} ({r['sex']}, 높은 순)"]
        lines += [f"- {v['region']}: {_num(v['value'], unit)}" for v in r["values"]]
        if r.get("gap") is not None:
            lines.append(
                f"- 가장 높은 {r['highest']}와 가장 낮은 {r['lowest']}의 차이: "
                f"{_num(r['gap'], r['gap_unit'])}"
            )
        return lines
    if "ranking" in r:
        lines = [
            f"{_when(r)} {name} {r['level']} 순위 ({r['order']}, 전체 {r['total_regions']}곳 중)"
        ]
        lines += [f"{x['rank']}. {x['region']}: {_num(x['value'], unit)}" for x in r["ranking"]]
        return lines
    if "parts" in r:
        unit = r.get("count_unit", "명")
        title = r.get("subject") or "인구"
        lines = [
            f"{r['region']} {r['year']}년 {title} 구성 ({r['sex']}, 전체 {r['total']:,}{unit})"
        ]
        lines += [f"- {p['label']}: {p['count']:,}{unit} ({p['share']}%)" for p in r["parts"]]
        return lines
    if (ops := r.get("operands")) and all(o.get("value") is not None for o in ops):
        a, b = ops
        times = " × 100" if r.get("kind") == "ratio" else ""
        if (scale := r.get("scale")) and scale != 1:
            times = f" × {scale:,}"
        op = r.get("op") or "÷"
        return [
            f"- 계산: {a['label']} {a['value']:,}{a['unit']} {op} "
            f"{b['label']} {b['value']:,}{b['unit']}{times} = {_num(r['value'], unit)}"
        ]
    return []


def _source_result(result):
    if result.get("split"):
        first = _source_result(result["split"][0])
        notes = list(dict.fromkeys(n for p in _leaves(result) for n in p.get("notes") or []))
        extra = list(first.get("extra_sources") or [])
        if result.get("split_by") == "indicator":
            seen = {first.get("tbl_id"), *(e.get("tbl_id") for e in extra)}
            for part in result["split"][1:]:
                src = _source_result(part)
                for one in [src, *(src.get("extra_sources") or [])]:
                    if one.get("source") and one.get("tbl_id") not in seen:
                        seen.add(one.get("tbl_id"))
                        extra.append({k: one.get(k) for k in ("source", "basis", "tbl_id", "url")})
        return {**first, "notes": notes, "extra_sources": extra}
    suggestion = result.get("suggestion", {})
    if result.get("status") in ("out_of_range", "prediction_question") and (
        suggestion.get("status") == "ok"
    ):
        return suggestion
    return result


def render(result):
    status = result.get("status")
    src = _source_result(result)
    lines = []
    if status == "ok":
        lines = _body(result)
    elif status == "ambiguous_region":
        lines = [f"- {c}" for c in result.get("candidates", [])]
    elif status in ("unknown_indicator", "not_supported_question"):
        lines = ["지원 지표: " + ", ".join(result.get("supported", []))]

    notes = list(src.get("notes") or [])
    notes = [n for n in notes if not any(n != m and n in m for m in notes)]
    defs = {sp.get("definition") for sp in indicators.specs().values()}
    notes.sort(key=lambda n: not any(d and d in n for d in defs))
    if src.get("formula"):
        notes.insert(0, f"계산식: {src['formula']}")
    lines += [f"※ {n}" for n in notes]
    if src.get("source"):
        lines.append(f"출처: {src['source']} ({src['basis']})")
    if src.get("url"):
        lines.append(f"통계표: {src['url']}")
    for extra in src.get("extra_sources") or []:
        lines.append(f"출처: {extra['source']} ({extra['basis']})")
        lines.append(f"통계표: {extra['url']}")
    return "\n".join(lines)


def sources(result):
    src = _source_result(result)
    if not src.get("source"):
        return []
    return [
        {
            "name": s["source"],
            "basis": s["basis"],
            "tbl_id": s.get("tbl_id"),
            "url": s.get("url"),
        }
        for s in [src, *(src.get("extra_sources") or [])]
    ]


RECENT_WORDS = re.compile(r"최근|최신|현재|요즘|요새|지금|올해|금년")


async def _refresh_recent(question, parsed, trace):
    if not RECENT_WORDS.search(question):
        return []
    sp = indicators.spec(parsed.get("indicator") or "")
    if sp is None or sp["method"] != "lookup":
        return []
    notes, steps = [], trace.setdefault("refreshed", [])
    for tbl_id in dict.fromkeys(s["table"] for s in sp["sources"] if not s.get("years")):
        try:
            info = await load_queue.table_info(tbl_id)
        except (load_queue.UnknownTable, SQLAlchemyError):
            continue
        step = {"tbl_id": tbl_id}
        outcome = await table_cache.refresh_if_stale(tbl_id, info["org_id"], step)
        step["refresh"] = outcome
        steps.append(step)
        if outcome in ("too_big", "failed", "timeout", "disabled", "busy") and step.get(
            "refresh_kosis_year"
        ):
            try:
                await load_queue.enqueue(tbl_id, "update")
            except SQLAlchemyError:
                continue
            notes.append(
                f"KOSIS에 {step['refresh_kosis_year']}년 자료가 새로 있어 받고 있습니다. "
                f"지금은 {step['refresh_db_year']}년까지 자료로 답합니다"
            )
    return notes


MAX_FETCH = 3


async def _compute_fetching(parsed, trace):
    for _ in range(MAX_FETCH + 1):
        try:
            return await run_in_threadpool(compute, parsed)
        except StatsTableMissing as e:
            error = e
        fetched = trace.setdefault("fetched", [])
        if len(fetched) >= MAX_FETCH or not error.tbl_id:
            break
        if any(f["tbl_id"] == error.tbl_id for f in fetched):
            break
        try:
            info = await load_queue.table_info(error.tbl_id)
        except (load_queue.UnknownTable, SQLAlchemyError):
            break
        step = {"tbl_id": error.tbl_id}
        outcome = await table_cache.fetch_now(error.tbl_id, info["org_id"], step)
        fetched.append(step)
        if outcome != "loaded":
            break
    return await _loading_result(error)


async def _loading_result(error):
    if not error.tbl_id:
        raise error
    try:
        info = await load_queue.table_info(error.tbl_id)
        job = await load_queue.enqueue(error.tbl_id, "question")
    except (load_queue.UnknownTable, SQLAlchemyError):
        raise error from None
    return {
        "status": "data_loading",
        "reason": (
            f"이 질문에 필요한 통계표({info['name']})는 처음 요청되어 데이터를 준비하고 있습니다."
        ),
        "tbl_id": error.tbl_id,
        "source": info["source"],
        "basis": "KOSIS 국가통계포털",
        "url": info["url"],
        "job": {"id": job["id"], "status": job["status"]},
    }


async def ask(question, adjust=None):
    trace = {}
    start = time.perf_counter()
    parsed = await understand(question, trace)
    trace["understand_ms"] = round((time.perf_counter() - start) * 1000)
    if adjust is not None:
        parsed = adjust(parsed)
    return await answer(parsed, trace, question)


def _add_data_end_note(parsed, result, today=None):
    if result.get("status") != "ok":
        return
    today = today or date.today()
    refs = [r for r in parsed.get("time_refs") or [] if r.get("kind") in PERIOD_KINDS]
    recent = _asked_years(parsed)
    kinds = {r["kind"] for r in refs}
    if not recent and not kinds - {"span_from"}:
        return
    parts = _leaves(result) if result.get("split") else [result]
    years = [
        (p["series"][0]["year"], p["series"][-1]["year"], len(p["series"]))
        for p in parts
        if p.get("series")
    ]
    if not years:
        return
    first, last = min(y[0] for y in years), max(y[1] for y in years)
    count = max(y[2] for y in years)
    if last >= today.year:
        return
    if recent and count < recent:
        note = f"자료가 {first}~{last}년만 있어 {count}개 연도로 계산했습니다"
    elif recent:
        note = f"가장 최근 자료가 {last}년까지라 {first}~{last}년({recent}개 연도)으로 계산했습니다"
    elif "compare_ago" in kinds:
        note = f"가장 최근 자료가 {last}년까지라 {first}년과 {last}년을 비교했습니다"
    else:
        note = f"가장 최근 자료가 {last}년까지라 {first}~{last}년으로 계산했습니다"
    holder = parts[0]
    holder["notes"] = [*(holder.get("notes") or []), note]


async def answer(parsed, trace=None, question=""):
    trace = {} if trace is None else trace
    start = time.perf_counter()
    if ask_followup.needs_year(question) and indicators.spec(parsed.get("indicator") or ""):
        result = await run_in_threadpool(ask_followup.missing_year, parsed)
    else:
        notes = await _refresh_recent(question, parsed, trace)
        result = await _compute_fetching(parsed, trace)
        if notes and result.get("status") == "ok":
            result["notes"] = list(result.get("notes") or []) + notes
        _add_data_end_note(parsed, result)
    trace["compute_ms"] = round((time.perf_counter() - start) * 1000)
    if not parsed.get("dynamic_spec"):
        flow.add("사전 계산", result.get("status"), flow_result(result))
        from app.core.config import get_settings

        months = parsed.get("months") or parsed.get("months_each")
        if months and not any(r.get("value") is not None for r in _leaves(result)):
            parsed["grounded"] = False
            parsed["gate_reasons"] = ["월을 물었는데 사전 표에 월별 값이 없음"]
            flow.add("사전 문지기", "넘김", parsed["gate_reasons"][0])
        settings = get_settings()
        gate = settings.binding_dict_gate and parsed.get("grounded") is not False
        if (gate or settings.binding_dict_shadow) and result.get("status") == "ok" and question:
            from app.services import dict_binding

            if (rec := await dict_binding.shadow(question, parsed, result)) is not None:
                trace["dict_binding"] = rec
                if gate and rec.get("decision") == dict_binding.HAND_OFF:
                    parsed["grounded"] = False
                    parsed["gate_reasons"] = rec.get("reasons") or []
                    flow.add("사전 문지기", "넘김", " · ".join(parsed["gate_reasons"])[:200])
    return {
        "status": result.get("status"),
        "answer": f"{summary(result)}\n\n{render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": sources(result),
        "trace": trace,
    }


def flow_result(result):
    tbl = ",".join(s.get("tbl_id") or "" for s in sources(result))[:60]
    if result.get("status") == "ok":
        value = result.get("value")
        shown = f"값={value}{result.get('unit') or ''}" if value is not None else "값 여러 개"
        return f"{result.get('indicator') or ''} {shown} 표={tbl or '-'}".strip()
    return str(result.get("reason") or result.get("indicator") or "")


DATA_GAP = {
    "unknown_indicator",
    "out_of_range",
    "region_not_in_year",
    "missing_data",
    "no_region_level",
    "data_loading",
}
REQUESTS_DIR = Path(__file__).resolve().parents[2] / "data" / "requests"
MISSING_LOG = REQUESTS_DIR / "missing.jsonl"
REQUEST_LOG = REQUESTS_DIR / "requests.jsonl"


def _append(path, record):
    from app.services.file_logs import append

    append(path, record)


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def log_missing(question, res, path=None):
    if res.get("status") not in DATA_GAP:
        return False
    result = res.get("result") or {}
    record = {
        "at": _now(),
        "question": question,
        "status": res["status"],
        "parsed": res.get("parsed"),
        "gap": result.get("gap"),
        "related": result.get("related"),
    }
    _append(path or MISSING_LOG, record)
    return True


def log_request(question, res=None, error=None, elapsed_ms=None, path=None, user_id=None):
    res = res or {}
    trace = res.get("trace") or {}
    record = {
        "at": _now(),
        "user_id": user_id,
        "question": question,
        "status": res.get("status"),
        "error": error,
        "elapsed_ms": elapsed_ms,
        "understand_ms": trace.get("understand_ms"),
        "compute_ms": trace.get("compute_ms"),
        "hcx_raw": trace.get("hcx_raw"),
        "parsed": res.get("parsed"),
        "result": res.get("result"),
    }
    if trace.get("fetched"):
        record["fetched"] = trace["fetched"]
    if trace.get("understand_cached"):
        record["understand_cached"] = True
    if trace.get("flow"):
        record["flow"] = trace["flow"]
    if trace.get("llm_calls"):
        record["llm_calls"] = trace["llm_calls"]
    if trace.get("plan_cache"):
        record["plan_cache"] = trace["plan_cache"]
    if trace.get("dynamic"):
        record["dynamic"] = trace["dynamic"]
    if trace.get("refreshed"):
        record["refreshed"] = trace["refreshed"]
    if "understand_usage" in trace:
        record["understand_usage"] = trace["understand_usage"]
        record["understand_max_completion_tokens"] = trace.get("understand_max_completion_tokens")
    _append(path or REQUEST_LOG, record)
