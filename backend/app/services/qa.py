"""질문 → 답변 (하위 태스크 1~5 연결)

  1. 질문 이해        LLM이 질문을 JSON(질문 유형, 지표, 지역, 연도)으로 바꾼다
  2~4. 판단·조회·계산  코드(indicators.py)가 계산하고, status로 답변 가능 여부를 알려준다
  5. 답변 생성        코드가 질문 유형별 템플릿에 계산 결과를 채워 문장을 만들고 (summary),
                      숫자 목록·주의사항·출처를 붙인다 (render)

LLM은 1단계(질문 해석)에만 쓴다. 답변 문장은 템플릿이라 숫자·지표 정의가 틀릴 수 없고,
질문 하나에 HCX를 한 번만 부른다.
답변할 수 없는 질문은 status(답변 불가 사유)별로 정해진 안내 문장을 쓴다.

LLM을 부르는 함수(understand, ask)는 async, 나머지는 DB·계산만 하는 일반 함수다.

API: POST /ask
실행: python -m scripts.ask --question "2025년 서울과 부산 고령인구 비율 비교"
      python -m scripts.ask --question "..." --debug   (중간 결과 JSON도 출력)
"""

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
# 질문 해석 응답 최대 길이. HCX 한도(분당 60,000토큰)는 요청마다 '보낸 글 + 이 값'을 미리 잡는다.
# 예전 값 5,120(+ 추론 low)은 이전 프로젝트의 범용 chat() 기본값이 그대로 따라온 것으로,
# 질문마다 약 7,000토큰을 잡아 분당 약 8질문이면 한도가 찼다. 추론까지 넣은 실제 응답도
# 평균 약 600 · 최대 약 1,100토큰이었고 (개발셋, 11/18), 추론을 끄면 짧은 JSON만 나온다
UNDERSTAND_MAX_TOKENS = 1024


@llm_calls.staged("understand")
async def _chat(prompt, system, temperature=0.1, trace=None):
    """HCX-007 호출 (질문 해석). 짧은 JSON만 받으므로 추론을 끄고 응답 길이를 줄인다
    (이어 묻는 말의 변경 추출 ask_session._chat과 같은 방식)"""
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


# 질문 단위 캐시 (11/18): 글자까지 같은 질문이면 질문 이해(HCX)를 다시 부르지 않는다.
# '작년' · '최근 5년'은 오늘 날짜와 자료의 마지막 연도로 풀리므로 열쇠에 둘 다 넣는다.
# 서버 메모리라 재시작하면 비고, 1시간 지나면 지운다. 평가처럼 질문 이해를 고친 효과를 잴 때는
# QUESTION_CACHE=false (scripts.eval_devset은 기본으로 끈다)
UNDERSTAND_CACHE = MemoryCache("understand", maxsize=1024, ttl=3600)


# 열쇠에서 빼는 끝 문장부호 ("서울 인구 알려줘?" · "서울 인구 알려줘."는 같은 질문)
CACHE_TAIL = re.compile(r"[\s?？!！.。~]+$")


def _cache_key(question, latest):
    text = CACHE_TAIL.sub("", re.sub(r"\s+", " ", question).strip())
    return (text, date.today().isoformat(), latest)


async def understand(question, trace=None):
    """1단계: 질문 → dict. JSON을 못 읽으면 type이 parse_error
    trace(dict)를 주면 HCX 원본 출력을 담는다 (요청 로그용)"""
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
        # 연도 칸에 계산식("start_year": 2025 - 8)을 쓰면 JSON이 깨져 안쪽 객체를 질문 해석으로
        # 읽었다 (11/21). 상대 연도는 코드 규칙표(_apply_time)가 다시 세므로 계산식은 null로 읽고,
        # 질문 해석은 type이 있는 객체만 쓴다
        objects = [o for o in _json_objects(YEAR_ARITH.sub(": null", text)) if "type" in o]
        if not objects:
            flow.add("질문 이해", "parse_error", "HCX 출력에서 JSON을 읽지 못함")
            return {"type": "parse_error", "raw": text}  # 해석 실패는 남기지 않는다
        parsed = objects[0]
        # 남녀를 나눠 JSON을 두 번 쓴 경우 → 성별 구분 없이 (남녀를 함께 보여준다)
        if len({json.dumps(o.get("sex"), ensure_ascii=False) for o in objects}) > 1:
            parsed["sex"] = "계"
        llm = "HCX"
    result = await run_in_threadpool(correct, question, parsed)
    flow.add("질문 이해", llm, _understood(result))
    if use_cache:
        # 대화 이어받기(ask_session의 adjust)는 꺼낸 뒤에 적용되므로 이어받기 전 결과를 남긴다
        UNDERSTAND_CACHE.set(key, {"raw": text, "parsed": copy.deepcopy(result)})
    return result


MERGED_MAX_TOKENS = 512  # 합친 출력은 140토큰 안팎 (11/21 개발셋 v2 · v1 최대 186)


async def _understand_merged(question, latest, trace=None):
    """UNDERSTAND_MERGED=gemma: 질문 이해 + 조건 추출을 Gemma 한 번으로 → (원문, 질문 이해 출력).
    조건 추출 몫은 slots.correct 까지 거쳐 이 요청에 보관한다 (slots.extract 가 꺼내 쓴다).
    Gemma 가 없거나(넘어가 있는 중 포함) 실패 · JSON 을 못 읽으면 None → 지금 방식(HCX)으로"""
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
                # JSON 뒤에 설명과 '수정된 JSON'을 이어 써서 20초 걸린 응답이 있었다 (11/21 D-049)
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
        except Exception:  # 조건 추출 몫이 없으면 통계표 찾기가 따로 부른다
            logger.exception("합친 질문 이해의 조건 추출 몫 정리 실패")
    return text, numeric


def _understood(parsed):
    """처리 흐름에 남길 질문 이해 결과 (코드가 고친 항목까지)"""
    fixes = parsed.get("corrections") or []
    tail = f" · 코드 보정: {'; '.join(map(str, fixes[:3]))}" if fixes else ""
    return flow.summary_of(parsed) + tail


def _json_objects(text):
    """HCX 출력 속 JSON 객체들 (코드 블록으로 감싸거나 설명 문장이 붙어 와도 읽는다)"""
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
    """프롬프트에 넣을 지원 지표 목록: 이름(단위: 설명)"""
    out = []
    for name, sp in indicators.specs().items():
        desc = sp.get("desc")
        out.append(f"{name}({sp['unit']}{': ' + desc if desc else ''})")
    return "; ".join(out)


# ---- 질문 해석 보정: LLM 해석을 질문 원문과 대조해 바로잡는다 ------------------------------

GENDER_WORDS = {"여": re.compile(r"여성|여자"), "남": re.compile(r"남성|남자")}
# 남녀를 함께 묻는 표현 (구성비 질문이면 성별 구성)
SEX_SPLIT = re.compile(r"남녀|성별|(남성|남자).*(여성|여자)|(여성|여자).*(남성|남자)")
# 남녀를 나누지 않고 합계를 물음 ("남녀 합계 인구", "성별 구분 없이")
SEX_TOTAL = re.compile(
    r"(?:남녀|성별|(?:남성|남자).*(?:여성|여자)|(?:여성|여자).*(?:남성|남자))"
    r"\s*(?:인구\s*)?(?:합계|전체|합산|합쳐|합치|합한|모두|통틀어)"
    r"|성별\s*(?:구분\s*없이|상관\s*없이)"
)
# 낱말 자체에 성별이 든 말: 성별 말(GENDER_WORDS)이 없어도 HCX가 읽은 성별을 지우지 않는다
# ("남편 평균 초혼연령" → 남, "미혼모" → 여)
SEX_IMPLIED = re.compile(
    r"남편|신랑|아버지|아빠|부친|아들|(?<!청)소년|할아버지|남아"
    r"|아내|부인|와이프|신부|어머니|엄마|모친|딸|소녀|할머니|여아|미혼모|산모|임산부|임신부"
)
# 상대시점("작년", "2년 전")은 app.services.timeref 가 검색과 같은 규칙으로 푼다
NO_REGION_TYPES = {"rank", "other", "parse_error"}

# 연령 구간: "20대", "6~11세", "65세 이상", "15세 미만" (뒤에 '광역시'가 오는 "5대 광역시"는 제외)
# "세" 대신 "살"도 쓴다 ("19~34살", "100살 이상", "15살 안 된")
AGE_RANGE = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)?\s*[~\-]\s*(\d+)\s*(?:세|살)")
AGE_OVER = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)\s*이상")
AGE_UNDER = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)\s*(?:미만|도?\s*안\s*(?:된|되는))")
AGE_UNDER_EQ = re.compile(r"(?:만\s*)?(\d+)\s*(?:세|살)\s*이하")
# 말로 이은 구간: "62살에서 67살 사이", "33세부터 38세까지", "62세와 67세 사이"
# (끝 나이는 단위 필수: "2020년부터 2025년까지"는 연령이 아니다)
AGE_SPAN = re.compile(
    r"(?:만\s*)?(\d{1,3})\s*(?:세|살)?\s*"
    r"(?:(?:에서|부터)\s*(?:만\s*)?(\d{1,3})\s*(?:세|살)|(?:와|과|하고)\s*(?:만\s*)?(\d{1,3})\s*(?:세|살)\s*사이)"
)
# "65세 이상 70세 미만", "65살 이상 69살 이하"
AGE_OVER_UNDER = re.compile(
    r"(?:만\s*)?(\d{1,3})\s*(?:세|살)\s*이상\s*(?:만\s*)?(\d{1,3})\s*(?:세|살)\s*(미만|이하)"
)
# "60대 이상", "20~30대", "20대 후반", "20대부터 30대까지"
AGE_DECADE_SPAN = re.compile(r"(\d+)\s*대\s*(?:에서|부터)\s*(\d+)\s*대(?!\s*(광역시|도시))")
AGE_DECADE_OVER = re.compile(r"(\d+)\s*대\s*이상")
AGE_DECADE_RANGE = re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*대(?!\s*(광역시|도시))")
AGE_DECADE_PART = re.compile(r"(\d+)\s*대\s*(초반|중반|후반)")
DECADE_PART = {"초반": (0, 3), "중반": (4, 6), "후반": (7, 9)}
AGE_DECADE = re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))")
# 여러 지역 묶음
# "충청도"는 묶음이 아니라 충북·충남 중 어디인지 되묻는다 (indicators.AMBIGUOUS_NAMES)
REGION_GROUPS = {
    # 위에서부터 차례로 보고 질문에 처음 들어 있는 묶음을 쓴다.
    # "비수도권"은 "수도권"을 품고 있으므로 수도권보다 먼저 둔다
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
    # "지방"만으로는 넣지 않는다 ("지방소멸", "지방자치단체")
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
    # 서울 강남권 구 묶음 (질문 글자에 구 이름이 없어 지역 확인에서 막혔다)
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
    # 권역을 "~지역", 짧은 이름으로 말한 것 (2026-11-20)
    "영남": ["부산", "대구", "울산", "경북", "경남"],
    "호남": ["광주광역시", "전북", "전남"],
    "충청 지역": ["대전", "세종", "충북", "충남"],
    "충청지역": ["대전", "세종", "충북", "충남"],
    # 서울 생활 표현의 구 묶음 (2026-11-20)
    "마용성": ["서울 마포구", "서울 용산구", "서울 성동구"],
    "노도강": ["서울 노원구", "서울 도봉구", "서울 강북구"],
    "금관구": ["서울 금천구", "서울 관악구", "서울 구로구"],
    # 인천을 뺀 광역시 (2026-11-20)
    "지방 광역시": ["부산", "대구", "광주광역시", "대전", "울산"],
    "지방광역시": ["부산", "대구", "광주광역시", "대전", "울산"],
    # 인구 100만 이상 특례시 (화성은 2025년부터)
    "특례시": ["경기 수원시", "경기 고양시", "경기 용인시", "경남 창원시(통합)", "경기 화성시"],
    "6대 광역시": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "5대 광역시": ["부산", "대구", "인천", "광주광역시", "대전"],
    # "광역시 전체"의 "전체"를 전국으로 읽지 않게 묶음으로 먼저 잡는다
    "광역시 전체": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "모든 광역시": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "광역시 모두": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    # "도 지역"은 넣지 않는다 ("시도 지역별"에 들어 있음). "모든 도"도 "모든 도시"에 걸림
    "9개 도": ["경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"],
    "9개도": ["경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"],
}
# "합계출산율"의 합계는 합산이 아니다.
# "다 해서"·"전부 몇"·"통틀어"·"누적"도 합계 (2026-11-13 결함 1·2: 이 표현을 놓치면 첫 지역만,
# 또는 추이로 답했다). "전부"·"모두"는 뒤에 몇·합·더가 올 때만 ("시도 전부 보여줘"는 목록).
# "다"는 혼자 쓰인 "다"만 ("서울보다 합계출산율"의 "보다"는 아니다), 그 뒤의 "합"도
# "합계출산율"이면 아니다 ("두 지역 모두 합계출산율이 낮아?"는 비교)
_HAP = r"합(?!계\s*출[산생])"
SUM_WORDS = re.compile(
    r"합계(?!\s*출[산생])|합친|합쳐|합하면|합치면|합산|총\s*몇|더하면|총합|총\s*인원"
    rf"|(?:모두|전부)\s*(?:몇|더|해서|{_HAP})|(?<![가-힣])다\s*(?:해서|더해|{_HAP})|통틀|누적"
)
COUNT_ASK = re.compile(r"몇\s*(명|건|가구|곳|개)|인구는|인구가|인구\s*수|수는|수가|건수")
PCT_ASK = re.compile(r"%|퍼센트|비율|비중|몇\s*프로")
COMPOSITION_WORDS = re.compile(r"구성|비중|비율|%|퍼센트|나누면|몇\s*프로")
BY_REGION = re.compile(r"(구|시군구|시도|지역|시)\s*(별|마다)|\d+\s*개\s*(구|시군구)\s*중")
# "가장 높은 시도랑 가장 낮은 시도" 처럼 양 끝을 함께 물음
BOTH_ENDS = re.compile(
    r"(높|많)\S*\s*\S*\s*(랑|와|과|하고)\s*.*(낮|적)|(낮|적)\S*\s*\S*\s*(랑|와|과|하고)\s*.*(높|많)"
)
NATIONAL_NAMES = {"우리나라", "한국", "국내", "대한민국"}
# 시군구보다 작은 생활권·신도시 이름 → 속한 시군구 (자료가 없어 시군구로 다시 묻도록 안내)
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
    # 2026-11-20 추가
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
# 생활권 이름 바로 뒤에 이 말이 오면 생활권이 아니다 (일반 낱말과 겹침)
SUB_AREA_NOT = {
    "일산": ("화",),  # 일산화탄소
    "미사": ("일",),  # 미사일
    "다산": ("가정", "가구", "모", "장려", "정책", "여성"),  # 다산 가정 = 자녀가 많은 가정
    "서면": ("으로", "신청", "동의", "제출", "조사"),  # 서면으로 = 문서로
    "중동": ("출신", "국가", "국적", "사람", "전쟁", "지역"),  # 중동 = 서아시아
}


def find_sub_area(question):
    """질문 속 생활권 이름 → (이름, 속한 시군구). 없으면 None
    뒤에 구·시·군이 붙으면 행정구역 이름이다 (예: 해운대구, 분당구).
    앞에 숫자가 붙으면 생활권이 아니다 (예: "1분당").
    """
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
LEVEL_CITY = re.compile(r"전국\s*(의\s*)?시\s*(중|가운데)|시\s*단위")
ASCENDING = re.compile(r"꼴찌|최하위|가장\s*(낮|적)|제일\s*(낮|적)")
# 높은 순으로 물음 ("초혼연령 높은 시도 3곳", "인구 많은 순서로", "상위 3곳"). 질문 해석이 추론 없이
# "높은"을 낮은 순(ascending)으로 읽은 경우를 되돌린다 (변형셋 v1 P-081, 11/18)
DESCENDING = re.compile(r"(높|많)(은|게|을)|상위|1등|1위|top", re.I)
# 감소·유출 순위는 값이 음수라 "큰" 쪽이 낮은 순이다 ("감소 폭 큰 시도") → 고치지 않는다
DECREASE = re.compile(r"낮|적은|적게|감소|줄|유출|빠져")
TOP_N = re.compile(r"(상위|하위)\s*(\d+)|(\d+)\s*(곳|개|위까지)(?!\s*\S*\s*중)")
# "최근 5년", "과거 10년간", "지난 3년 동안" (timeref와 같은 표현)
RECENT = timeref.RECENT_N_YEARS
UNIT_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")  # 지표 이름 뒤의 "(명)"·"(%)"
LATEST = re.compile(r"최근|최신|현재|요즘|요새|지금")
# 기간 변화를 묻는 표현 ("최근 부산 인구 추이"는 추이, "최근 부산 인구"는 최신 값)
THESE_DAYS = re.compile(r"요즘|요새")
LATEST_KINDS = ("value", "compare", "rank")  # "최근"이면 마지막 해로 답하는 질문 유형
CHANGE_ASK = re.compile(r"추이|변화|변동|흐름|증감|추세|늘|줄|증가|감소|동안|사이|년간|그래프")
# 두 연도 사이: "2020년부터 2025년", "2020년 대비 2023년", 조사만 다른 "2020년이랑 2023년" ·
# "2020년과 2023년"도 같다 (11/22: 이 모양은 HCX가 옮겨 적은 연도를 검증 없이 썼다)
SPAN = re.compile(
    r"((?:19|20)\d{2})\s*년?\s*(대비|부터|에서|이랑|랑|하고|과|와|및)\s*((?:19|20)\d{2})\s*년"
)
# 질문에 숫자로 적힌 연도 ('년' 없이 쓴 "2020~2025"도)
YEAR_NUMBER = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
YEAR = re.compile(r"((?:19|20)\d{2})\s*년")
# 한글 숫자·사건으로 말한 해 ("이천이십이년", "십 년 전", "코로나 첫 해", "월드컵 때", "그 당시")
# 청년·유소년·노년·중년·장년의 "년"은 연도가 아니다
YEAR_WORDS = re.compile(r"(?<![청소노중장])년|때|당시|해(?![가-힣])")
# 지역 이름이면서 흔한 낱말인 짧은 이름 ("고령 인구", "장수 노인", "영양 상태", "이천 명")
COMMON_WORD_REGIONS = frozenset({"고령", "장수", "영양", "이천", "예산", "수영"})


def _says_year(question):
    """질문에 연도를 말한 표현이 있는지. 있으면 HCX가 읽은 연도를 지우지 않는다.
    애매하면 있다고 본다 (지우면 사용자가 말한 연도를 무시하게 되므로).
    "인구 2000명 이하"의 2000도 있다고 보지만, 그때는 HCX 연도를 그대로 두니 develop과 같다"""
    return bool(
        YEAR.search(question)
        or ask_followup.has_explicit_year(question)
        or YEAR_WORDS.search(question)
        or timeref.resolve(question)
    )


SINCE = re.compile(r"((?:19|20)\d{2})\s*년\s*(이후|이래|부터)(?!.*까지)")
UNTIL = re.compile(r"((?:19|20)\d{2})\s*년\s*까지")
# "한국 인구", "한국 사람"도 전국 ("한국 국적"은 아니다: 귀화자)
NATIONWIDE = re.compile(r"전국|우리나라|대한민국|한국\s*(전체|의|은|는|인구|사람)|국내\s*(전체)?")
WHY_WORDS = re.compile(r"왜|이유|원인|때문|탓|효과|영향")
CAUSAL = re.compile(r"효과가?\s*(있|없|컸)|효과적|원인|왜\s|영향을?\s*(줬|주|미쳤|끼쳤)")
# 지표 사전이 나누지 않는 조건 (체중·기간 등). 버리고 전체 값으로 답하면 틀린 답이 된다
# ("체중 3.2kg 출생아 수" → 전국 출생아 수, "결혼 1년 반 만에 이혼한 건수" → 전체 이혼 건수)
UNSUPPORTED_MEASURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|킬로그램|킬로|키로|그램)|\d+(?:\.\d+)?\s*(?:cm|센티)|"
    r"\d+\s*년\s*반|\d+\s*개월|(?:결혼|혼인|동거|거주|근속|재직)\s*기간|체중|몸무게|키가"
)
# 앞으로를 묻는 질문 ("출산율이 앞으로 오를까?").
# 미래 연도를 말했으면 장래추계로 답하므로 따로 본다
PREDICT = re.compile(
    r"앞으로|향후|미래에?|전망|예측|예상(?!보다)|"
    r"(오를|내릴|늘|줄|늘어날|줄어들|증가할|감소할|떨어질|올라갈|내려갈|회복할|반등할)\s*(까|지)"
)
# "가장 많이 빠져나간 시군구", "합계출산율이 가장 높은 시도" → 순위
RANK_ASK = re.compile(
    r"(가장|제일|꼴찌|최하위)\S*\s*(\S+\s*){0,3}?(시군구|시도|지역|곳|구|시)(는|가|은|이|를|\s|$|\?)"
    r"|(?:비중|비율)이?\s*(?:높|낮)은\s*(?:시군구|시도|지역|곳)(?:은|는|이|가|\s|$|\?)"
)
# 별칭으로 못 찾은 지표 표현 (질문 표현 → 지표 이름)
# "아기 몇 명 낳"은 여자 한 명이 평균 몇 명 낳는지(합계출산율)를 물을 때와 그해 태어난 아이 수
# (출생아 수)를 물을 때가 있다. '한 명당 · 한 명이 · 평균 · 평생'이 있으면 합계출산율
BIRTHS_PER_WOMAN = re.compile(
    r"(?:여자|여성|엄마|가임\s*여성)?\s*(?:한|1)\s*명\s*(?:당|이|이서|마다)|평균|평생|일생|몇\s*명\s*씩"
)
# 몇 명을 묻지 않아도 낳는 · 출산하는 아이 수를 물은 것 ("여자 1명이 평생 낳는 아이 수",
# "여자 한 명이 아이 가장 많이 낳는 동네") — 한 명당 · 평생과 함께일 때만 합계출산율
PER_WOMAN_BIRTH = re.compile(r"(낳|출산)")
BABIES_BORN = re.compile(r"(?:아이|아기|애|신생아)\S*\s*몇\s*명\S*\s*(?:\S+\s*){0,2}낳")
INDICATOR_PATTERNS = [
    (re.compile(r"(아이|아기|신생아)\S*\s*(\S+\s*){0,3}태어"), "출생아 수"),
    (re.compile(r"결혼(을|한)?\s*(\S+\s*){0,2}(많이|적게)"), "혼인 건수"),
    # "태어난 사람이 많아 돌아가신 사람이 많아?" → 출생아 − 사망자
    (
        re.compile(
            r"(태어|출생)\S*\s*(\S+\s*){0,4}?(돌아가|사망|죽)"
            r"|(돌아가|사망|죽)\S*\s*(\S+\s*){0,4}?(태어|출생)"
        ),
        "자연증가 건수",
    ),
    # 지표 이름 없이 정의를 풀어 말한 질문 (변형셋 1차, 2026-11-18)
    # "일할 나이 인구 100명당 노인 몇 명" → 노년부양비
    (
        re.compile(
            r"(일할|일하는|생산\s*가능|생산\s*연령)\s*(나이\s*)?(인구|사람)\S*\s*"
            r"(?:(?:100|백)\s*명\s*)?당\s*(노인|어르신|고령)"
        ),
        "노년부양비",
    ),
    # "어르신 수가 아이들 수의 몇 배" → 노령화지수 (노인이 주어일 때만. "노인 대비 유소년"은 아니다)
    (
        re.compile(
            r"(노인|어르신|고령\S*)\s*(수|인구)?\s*(가|이)\s*(아이|어린이|유소년|애)\S*\s*"
            r"(수|인구)?\s*의\s*몇\s*배"
        ),
        "노령화지수",
    ),
    # "나이 순으로 줄 세우면 가운데가 몇 살" → 중위연령
    (
        re.compile(r"(나이|연령)\s*순\S*\s*(줄|세우|정렬).*(가운데|중간)|(가운데|중간)\s*나이"),
        "중위연령",
    ),
    # "인구에 비해 아기 태어나는 정도" → 조출생률
    (re.compile(r"인구\s*(에\s*비해|대비|당)\S*\s*.*(태어|출생)"), "조출생률"),
    # "이사 온 사람에서 떠난 사람 빼면" → 순이동
    (re.compile(r"(이사\s*온|들어온|전입)\S*.*(떠난|나간|전출)\S*.*(빼|뺀|차감)"), "순이동"),
    # "어르신 비율 · 노인 비중" → 고령인구 비율
    (
        re.compile(r"(어르신|노인|고령자|노령)\S*\s*(인구\s*)?(몇\s*)?(비율|비중|%|퍼센트|프로)"),
        "고령인구 비율",
    ),
    # "사람 수 · 사람 몇 · 사는 사람 → 총인구" 패턴은 뺐다 (11/19 세진). "지하철 타고 출근하거나
    # 학교 다니는 사람 몇 명이야"처럼 다른 통계의 사람을 총인구로 끌어가 표 찾기를 막았고,
    # OTHER_PEOPLE 낱말 목록으로는 일상어를 다 막을 수 없다
]
MORE_SPECIFIC = {
    "조출생률": {"출생아 수"},
    "셋째 이상 출생아 수": {"출생아 수"},
    "다문화 출생아 수": {"출생아 수"},
}
# 변화량 순위 ("작년에 인구 제일 많이 줄어든 시도 세 군데", "인구 증가 폭이 큰 구"):
# 그해 값이 아니라 전년 대비 변화량으로 순위를 매긴다
CHANGE_RANK = re.compile(
    r"(가장|제일)\s*많이\s*(줄어|줄었|감소|빠졌|늘어|늘었|증가)|(감소|증가)\s*폭이?\s*(가장|제일)?\s*(큰|많)"
)
DECREASE_WORDS = re.compile(r"줄어|줄었|감소|빠졌")
# 지역이 아닌 순위 축 ("국적별로 많은 순서", "어느 나라가", "교통수단 중 가장 많이")
OTHER_AXIS = re.compile(
    r"(국적|나라|국가|교통\s*수단|이동\s*사유|전입\s*사유)\s*(별|중|가|이|마다)?"
)
AXIS_REGION = re.compile(r"시도|시군구|지역|도시|광역시|어느\s*(시|구|군|도)")
YEAR_ASKED = re.compile(r"(해|연도|년도|때)\s*(는|가|를|은|가\s*언제|$|\?)|언제")
# 연령 구간 · 연령 집단을 말하고 사람 수를 물었으면 그 연령의 인구 ("달서구 이십대는 몇 명 살아?",
# "광주하고 전남 어르신 수 합쳐줘", "서울 사는 19살부터 34살까지 몇 명")
PEOPLE_COUNT = re.compile(
    r"몇\s*(명|분)|얼마나\s*(살|계|있|돼|되|줄|늘)|(수|인구)\s*(는|가|를|합|얼마|좀)|사람|분\s*(이|들)"
)
# '사람 · 분'이 이 통계의 사람이면 총인구가 아니다
# ("외국인 등록한 사람", "통근한 사람", "사망자 중")
OTHER_PEOPLE = re.compile(
    r"외국|등록|이사|전입|전출|이동|통근|통학|결혼|혼인|이혼|사망|돌아가|태어|출생|낳|가구|유학|귀화|"
    r"근로|이민|입국|출국|취업|학생|"
    # 사는 방식 · 집 · 장애는 다른 통계다 ("혼자 사는 사람" → 1인 가구,
    # "아파트에 사는 사람" → 거처별, "장애가 있는 사람" → 장애인).
    # 총인구로 답하면 전체 인구 숫자가 틀린 답으로 나간다 (11/18)
    r"혼자|홀로|독거|장애|아파트|주택|단독|연립|다세대|오피스텔|거처|셋방|기숙사"
)
# 구성비 질문에서 콕 집어 물은 항목 (질문 표현 → 구성 항목 이름)
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
# 통계를 묻는 질문인지 (범위 밖 질문과 '아직 지원하지 않는 지표'를 구분)
STATS_WORDS = re.compile(
    r"인구|출생|출산|사망|혼인|결혼|이혼|이사|전입|전출|이동|외국인|다문화|가구|주민|아이|자녀|국적|귀화"
    r"|태어|돌아가"
    r"|몇\s*(명|건|가구|분)"
)


def extract_ages(question):
    """질문 속 연령 구간 → [[시작, 끝 또는 None], ...] (나온 순서, 중복 제거)
    고유어 · 한자어 나이도 숫자로 읽는다 ("이십대" → 20대, "일흔 살 이상" → 70세 이상)"""
    bands = []
    for _, _, band in age_spans(agegroup.native_to_digits(question)):
        if band not in bands:
            bands.append(band)
    # "20대(20~29세)"처럼 같은 구간을 두 번 쓰면 하나로
    return bands


def age_spans(question):
    """질문 속 숫자 연령 구간 → [(시작 위치, 끝 위치, [시작, 끝 또는 None]), ...] (나온 순서)"""
    found, covered = [], []

    def add(m, band):
        # 앞에서 더 구체적인 표현으로 이미 읽은 자리면 건너뛴다 ("60대 이상"의 "60대")
        if not any(a < m.end() and m.start() < b for a, b in covered):
            covered.append(m.span())
            found.append((m.start(), m.end(), band))

    for m in AGE_OVER_UNDER.finditer(question):
        add(m, [int(m[1]), int(m[2]) - (m[3] == "미만")])
    for m in AGE_RANGE.finditer(question):
        add(m, [int(m[1]), int(m[2])])
    for m in AGE_SPAN.finditer(question):
        lo, hi = int(m[1]), int(m[2] or m[3])
        if lo < hi:
            add(m, [lo, hi])
    for m in AGE_DECADE_SPAN.finditer(question):
        if int(m[1]) % 10 == 0 and int(m[2]) % 10 == 0 and int(m[1]) < int(m[2]):
            add(m, [int(m[1]), int(m[2]) + 9])
    for m in AGE_OVER.finditer(question):
        add(m, [int(m[1]), None])
    for m in AGE_UNDER.finditer(question):
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
    """질문에서 찾은 지표 이름들 → 하나로 정할 수 있으면 그 이름
    "인구"(총인구)는 거의 모든 질문에 있으므로 다른 지표가 하나 있으면 그쪽,
    비율·구성을 물었으면 비율·구성 지표를 고른다"""
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


def _ratio_sibling(question, parsed, fix):
    """'몇 프로'를 물었는데 수 지표를 골랐으면 같은 이름의
    비율 지표로 (등록외국인 수 → 등록외국인 비율).
    "○○구 주민 중 등록외국인이 몇 %"처럼 '비율'이라는 말 없이 물으면 수로 답했다 (11/18).
    '몇 퍼센트 늘었어'처럼 증감을 물으면 증감률이라 그대로 두고, '전국 중 ~ 비중'은 아래의
    전국 대비 비중(share_of)이 맡으므로 그대로 둔다. 바꿨으면 그 지표 이름, 아니면 None"""
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


# 연도 칸의 계산식 ("start_year": 2025 - 8)
YEAR_ARITH = re.compile(r":\s*(?:19|20)\d{2}\s*[-+]\s*\d{1,3}(?=\s*[,}])")
PERIOD_KINDS = ("recent", "span", "compare_ago", "span_from", "until_now")


def _apply_time(question, parsed, fix, today):
    """상대 기간은 코드가 질문에서 직접 알아보고(timeref.find: 표현 · 종류 · N) 연도도
    규칙표(timeref.period)가 센다. LLM은 오늘이 언제인지 모르고 산수도 틀린다:
    "2020년이랑 작년"의 작년을 2024로, "10년 동안"의 끝을 2024로, '최근 5년'을 6개 해로 적었다.
    여기서 고치는 것은 지금까지 코드가 풀지 못한 모양뿐이다.
      기간(최근 N년 · N년 동안 · N년 전보다 · ○년부터 N년 동안 · 지금까지): 시작 · 끝
      숫자 연도 + 낱말 시점인 추이("2018년이랑 작년"): 두 해를 시작 · 끝으로
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
    if not said and not slots.short_years(question):  # "22년"처럼 두 자리로 말한 연도는 둔다
        keys = ["start_year", "end_year"]
        if kind_now == "trend" and any(r["kind"] in PERIOD_KINDS for r in refs):
            # 기간을 물은 추이의 year: 계산이 끝 해로 쓴다 ("7년 동안"에 HCX가 2022를 넣어
            # 2019~2022로 답했다)
            keys.append("year")
        for key in keys:
            if key not in touched and parsed.get(key):
                fix(key, None, "질문에 숫자로 없는 연도 (상대 기간은 코드가 센다)")
    _drop_unsaid_years(question, parsed, fix, touched)
    parsed["time_refs"] = resolved


def _drop_unsaid_years(question, parsed, fix, touched):
    """질문에 숫자 연도가 있는데 HCX가 연도 칸에 그 밖의 연도를 넣었으면 믿지 않는다
    (잘못 옮겨 적었거나 짐작한 연도. 코드가 방금 센 칸(touched)은 그대로).
    숫자 연도가 없는 질문은 보지 않는다: 한글 연도 · 사건으로 말한 해("코로나 첫 해")는
    HCX가 푼 값을 쓴다 (_says_year)"""
    said = {int(y) for y in YEAR_NUMBER.findall(question)} | slots.short_years(question)
    if not said:
        return
    for key in ("year", "start_year", "end_year"):
        value = parsed.get(key)
        if key not in touched and isinstance(value, int) and value not in said:
            fix(key, None, "질문에 없는 연도")


def _blank_to_none(parsed, fix):
    """HCX가 '없음'을 0이나 빈 문자열로 적은 칸을 정리한다 (연도 0 · "" → 없음, 성별 "" → 계).
    지금 지시문에서도 성별 빈 문자열이 나오고(11/22 80문항 중 3), 지시문에서 '최신 연도' 줄을 빼면
    연도 0이 3~7% 나온다. 0을 진짜 연도로, ""를 성별 값으로 읽지 않게 한다.
    숫자 글자로 온 연도("2020")는 숫자로 바꾼다"""
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
    """같은 질문도 실행마다 HCX 해석이 달라서, 질문 원문에서 확실히 알 수 있는 것은 코드가 맞춘다.

    - 지표: 질문에 지표 이름·별칭이 있으면 그 지표 (예: "고령화율"을 노령화지수로 해석한 경우)
    - 지표 바꿔치기 막기: 질문에 근거가 없는 계산 지표, "몇 명"을 물었는데 비율 지표면 거절,
      지표의 제외 표현이 질문에 있으면 거절 (예: "등록외국인"은 외국인주민 수가 아님)
    - 연령 구간·지역 묶음(수도권 등)·합계·극값 연도·연속·반등·몇 배·전국 대비 비중은 원문에서 읽는다
    - 성별: "여성/여자", "남성/남자" 중 하나만 있으면 그 성별
    - 구성비: 구성·비중을 묻지 않았으면 구성비가 아니다 ("구별 20대 인구" → 구별 목록)
    - 지역: "전국"이 있는데 지역이 비었으면 전국
    - 연도: "작년", "올해" 같은 상대 표현은 오늘 날짜 기준
    고친 내용은 parsed["corrections"]에 남긴다.
    """
    today = today or date.today()
    kind = parsed.get("type")
    fixes = []

    def fix(key, value, reason):
        fixes.append(f"{key}: {parsed.get(key)!r} → {value!r} ({reason})")
        parsed[key] = value

    _blank_to_none(parsed, fix)
    name = parsed.get("indicator")
    if name and indicators.spec(name) is None:
        # HCX가 "전출자 수(명)"·"고령인구 비율 (%)"처럼 단위를 붙여 쓰면 지표를 못 찾는다
        bare = UNIT_SUFFIX.sub("", name)
        if bare != name and indicators.spec(bare) is not None:
            fix("indicator", bare, "지표 이름 뒤의 단위 표기")

    undone_pct = False
    if kind == "causal" and not (CAUSAL.search(question) or WHY_WORDS.search(question)):
        # "작년 혼인 중에 남녀 모두 초혼인 비율" 같은 비율 질문을 HCX가 원인 질문으로 읽었다
        kind = "value"
        fix("type", kind, "원인을 묻는 말이 없음")
        undone_pct = bool(PCT_ASK.search(question))
    if kind == "other" and CAUSAL.search(question):
        fix("type", "causal", "원인·효과를 묻는 질문")
        kind = "causal"
    elif kind == "other" and STATS_WORDS.search(question):
        kind = "compare" if len(parsed.get("regions") or []) > 1 else "value"
        if COMPOSITION_WORDS.search(question) and re.search(r"남녀|성별|연령", question):
            kind = "composition"  # 예: "전국 인구 중 남녀 비중"
        fix("type", kind, "통계를 묻는 질문")
        if indicators.spec(parsed.get("indicator")) is None:
            parsed["unsupported"] = True  # 지원 지표가 아니어서 HCX가 other로 본 것

    ages = extract_ages(question)
    if ages:
        parsed["ages"] = ages
    # "노인"·"청년" 같은 이름은 정의 구간으로 (답변에 기준 고지).
    # 단, 출생 지표의 "아기 · 아이"는 태어난 아이를 가리키는 말이지 0세 연령 조건이 아니다
    # ("다문화 가정 아기 최근 5년" → 연령 조건을 붙여 '연령으로 나눈 자료 없음'으로 끝났다)
    elif (group := agegroup.resolve(question)) and not (group.lo == 0 and _about_births(parsed)):
        parsed["age_group"] = {"expr": group.expr, "range": group.label(), "basis": group.basis}
        ages = [[group.lo, group.hi]]  # 이름 연령도 계산에 쓴다 (예: 청년 → 19~34세)
        parsed["ages"] = ages
    if parsed.get("sex") not in (None, "", "계", "남", "여"):  # 예: ["남", "여"]
        fix("sex", "계", "성별 값 정리")
    if parsed.get("level") in ("구", "시군구별", "구별", "군", "시군"):
        fix("level", "시군구", "지역 수준 정리")
    if sub := find_sub_area(question):
        parsed["sub_area"] = list(sub)
    merged = _merge_regions(parsed.get("regions") or [])
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
        # 더 구체적인 지표가 함께 걸렸으면 그쪽 ("인구에 비해 아기 태어나는 정도"는 조출생률,
        # "셋째나 그 이후로 태어난 아기"는 셋째 이상 출생아 수)
        for specific, general in MORE_SPECIFIC.items():
            if specific in names:
                names -= general
        name = _pick_indicator(question, names)
        current = indicators.canonical_name(parsed.get("indicator") or "")
        if name and current != name:
            # "인구"만 찾았고 HCX가 다른 지표를 골랐으면 그대로 둔다 (예: "인구에서 고령층 비중")
            # 단, 연령 구간 인구 수를 물었으면 총인구 (예: "65세 이상 인구는?")
            if name != "총인구" or current is None or (ages and not PCT_ASK.search(question)):
                fix("indicator", name, "질문의 지표 표현")
                parsed.pop("unsupported", None)
        if sibling := _ratio_sibling(question, parsed, fix):
            names = names | {sibling}  # 질문의 수 지표 표현 + '몇 프로'가 근거
        _guard_indicator(question, parsed, names, fix)
        _age_population(question, parsed, fix)
        # 질문이 다른 통계를 가리키면 비슷한 이름의 지표로 답하지 않는다
        # 예) "등록외국인 수"를 외국인주민 수(귀화자·자녀 포함)로 답하지 않고 표 안내로
        current = indicators.canonical_name(parsed.get("indicator") or "")
        if current and (phrase := indicators.excluded_phrase(current, question)):
            fix(
                "indicator",
                parsed.get("term") or phrase,
                josa(f"'{current}'", "과와") + " 다른 통계",
            )
            parsed["unsupported"] = True
        kind = parsed.get("type")
        # 질문에 고른 지표의 이름·별칭이 있는가. 없으면 HCX가 비슷한 지표를 고른 것일 수 있어
        # ask_session이 통계표에서 바로 계산해 보고, 되면 그 답을 쓴다 (예: 통근 인구 → 전출자 수)
        chosen = indicators.canonical_name(parsed.get("indicator") or "")
        # 지표가 없으면(원인 질문 등) 판단하지 않는다 (None)
        parsed["grounded"] = (chosen in names) if chosen else None
        if chosen == "총인구" and parsed["grounded"] is False and _plain_people_question(question):
            # "노원구가 강남구보다 사람이 많아 적어?": '인구'라는 말은 없지만 다른 통계를 가리키는
            # 말도 없다 → 총인구로 본다 (통계표 찾기로 돌아가지 않고 사전 계산으로 답한다)
            parsed["grounded"] = True
        if chosen == "총인구" and population_modifier(question):
            # "통학 인구"·"외국인 인구"처럼 '인구' 앞에 다른 통계를 가리키는 말이 있으면
            # 별칭 '인구'만으로 총인구라고 볼 수 없다 → 바로 계산을 시도한다
            parsed["grounded"] = False
        chosen_spec = indicators.spec(chosen) if chosen else None
        if undone_pct and chosen_spec and lookup.kind_of(chosen_spec) == "count":
            # 원인 질문으로 잘못 읽은 비율 질문인데 고른 지표가 건수다 ("혼인 중 초혼 비율" → 혼인
            # 건수): 건수로 답하지 않고 통계표에서 비율을 계산해 본다
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
    _read_question(question, parsed, fix)

    genders = [sex for sex, pattern in GENDER_WORDS.items() if pattern.search(question)]
    if len(genders) == 1 and parsed.get("sex") in (None, "", "계"):
        fix("sex", genders[0], "질문의 성별 표현")
    both = len(genders) == 2 or (not genders and SEX_SPLIT.search(question))
    if parsed.get("sex") in ("남", "여"):
        if both:
            fix("sex", "계", "남녀를 함께 물음")  # "남자랑 여자 중 누가", "남녀 인구 비교"
        elif not genders and not SEX_IMPLIED.search(question):
            # 성별을 말하지 않았는데 HCX가 짐작해 넣은 성별 ("서울 인구 몇 명이야?" → 남)
            fix("sex", "계", "성별을 말하지 않음")
    if both and kind != "composition" and not SEX_TOTAL.search(question):
        # 남녀를 나눠 물음 → 계산 단계(compute)가 남·여를 따로 계산한다 (구성비는 by=sex가 맡는다)
        parsed["split_sex"] = True
        if kind == "compare" and len(parsed.get("regions") or []) == 1:
            fix("type", "value", "한 지역의 남녀 비교")  # 지역 비교가 아니라 남·여 값
    if kind == "composition" and parsed.get("by") != "sex" and SEX_SPLIT.search(question):
        fix("by", "sex", "질문의 남녀 구성 표현")

    _full_region_names(question, parsed, fix)
    _ground_regions(question, parsed, fix)

    if NATIONWIDE.search(question) and kind not in NO_REGION_TYPES:
        regions = parsed.get("regions") or []
        if not regions or all(r in NATIONAL_NAMES for r in regions):
            fix("regions", ["전국"], "질문의 전국 표현")

    years = {int(y) for y in YEAR.findall(question)}
    has_abs_year = bool(years)
    single = len(years) == 1 and not SPAN.search(question) and not SINCE.search(question)
    if single and parsed.get("type") not in ("trend", "other", "parse_error"):
        year = years.pop()
        if parsed.get("year") != year:  # HCX가 질문의 연도 대신 최신 연도를 넣은 경우
            fix("year", year, "질문의 연도")

    if not has_abs_year and (ref := timeref.resolve(question, today)):
        if ref.open_end:  # "3년 전부터" → 그 해부터 최신까지
            if parsed.get("start_year") != ref.year:
                fix("start_year", ref.year, ref.describe(today))
            if parsed.get("end_year"):
                fix("end_year", None, "'부터'는 최신 연도까지")
            if kind == "value":
                fix("type", "trend", "기간의 변화를 물음")
                kind = "trend"
        elif kind not in ("trend", "other", "parse_error") and parsed.get("year") != ref.year:
            fix("year", ref.year, ref.describe(today))
    elif not has_abs_year and not _says_year(question) and parsed.get("year"):
        # 질문에 연도 표현이 전혀 없을 때만 HCX가 짐작해 넣은 연도를 지운다
        # (예: 연도 없이 물었는데 2022). "22년", "2022.12", "이천이십이년", "코로나 첫 해"처럼
        # 연도를 말했으면 HCX 연도를 그대로 둔다.
        # 질문 유형과 관계없이 지운다 (11/22): 값 · 비교 · 순위만 지웠더니 구성비 · 원인 · 기타로
        # 읽힌 질문은 프롬프트가 넣게 한 최신 연도(2025)가 남아, 통계표 찾기로 넘어가 2020년까지인
        # 표를 만나면 범위 밖이 됐다. 비우면 계산이 그 자료의 마지막 해를 쓴다
        fix("year", None, "연도를 말하지 않음 → 자료 마지막 해")

    # 지역이 여럿인 값 질문: 합계를 물었으면 더하고, 아니면 지역별 비교로 답한다.
    # value + 여러 지역 + 합계 표시 없음은 계산이 첫 지역만 써서 나머지 지역이 말없이 빠졌다
    # (2026-11-13 결함 1)
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
    if len(names) > 1 and parsed.get("type") in ("value", "trend", "compare"):
        parsed["indicators"] = names  # 지표마다 따로 계산한다 (compute)

    _cross(question, parsed, fix)
    _change_rank(question, parsed, fix)
    _region_mean(question, parsed, fix)
    other_axis = OTHER_AXIS.search(question) and not AXIS_REGION.search(question)
    if parsed.get("type") == "rank" and other_axis:
        # "등록외국인 국적별로 많은 순서 세 나라": 지역이 아닌 분류의 순위. 사전 지표는 지역 순위만
        # 하므로 그 지표의 표에서 분류 순위를 계산한다 (ask_session._dynamic_instead)
        parsed["rank_axis"] = OTHER_AXIS.search(question)[1]
        parsed["grounded"] = False
    if fixes:
        parsed["corrections"] = fixes
    return parsed


def _change_rank(question, parsed, fix):
    """변화량 순위: rank_by에 감소(decrease) · 증가(increase)를 적는다. "인구"만 말했으면 총인구
    (HCX가 "인구가 줄어든"을 자연증가 건수로 읽었다)"""
    if parsed.get("cross") or not (m := CHANGE_RANK.search(question)):
        return
    if YEAR_ASKED.search(question):
        return  # "출생아가 가장 많이 늘어난 해는?": 지역이 아니라 해를 묻는다 (추이의 극값 연도)
    if parsed.get("type") not in ("rank", "compare", "value", None):
        return
    parsed["rank_by"] = "decrease" if DECREASE_WORDS.search(m[0]) else "increase"
    if parsed.get("type") != "rank":
        fix("type", "rank", "변화량이 큰 지역을 물음")
    names = {name for *_, name in indicators.find_in_text(question)}
    if names == {"총인구"} and parsed.get("indicator") != "총인구":
        fix("indicator", "총인구", "'인구'의 변화량")
        parsed["grounded"] = True


# "시도 평균 출생아 수", "서울 구별 평균": 여러 지역 값의 단순 평균 ("전국 평균과 비교"는 아니다)
REGION_MEAN = re.compile(
    r"(시도|광역|시군구|구|군|지역|도시)\s*(별|들|마다)?\s*(의\s*)?평균(?!\s*(과|보다|이랑|대비))"
)


def _region_mean(question, parsed, fix):
    """지역 평균: 순위(모든 지역 값)를 구해 평균한다 (compute의 region_mean)"""
    if parsed.get("cross") or parsed.get("rank_by") or not (m := REGION_MEAN.search(question)):
        return
    if parsed.get("type") not in ("value", "rank", "compare", None):
        return
    regions = [r for r in parsed.get("regions") or [] if r != "전국"]
    sido = parsed.get("sido")  # "서울 구별"은 지역별 목록이 이미 시도 안의 시군구로 읽었다
    level = "시도" if m[1] in ("시도", "광역") or not (regions or sido) else "시군구"
    parsed["region_mean"] = True
    if parsed.get("type") != "rank":
        fix("type", "rank", "여러 지역 값의 평균")
    if parsed.get("level") != level:
        fix("level", level, "평균할 지역 수준")
    parsed["top"] = None
    # 시군구 평균은 그 시도 안의 순위 (regions에 시도 하나: 순위가 그 범위로 계산한다)
    # 시군구 평균은 그 시도 안의 시군구 순위로 (지역별 목록과 같이 sido에 범위)
    if level == "시군구" and not sido and regions:
        fix("sido", regions[0], "평균할 범위")
    if level == "시도" and sido:
        fix("sido", None, "시도 평균")
    if parsed.get("regions"):
        fix("regions", [], "지역 평균")


def _mean_of_regions(q):
    """지역 평균 = 순위의 모든 지역 값의 단순 평균 (값이 없는 지역은 뺀다)"""
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


PEOPLE_INDICATOR = re.compile(r"인구|사람|주민|남녀|성별|연령|나이")
CROSS_DYNAMIC = "cross_dynamic"  # 두 값 중 한쪽이 사전에 없음 (ask_session이 마저 계산)


def _about_births(parsed):
    """해석한 지표가 출생 지표인가 (출생아 수 · 다문화 출생아 수 · 셋째 이상 출생아 수 등)"""
    name = indicators.canonical_name(parsed.get("indicator") or "") or ""
    return "출생" in name


def _cross(question, parsed, fix):
    """두 지표를 나눈 값 ("세대당 출생아 수", "인구 1,000명당 외국인근로자 수"). cross_indicator.py
    공식 지표가 있으면 그 지표로, 없으면 parsed["cross"]에 두 지표를 적어 compute가 나눠 계산한다.
    HCX는 이런 질문을 재료 지표 하나(출생아 수)로 읽어 그 건수를 답하기 쉽다"""
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
        # "출생아 수 대비 사망자 수"의 '대비'를 지역 비교로 읽은 경우 (지역은 하나)
        fix("type", "value", "'대비'는 두 지표를 나눈 것 (비교할 지역이 하나)")
    parsed["term"] = found.official or found.name
    parsed["indicators"] = None
    parsed["grounded"] = True
    parsed.pop("unsupported", None)
    if (found.plus or found.form == "sum") and not parsed.get("group"):
        # "혼인 건수와 이혼 건수 합"의 합은 분자 두 지표를 더하라는 말이다
        # (기간 · 지역 합계가 아니다)
        parsed.pop("aggregate", None)


# 지표 이름 사이에 이 말만 있으면 여러 지표를 함께 물은 것 ("출생아 수와 사망자 수")
AND_WORDS = re.compile(r"^(와|과|하고|랑|이랑|및|,|그리고|,그리고|와함께|과함께)$")


# '인구' 앞에 와도 총인구인 말: 지역·성별·연령·전체를 뜻하는 말 (그 밖이면 다른 통계일 수 있다)
PLAIN_POPULATION = {
    "총", "전체", "주민등록", "우리나라", "한국", "국내", "전국", "남자", "여자", "남성", "여성",
    "남녀", "이상", "이하", "미만", "대", "세", "살", "노인", "고령", "노년", "청년", "청소년",
    "유소년", "어린이", "아동", "생산연령", "중장년", "현재", "작년", "올해", "지난해", "의",
    "수도권", "비수도권", "광역시", "특별시", "시군구", "시도", "지역", "구", "군", "시", "도",
    "나이", "연령", "연령대", "만",
    # 합치기 · 고르기 말 ("부산 울산 경남 합쳐서 인구", "경기도 시군 중 인구 많은 순서")
    "합쳐서", "합친", "합하면", "더한", "중", "모두", "전부", "다", "사는",
    # 세는 말 ("도시 3개 인구로", "상위 5곳 인구")
    "개", "곳", "군데",
}  # fmt: skip
POPULATION_NOUN = re.compile(r"([가-힣]+)\s*인구")


# '사람 · 주민'으로 인구를 물은 질문에 있어도 되는 말: 세는 말 · 비교 · 순위 · 지역 단위.
# 여기에 없는 낱말이 하나라도 남으면 다른 통계의 사람일 수 있다 ("지하철 타고 출근하는 사람",
# "혼자 사는 사람") → 총인구로 확정하지 않고 지금처럼 통계표 찾기에 넘긴다.
# 막을 낱말을 모으는 방식(OTHER_PEOPLE)은 일상어를 다 막지 못해 11/19에 '사람 → 총인구' 규칙을
# 뺐다. 반대로 허용할 말만 정해 두면 모르는 말은 늘 넘어간다
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
    """낱말 하나가 지역 이름이거나 PLAIN_PEOPLE_WORD 인가 (조사를 뗀 모양도 본다)"""
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
    """'사람 · 주민'으로 인구를 물었고, 지역 · 연도 · 세는 말 · 비교 · 순위 말 말고는
    남는 낱말이 없는가"""
    if not PEOPLE_WORD.search(question) or OTHER_PEOPLE.search(question):
        return False
    text = YEAR.sub(" ", question)
    for ref in timeref.find(question):
        text = text.replace(ref["expr"], " ")
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", text)
    return bool(tokens) and all(_plain_token(t) for t in tokens)


def population_modifier(question):
    """'인구' 바로 앞의 말이 지역·성별·연령이 아닌 다른 통계를 가리키면 그 말 (예: 통학, 외국인)"""
    if group := agegroup.resolve(question):
        # 연령 집단 이름은 연령 조건이다 ("2030세대 인구"의 '세대'를 다른 통계로 보지 않는다)
        question = question.replace(group.expr, " ")
    # 지역 묶음·생활권 이름도 지역 조건이다
    # ("부울경 인구", "특례시 인구"의 앞말을 다른 통계로 보지 않는다)
    for word in REGION_GROUPS:
        if _group_in(word, question):
            question = question.replace(word, " ")
    if sub := find_sub_area(question):
        question = question.replace(sub[0], " ")
    for m in POPULATION_NOUN.finditer(question):
        # 조사를 뗀 말과 그대로의 말을 모두 본다 ("나이"의 '이'는 조사가 아니다)
        words = {m[1], re.sub(r"(의|은|는|이|가|에서|에|별)$", "", m[1]) or m[1]}
        if any(w in PLAIN_POPULATION or re.search(r"\d", w) for w in words):
            continue
        if any(indicators.find_regions(w) or agegroup.resolve(w) for w in words):
            continue
        return min(words, key=len)
    return None


def asked_indicators(question):
    """질문이 함께 물은 지원 지표들 (MAX_INDICATORS개까지). 하나면 [] (HCX 해석을 그대로 쓴다).
    "서울 인구 중 고령인구 비율"처럼 이름이 이어지지 않으면 여러 지표로 보지 않는다"""
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
    """HCX가 질문에 없는 지역으로 바꾸지 않게 한다 (예: "송도" → 인천광역시 전체)
    질문에 그 지역 이름이 없고, 질문에서 찾은 지역 이름도 없으면 그 지역으로 답하지 않는다"""
    if parsed.get("group"):
        return
    text = indicators._norm(question)
    found = set()  # 질문에 나온 지역들의 코드 (시군구면 속한 시도 코드도)
    for name in indicators.find_regions(question):
        reg = indicators.resolve_region(name)
        for c in [reg] if reg["status"] == "ok" else []:
            found |= {c["region_code"], c.get("sido_code")}
        for cand in reg.get("candidates", []):  # 이름이 같은 여러 지역은 이름으로 비교
            found.add(indicators._norm(cand))
    for r in parsed.get("regions") or []:
        if r == "전국" or r in NATIONAL_NAMES or indicators._norm(r) in text:
            continue
        reg = indicators.resolve_region(r)
        codes = {reg.get("region_code"), indicators._norm(reg.get("display", ""))}
        if reg["status"] != "ok" or codes & found:
            continue  # 지역을 못 찾으면 계산 단계에서 그대로 안내한다
        # "강남" → 서울특별시 강남구처럼 끝의 시·군·구를 뗀 짧은 이름으로 물은 경우
        short = re.sub(
            r"(특별자치시|특별자치도|특별시|광역시|시|군|구|도)$", "", reg.get("region_name") or ""
        )
        # 단, 질문에 시도가 있으면 그 시도 안의 지역일 때만
        # (예: "서울 강서"를 부산 강서구로 바꾸면 막는다)
        named = found - {None}
        same_sido = not named or reg.get("sido_code") in named
        # 짧은 이름이 지표 이름 안에만 있거나("고령" ⊂ "고령인구 비율") 흔한 낱말이면
        # 지역으로 보지 않는다
        spans = indicators.find_in_text(question)
        hits = [m.start() for m in re.finditer(re.escape(short), text)] if len(short) >= 2 else []
        in_indicator = bool(hits) and all(any(a <= i < b for a, b, _ in spans) for i in hits)
        if hits and same_sido and not in_indicator and short not in COMMON_WORD_REGIONS:
            # 질문에 시도가 없고 짧은 이름이 여러 곳이면 (예: "강서" → 서울·부산)
            # AI가 고른 곳 대신 되묻는다
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
    """HCX가 줄여 쓴 지역 이름을 질문의 정식 이름으로 되돌린다.
    "광주광역시"를 "광주"로 줄이면 경기 광주시와 겹쳐 되묻게 되므로,
    질문에 그 이름으로 시작하는 지역 이름이 하나뿐이고 바로 찾아지면 그것을 쓴다"""
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


def _merge_regions(regions):
    """["서울", "중구"] → ["서울 중구"]: 시도 바로 뒤에 그 시도 안의 시군구가 오면 한 지역"""
    out = []
    for name in regions:
        if out and indicators.resolve_region(out[-1]).get("kind") == "시도":
            both = indicators.resolve_region(f"{out[-1]} {name}")
            if both["status"] == "ok" and both["kind"] != "시도":
                out[-1] = f"{out[-1]} {name}"
                continue
        out.append(name)
    return out


# 연령 집단으로 정의한 계산 지표 → (필요한 연령 구간들, 묻는 말). 질문에 그 집단(이름·숫자 구간)과
# 묻는 말이 있으면 지표 이름이 없어도 근거가 있다
# ("노인이 몇 퍼센트" → 고령인구 비율, "애들보다 노인이 몇 배" → 노령화지수)
# 배수 지표는 나누는 쪽 구간도 적는다 (노령화지수 = 65세 이상 ÷ 0~14세 × 100)
AGE_DEFINED = {
    "고령인구 비율": ([(65, None)], PCT_ASK, None),
    "유소년인구 비율": ([(0, 14)], PCT_ASK, None),
    "노령화지수": ([(0, 14), (65, None)], re.compile(r"몇\s*배|대비"), (0, 14)),
}
# 사람 수를 묻는 말. 연령 집단 이름("노인", "어르신")은 그 자체로 사람이라 이 말이 없어도 된다
PEOPLE_ASK = re.compile(r"인구|몇\s*(명|분)|사람|인원")
# 사람이 아닌 것을 세는 질문 ("청년 1인 가구 몇 가구")
OTHER_UNITS = re.compile(r"가구|세대|몇\s*(곳|개|건|집)")
# HCX가 지표 칸에 적은 말에서 지우면 "연령 구간 인구"만 남는지 볼 때 지우는 말
POPULATION_WORDS = re.compile(r"인구|인원|사람|수|명|분|몇|들|[\s()·,~\-]")
AGE_PATTERNS = (
    AGE_OVER_UNDER, AGE_RANGE, AGE_SPAN, AGE_OVER, AGE_UNDER, AGE_UNDER_EQ,
    AGE_DECADE_SPAN, AGE_DECADE_RANGE, AGE_DECADE_OVER, AGE_DECADE_PART, AGE_DECADE,
)  # fmt: skip


def _age_bands(question):
    """질문에 나온 연령 구간 전부: 숫자 구간 + 연령 집단 이름"""
    bands = {tuple(b) for b in extract_ages(question)}
    return bands | {(r.lo, r.hi) for r in agegroup.resolve_all(question)}


def _band_spans(question):
    """질문 속 연령 구간의 자리: [(시작, 끝, (시작 나이, 끝 나이)), ...] (숫자 구간 + 집단 이름)"""
    spans = [(a, b, tuple(band)) for a, b, band in age_spans(question)]
    spans += [(a, b, (r.lo, r.hi)) for a, b, r in agegroup.spans(question)]
    return sorted(spans)


# "A보다 B가 몇 배", "A 대비 B"에서 A(나누는 쪽)
BASE_MARK = re.compile(r"\s*(?:이|가|은|는|들)?\s*(?:보다|대비|에\s*비해|에\s*비하면)")


def _base_band(question):
    """배수·대비 질문에서 나누는 쪽 연령 구간. "보다·대비" 바로 앞의 구간, 없으면 None"""
    for _a, b, band in _band_spans(question):
        if BASE_MARK.match(question, b):
            return band
    return None


def _age_grounded(question, name):
    """연령 집단으로 정의한 지표를 질문이 그 집단으로 물었는지.
    배수 지표는 나누는 쪽도 맞아야 한다: 노령화지수는 노인 ÷ 유소년이라
    "애들보다 노인이 몇 배"는 맞고 "노인보다 애들이 몇 배"는 아니다"""
    rule = AGE_DEFINED.get(name)
    if rule is None:
        return False
    bands, ask, base = rule
    if not ask.search(question) or not set(bands) <= _age_bands(question):
        return False
    said = _base_band(question)
    return base is None or said is None or said == base


# 연령 구간과 "몇 명" 사이에 와도 사람 수를 묻는 것으로 보는 말
# ("광주랑 전남 노인 다 합치면 몇 명", "30대 중에 서울 사는 사람")
COUNT_FILLER = frozenset(
    "다 모두 전부 총 합치면 합하면 더하면 대략 약 대충 중 중에 중에서 사는 살고 있는 계신 사시는"
    " 남자 여자 남성 여성 남자애 여자애 사람 인구 주민 인원".split()
)
PARTICLES = re.compile(r"(?:은|는|이|가|을|를|도|의|에|에서|랑|이랑|하고|와|과|들)$")


def _counts_people(question, parsed):
    """연령 구간 바로 뒤에서 "몇 명"까지 다른 대상(취업자·학생 등)이 없는지.
    "청년 취업자 몇 명"은 청년 인구가 아니다"""
    bands = _band_spans(question)
    # 연령 구간 뒤에 처음 나오는 "몇 명·사람·인구" ("서울 사는 사람 중 30대 몇 명"의 "몇 명")
    m = next((x for x in PEOPLE_ASK.finditer(question) if bands and x.start() >= bands[0][1]), None)
    if m is None:
        return bool(parsed.get("age_group"))  # "부산 어르신들 얼마나 늘었어"
    spans = [s for s in bands if s[1] <= m.start()]
    regions = parsed.get("regions") or []

    def allowed(word):
        bare = PARTICLES.sub("", word) or word
        return any(w in COUNT_FILLER or any(w in r for r in regions) for w in (word, bare))

    # 연령 구간 뒤 ~ "몇 명" 사이 ("청년 취업자 몇 명")
    if not all(
        allowed(w) for w in re.findall(r"[가-힣A-Za-z]+", question[spans[-1][1] : m.start()])
    ):
        return False
    # 연령 구간 앞의 "○○ 중" ("취업자 중 청년 몇 명")
    head = question[: spans[0][0]]
    return all(allowed(w) for w in re.findall(r"([가-힣A-Za-z]+)\s*중(?:에|에서)?\s", head + " "))


def _asks_age_population(question, parsed):
    """연령 구간의 사람 수를 물었는지: 연령이 있고, 비율·배수가 아니고, 연령 집단 자체를 센다"""
    return bool(
        parsed.get("ages")
        and not PCT_ASK.search(question)
        and not RATIO_ASK.search(question)
        and not OTHER_UNITS.search(question)
        and (PEOPLE_ASK.search(question) or parsed.get("age_group"))
        and _counts_people(question, parsed)
    )


def _population_label(label, regions=()):
    """HCX가 적은 지표 이름이 "연령 구간 인구"뿐인지
    ("19~34세 인구 수", "유소년인구 수", "100살 이상 어르신")"""
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
    """HCX가 지표를 비웠거나 사전에 없는 이름("19~34세 인구 수", "유소년인구 수")을 줬어도
    연령 구간 인구 수를 물었으면 총인구 (연령 구간 인구)로 답한다"""
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


def _guard_indicator(question, parsed, names, fix):
    """지원 지표로 바꿔서 답하지 않게 한다 (0회차: 다른 지표로 답한 8문항)
    - 계산 지표(고령인구 비율 등)는 질문에 그 지표 표현이 있어야 한다
    - "몇 명"을 물었는데 비율 지표면 맞지 않는다
    맞지 않으면 연령 구간 인구 수로 답할 수 있으면 총인구, 아니면 지원하지 않는 지표"""
    sp = indicators.spec(parsed.get("indicator"))
    if sp is None:
        return
    k = lookup.kind_of(sp)
    # 고른 지표의 별칭 안에 든 "몇 명"은 사람 수를 묻는 말이 아니다
    # ("노인 한 명을 몇 명이 부양해" = 노인 1명당 생산연령인구, 노인 인구 수가 아님)
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
        else:
            term = parsed.get("term") or parsed.get("indicator")
            fix("indicator", term, "질문에 근거가 없는 지표")
            parsed["unsupported"] = True


def _region_list(question, parsed, fix):
    """'서울 구별 ~' → 서울 안의 시군구 목록(순위). 지역은 sido로 옮긴다"""
    regions = parsed.get("regions") or []
    level = "시도" if re.search(r"시도\s*(별|마다)", question) else "시군구"
    if parsed.get("level") != level:
        fix("level", level, "지역별 목록")
    if level == "시군구":
        # 목록의 범위가 될 시도: 질문 원문 → HCX 지역 → HCX sido 순서로 처음 나오는 시도
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


# "수도권 외"·"수도권 밖"의 외·밖 뒤에 낱말이 이어지면 다른 말이다 ("수도권 외국인", "수도권 외곽").
# 조사 · "지역"만 붙어도 된다 ("수도권 외의", "수도권 외 지역", "수도권 외에서")
GROUP_TAIL = re.compile(r"(?:의|에서|에|은|는|이|가|로|으로|도|만|지역|인구)?(?![가-힣])")


def _group_in(word, question):
    """지역 묶음 이름이 질문에 있는가. 끝이 외·밖이면 뒤에 다른 낱말이 이어지지 않아야 한다"""
    if not word.endswith((" 외", " 밖")):
        return word in question
    return any(GROUP_TAIL.match(question, m.end()) for m in re.finditer(re.escape(word), question))


def _read_question(question, parsed, fix):
    """질문 원문에서 바로 읽을 수 있는 조건 (지역 묶음, 합계, 극값 연도, 연속, 반등 등)"""
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
    if PREDICT.search(question) and kind not in ("other", "parse_error"):
        # 미래 연도를 말했으면 장래추계로 답한다 (예: "2040년 서울 인구는 몇 명이 될까?")
        lo, hi = indicators._data_years()
        years = [y for y in (parsed.get("year"), parsed.get("end_year")) if y]
        if not any(y > hi for y in years):
            parsed["prediction"] = True
    if (m := SPAN.search(question)) and kind in ("value", "trend", "causal"):
        start, end = sorted((int(m[1]), int(m[3])))  # "2023년이랑 2020년"도 앞선 해부터
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
        if not SPAN.search(question) and not SINCE.search(question):  # 기간을 안 물었으면 전체
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
        asked["latest"] = True  # "최근 서울 인구" → 연도 없이 가장 최근 값 (인구는 최신 달까지)
        if THESE_DAYS.search(question):
            asked["these_days"] = True  # 추이면 최근 5개 해 (11/22 세진, 골든셋 '요즘' 기준)
        if kind == "trend" and not by_year and not CHANGE_ASK.search(question):
            # HCX가 "최근"을 최근 몇 년 추이로 읽는 경우: 변화를 묻지 않았으면 최신 값 하나
            fix("type", "value", "최근 값을 물음 (변화를 묻지 않음)")
            for key in ("start_year", "end_year"):
                if parsed.get(key):
                    fix(key, None, "최근 값을 물음")
            kind = "value"
        if kind in LATEST_KINDS and parsed.get("year"):
            # 질문에 연도가 없으니 HCX가 넣은 연도는 짐작이다. 계산 단계가 표마다 마지막 해를 쓴다
            fix("year", None, "최근 값은 자료 마지막 해")
        elif kind == "trend":
            # "최근 부산 고령인구 비율 추이": 연도를 말하지 않았으므로 HCX가 넣은 기간(2020~2024 등)
            # 대신 자료 마지막 해까지 (계산 단계가 표마다 마지막 해를 정한다)
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
        # 연도를 말하지 않은 질문은 "최근"과 같이 가장 최근 자료로 답한다
        # - 조회 지표 ("서울 외국인주민 수"): 표마다 마지막 해
        #   (공통 기준 연도로 계산하면 2024년까지인 표는 자료가 있는데도 범위 밖이 된다)
        # - 주민등록인구 계산 지표 ("서울 인구"): 연간 자료 다음 해의 KOSIS 최신 달
        #   (연간 마지막 해 12월 말이 아니라). 연간 자료와 섞는 지표는 compute가 연간으로 다시 계산
        asked["latest"] = True
    elif (
        kind == "trend"
        and not by_year
        and not _says_year(question)
        and not SPAN.search(question)
        and not SINCE.search(question)
        and not ask_followup.FROM_PAST.search(question)
    ):
        # 기간을 말하지 않은 추이 ("서울 인구 추이 보여줘"): HCX가 짐작한 기간(2015~2024 등)
        # 대신 계산 단계의 기본 기간(자료 마지막 해까지 6개 연도). 위 분기는 값·비교·순위만 본다
        for key in ("start_year", "end_year", "year"):
            if parsed.get(key):
                fix(key, None, "기간을 말하지 않음 → 자료 마지막 해까지 최근 6개 연도")
    if ask_followup.FROM_PAST.search(question) and not SPAN.search(question):
        # "과거부터 지금까지 합계출산율 추이" → 수록 기간 전체 추이
        asked["from_start"] = True
        if parsed.get("start_year"):
            fix("start_year", None, "'과거부터'는 수록 첫해부터")
        # 끝은 질문에 "2010년까지"가 있으면 그 해, 없으면 최신 (HCX가 넣은 끝 연도는 지운다)
        until = int(m[1]) if (m := UNTIL.search(question)) else None
        if parsed.get("end_year") != until:
            fix("end_year", until, "질문의 끝 연도" if until else "'과거부터'는 최신까지")
        if kind != "trend":
            fix("type", "trend", "과거부터의 변화를 물음")
            kind = "trend"
    # "1995년부터 8년 동안"은 끝이 정해진 기간이다 (_apply_time 이 그 해 + N 으로 셌다)
    fixed_span = any(r.get("kind") == "span_from" for r in parsed.get("time_refs") or [])
    if (m := SINCE.search(question)) and not SPAN.search(question) and not fixed_span:
        # "1970년 이후" → 끝은 최신
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
        # "전국 외국인 주민 자녀 중 서울 아이들 비중"을 전국·서울 비교로 읽어도 비중으로 바꾼다
        if regions and (parsed["type"] == "value" or len(regions) == 1):
            if parsed["type"] == "compare":
                fix("type", "value", "전국 중 한 지역의 비중을 물음")
            fix("regions", regions[:1], "전국 대비 비중")
            parsed["share_of"] = "전국"


# 여러 지역 추이는 이만큼까지 계산한다 (시도 17곳을 모두 담는다). 그래프는 charts.MAX_LINES곳까지만
# 겹쳐 그리고, 넘으면 수치로만 보여 주며 이유를 알린다 (charts.omission_reason)
MAX_TREND_REGIONS = 20
MAX_INDICATORS = 3  # 한 질문에서 함께 답하는 지표 수


# 계산 지표 중 남녀로 나눠 볼 수 없는 것 (성비는 그 자체가 남녀 비교, 세대·면적은 성별이 없다)
NO_SEX_SPLIT = frozenset({"성비", "세대당 인구", "인구밀도"})


def _splits_by_sex(q):
    """남녀를 나눠 물은 질문(split_sex)을 남·여 따로 계산할 수 있는가"""
    if q.get("type") not in ("value", "trend", "compare") or q.get("sex") in ("남", "여"):
        return False
    sp = indicators.spec(q.get("indicator"))
    if sp is None or sp["name"] in NO_SEX_SPLIT:
        return False
    if sp.get("method") == "lookup":
        # 성별 칸이 없는 지표(합계출산율)는 나눌 수 없고, 남녀 값만 있는 지표(평균 초혼연령)는
        # 계산 단계가 이미 남녀를 나눠 보여준다
        return any(s.get("by_sex") for s in sp["sources"]) and not lookup.needs_split(sp, "계")
    return True


def compute(q):
    """2~4단계: 질문 분석 결과 → indicators.py(계산 지표)·lookup.py(조회 지표) 결과.
    연도를 말하지 않아 최신 달 값으로 계산했는데 기간 밖이면 연간 자료의 마지막 해로 다시 계산한다
    (총인구는 최신 달이 다음 해라, 등록외국인처럼 연간 자료를 섞는 지표가 '기간 밖'으로 끝났다)"""
    if q.get("split_sex") and _splits_by_sex(q):
        # "서울 남녀 인구 비교": 남·여를 따로 계산해 함께 보여준다 (더하지 않는다)
        return _combine([compute({**q, "sex": s, "split_sex": False}) for s in ("남", "여")])
    result = _compute(q)
    asked = q.get("asked") or {}
    if (
        asked.get("latest")
        and not q.get("year")
        and (result.get("status") == "out_of_range" or _monthly_empty(result))
    ):
        retry = _compute({**q, "asked": {**asked, "latest": False}})
        if retry.get("status") == "ok":
            result = retry
    elif asked.get("latest") and not q.get("year") and result.get("as_of") and "ranking" in result:
        # 최신 달 자료는 행정구역 개편(예: 전남광주통합특별시, 인천 중·동·서구)이 반영돼
        # 지역 목록과 맞지 않는 곳이 순위에서 빠진다. 빠지면 연간 마지막 해로 답한다
        annual = _compute({**q, "asked": {**asked, "latest": False}})
        if (annual.get("total_regions") or 0) > (result.get("total_regions") or 0):
            annual["skipped_month"] = result["as_of"]
            result = annual
    _add_latest_note(q, result)
    return result


def _monthly_empty(result):
    """최신 달 값으로 계산했는데 값이 비었다 (세대당 인구: 최신 달 세대수가 없음)"""
    if result.get("status") != "ok":
        return False
    leaves = [r for r in _leaves(result) if r.get("as_of")]
    vals = [v.get("value") for r in leaves for v in (r.get("values") or r.get("ranking") or [r])]
    return bool(leaves) and all(v is None for v in vals)


def _add_latest_note(q, result):
    """연도 · 기간을 말하지 않아 가장 최근 자료로 답했으면 그 시점을 밝힌다.
    첫 문장의 "2024년"만으로는 그게 가장 최근 자료인지 알 수 없다 (외국인주민 수는 2024년까지,
    총인구는 최신 달까지). 연도를 말하지 않은 질문은 correct가 year · start_year · end_year를
    비운다"""
    if result.get("status") != "ok" or q.get("prediction"):
        return
    for r in _leaves(result):
        if r.get("series"):
            # 추이는 끝 연도만 본다: "2010년부터" · "최근 5년"도 끝은 가장 최근 자료
            if q.get("end_year"):
                continue
            last = r["series"][-1]
            when = last.get("as_of") or f"{last['year']}년"
            name = r.get("indicator") or "이"
            note = f"{name} 자료는 {when}이 가장 최근입니다"
        elif q.get("year"):
            continue
        elif r.get("as_of") or r.get("year"):
            note = f"연도를 말하지 않아 가장 최근 자료인 {_when(r)} 기준으로 답했습니다"
            if month := r.get("skipped_month"):
                note = (
                    f"연도를 말하지 않아 가장 최근 연간 자료인 {_when(r)} 기준으로 답했습니다. "
                    f"최신 달({month}) 자료는 행정구역 개편으로 일부 지역이 달라 쓰지 않았습니다"
                )
            elif r.get("as_of"):
                # 최신 달 값: "○년까지는 연간 자료이고 ○년은 월별 자료의 최신 달 값" 안내와 겹친다
                note += " (KOSIS 월별 자료)"
                r["notes"] = [n for n in r.get("notes", []) if "KOSIS 월별 자료의" not in n]
        else:
            continue
        if note not in (notes := r.setdefault("notes", [])):
            notes.append(note)


CALC_ERROR_TEXT = "계산하는 중에 문제가 생겨 답하지 못했습니다. 잠시 후 다시 질문해 주세요."


def _calc_error(e: Exception) -> dict:
    """계산 중 오류의 답. 오류 원문(표 이름 · SQL · 코드의 칸 이름)은 사용자에게 보이지 않고
    서버 기록에만 남긴다. error_id로 요청 기록(requests.jsonl)과 서버 기록을 이어 찾는다"""
    error_id = uuid4().hex[:8]
    logger.error("계산 중 오류 [%s]", error_id, exc_info=e)
    return {"status": "error", "reason": CALC_ERROR_TEXT, "error_id": error_id}


def _compute(q):
    """compute의 본문 (최신 달 다시 계산 없이)"""
    lookup.refresh_if_reloaded()  # 적재 프로세스가 표를 새로 넣었으면 표 정보 캐시를 비운다
    kind = q.get("type")
    ind = q.get("indicator") or ""
    regions = q.get("regions") or []
    sex = q.get("sex") or "계"
    ages = [tuple(a) for a in q.get("ages") or []] or None
    lo, hi = indicators._data_years()
    year = q.get("year") or hi

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
        # 여러 지표를 함께 물음: 지표마다 따로 계산해 묶는다 (단위가 달라 차트 하나에 그리지 않는다)
        parts = [compute({**q, "indicators": None, "indicator": n, "term": n}) for n in names]
        ok = [p for p in parts if p.get("status") == "ok"]
        if not ok:
            return parts[0]
        result = _combine(ok, by="indicator")
        missed = [n for n, p in zip(names, parts, strict=False) if p.get("status") != "ok"]
        if missed:
            ok[0].setdefault("notes", []).append(
                josa(", ".join(missed), "은는") + " 이 조건으로 답할 수 없어 뺐습니다"
            )
        return result

    # 원인·정책 효과·앞으로의 변화는 통계로 답할 수 없다 (답변 불가). 대신 그 지표의 추이를 볼 수
    # 있는 통계표를 안내한다. 추이는 안내할 표를 고르려고만 계산하고 숫자는 답에 쓰지 않는다
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
    if q.get("cross"):  # 두 지표를 나눈 값 (cross_indicator.py): 두 지표를 각각 계산해 나눈다
        if cond := q.get("unsupported_condition"):
            return {
                "status": "band_mismatch",
                "indicator": ind,
                "reason": f"'{cond}' 조건으로 나뉜 자료가 없어 그 값을 낼 수 없습니다.",
            }
        if q["cross"].get("dynamic"):
            # 한쪽이 사전에 없다: 그 쪽은 통계표 찾기 · 칸 고르기가 필요해 대화 처리(ask_session.
            # _cross_dynamic)가 맡는다. 거기서도 못 하면 지금처럼 통계표 안내로 간다
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
    # 사전에 없는 지표를 통계표에서 바로 만든 정의 (dynamic_indicator.py). 조회 지표와 같이 계산한다
    dyn = q.get("dynamic_spec") if _dynamic_fits(q) else None
    sp = dyn or (None if q.get("unsupported") else indicators.spec(ind))
    if dyn and (dyn.get("ages_in_filters") or dyn.get("ignore_ages")):
        ages = None  # 비율의 분자에 연령이 이미 들어 있거나, 말에서 짐작한 연령을 쓰지 않는 정의
    # 사전에 없는 지표는 인구를 가리킬 때만 인구 구성비로 본다 ("초혼 비율"을 인구 성별 구성으로
    # 읽어 '지역이 없음'으로 끝났다 → 지원하지 않는 지표로 보내 통계표에서 계산하게 한다)
    population = sp["name"] == "총인구" if sp else not ind or bool(PEOPLE_INDICATOR.search(ind))
    if kind == "composition" and population and not q.get("unsupported"):
        # 구성비는 지표가 아니라 총인구를 나눠 보므로 지표 확인을 건너뜀
        if not regions:
            return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
        by = "sex" if q.get("by") == "sex" else "age"
        try:
            if (q.get("asked") or {}).get("latest"):  # "현재 서울 인구 구성" → 최신 달까지
                year = indicators.latest_year(regions[:1])
            return indicators.composition(regions[0], year, by, sex)
        except StatsTableMissing as e:
            if e.tbl_id:  # 적재 안 된 표: ask가 바로 받거나 대기열에 넣는다
                raise
            return _calc_error(e)
        except Exception as e:
            return _calc_error(e)

    if sp is None:  # 지역보다 지표를 먼저 확인
        term = q.get("term") or ind or None
        return {
            "status": "unknown_indicator",
            "indicator": term,
            "supported": list(indicators.specs()),
            **lookup.data_gap(term),
        }

    if (cond := q.get("unsupported_condition")) and not q.get("dynamic_spec"):
        # 지표 사전의 표는 이 조건으로 나뉘어 있지 않다: 버리고 전체 값으로 답하지 않는다
        where = " ".join(regions) if regions else "전국"
        return {
            "status": "band_mismatch",
            "indicator": sp["name"],
            "reason": f"{sp['name']} 자료는 '{cond}' 조건으로 나뉘어 있지 않아 "
            "그 값을 낼 수 없습니다.",
            "alternative": f"대신 조건 없는 {where} {sp['name']}는 확인할 수 있습니다",
        }

    def calc(rs):
        if sp["method"] == "lookup":
            return _compute_lookup(q, sp, kind, rs, year, sex, ages)
        return _compute_population(q, sp["name"], kind, rs, year, sex, ages, hi)

    try:
        if kind == "trend" and len(regions) > 1 and q.get("aggregate") != "sum":
            # 여러 지역의 추이: 지역마다 따로 계산해 묶는다 (화면은 한 꺾은선 차트에 겹쳐 그린다)
            result = _combine([calc([r]) for r in regions[:MAX_TREND_REGIONS]], by="region")
            if result.get("status") == "ok" and len(regions) > MAX_TREND_REGIONS:
                result["split"][0].setdefault("notes", []).append(
                    f"지역이 많아 앞의 {MAX_TREND_REGIONS}곳만 연도별로 봤습니다"
                )
        elif kind == "trend" and len(regions) > 1:
            # 여러 지역의 기간 합계 (예: 수도권 최근 5년 출생아 수 합계). 예전에는 첫 지역만 셌다
            result = _sum_region_periods([calc([r]) for r in regions], q)
        else:
            result = calc(regions)
    except StatsTableMissing as e:
        if e.tbl_id:  # 적재 안 된 표: ask가 바로 받거나 대기열에 넣는다 (_compute_fetching)
            raise
        return _calc_error(e)
    except Exception as e:  # 계산 중 오류도 답변 단계에서 안내
        return _calc_error(e)

    if dyn:  # 사전 지표가 아니라 통계표에서 바로 만든 정의로 계산했다 (다음 턴에서도 표시)
        result["method"] = "dynamic"
    if result.get("status") == "unknown_indicator" and "gap" not in result:
        # 지표는 있지만 질문한 구분(예: 연령)이 표에 없음 → 그 구분의 자료는 수집하지 않음
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
        if indicators._norm(term) != indicators._norm(sp["name"]):  # 예: 합계출생율 → 합계출산율
            for r in _leaves(result):
                note = f"{josa(repr_q(term), '을를')} {ro(sp['name'])} 보고 답했습니다"
                r["notes"] = [note] + r.get("notes", [])
    if (group := q.get("group")) and result.get("status") == "ok" and "values" in result:
        # 묶음 비교·순위는 정의가 갈릴 수 있어 (예: 5대 광역시에 울산 포함 여부) 포함 지역을 밝힌다
        names = ", ".join(v["region"] for v in result["values"])
        scope = f"{_josa(group, '은는')} {names} 기준입니다."
        result["answer_note"] = " ".join(filter(None, [scope, result.get("answer_note")]))
    if refusal:
        return _limit_result(refusal, result)
    return result


def _rank_by_change(q, hi):
    """전년 대비 변화량 순위: 그해와 전년 순위(모든 지역)를 각각 계산해 지역마다 뺀다"""
    year = q.get("year") or hi
    base = {**q, "rank_by": None, "top": None}
    now, before = compute({**base, "year": year}), compute({**base, "year": year - 1})
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
        order="감소 폭이 큰 순" if decrease else "증가 폭이 큰 순",
        base_indicator=now.get("indicator"),
        indicator=f"{now.get('indicator')} 변화량",
        notes=[f"{year - 1}년 → {year}년 변화량으로 순위를 매겼습니다", *(now.get("notes") or [])],
        kind="value",
    )
    return out


LIMIT_REASONS = {
    "causal_question": "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 답할 수 없습니다.",
    "prediction_question": "앞으로의 변화는 지금까지의 통계로 예측해 답할 수 없습니다.",
}


def _limit_result(status, alternative):
    """원인·예측 질문의 답변 불가. alternative: 대신 볼 수 있는 통계(그 지표의 추이) 계산 결과.
    숫자는 쓰지 않고 그 통계와 통계표만 안내한다 (출처·표 카드는 alternative의 표)"""
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
    """바로 만든 지표 정의를 이 조건에 그대로 써도 되는가.
    지표가 바뀌었거나 정의를 만들 때 고정한 조건(성별·연령·구성비)이 바뀌면 쓰지 않는다
    → 사전에 없는 지표로 처리되어 통계표 찾기·바로 계산을 다시 한다"""
    spec = q.get("dynamic_spec")
    if not spec or spec.get("name") != q.get("indicator"):
        return False
    if (after := spec.get("future_after")) is not None:  # 장래추계 표: 미래 연도만
        years = [y for y in (q.get("year"), q.get("start_year"), q.get("end_year")) if y]
        if not years or min(years) <= after:
            return False
    norm = {"sex": lambda v: v or "계", "ages": lambda v: [list(a) for a in v] if v else None}
    return all(
        norm.get(k, lambda v: v)(q.get(k)) == norm.get(k, lambda v: v)(v)
        for k, v in (spec.get("fixed") or {}).items()
    )


def _value_instead(result, name):
    """구성 항목이 없는 지표에 구성비를 물었을 때: 값으로 답하고 그 사실을 알린다.
    첫 질문은 correct가 값 질문으로 고치지만, 대화 중 "구성비로 보여줘"는 correct를 거치지 않는다
    (전에는 "계산 중 오류가 났습니다: 'parts_dim'"으로 답했다)"""
    if result.get("status") == "ok":
        note = f"{_josa(name, '은는')} 나눠 볼 구성 항목이 없어 구성비 대신 값으로 답했습니다"
        for r in _leaves(result):
            r["notes"] = [*(r.get("notes") or []), note]
    return result


def _asked_years(q):
    """질문이 말한 햇수 N ("최근 N년", "N년 동안"). 없으면 None"""
    if n := (q.get("asked") or {}).get("recent"):
        return n
    spans = [r.get("n") for r in q.get("time_refs") or [] if r.get("kind") == "span"]
    return spans[0] if spans and spans[0] else None


def _with_this_year(ind, region, sex, ages, end):
    """끝을 말하지 않은 추이의 끝 해. 연간 자료 다음 해의 최신 달 값이 있으면 그 해까지 붙인다
    (11/22 세진: 2026년 자료가 있으면 넣는다. 지금은 주민등록인구 월별 자료뿐이고,
    연간 자료만 있는 지표(등록외국인 등)는 그대로다).
    indicators.trend 가 그 해 값의 기준 달을 알린다"""
    try:
        _, spec = indicators._get_indicator(ind, ages)
        if spec is None or not indicators._years_of(spec)[2]:
            return end
        return max(end, indicators.latest_year([region], sex))
    except Exception:
        return end


def _compute_population(q, ind, kind, regions, year, sex, ages, hi):
    """계산 지표 (주민등록인구)"""
    if kind == "composition":  # 총인구의 구성비는 compute가 먼저 처리한다
        result = _compute_population(q, ind, "value", regions, year, sex, ages, hi)
        return _value_instead(result, ind)
    if kind in ("value", "trend", "compare") and not regions:
        return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
    if (q.get("asked") or {}).get("latest") and kind in ("value", "compare", "rank"):
        # 연간 자료 다음 해의 최신 달이 있으면 그 해 (순위는 지역 없이 전국으로 확인)
        year = indicators.latest_year([] if kind == "rank" else regions, sex)
    if kind == "value" and len(regions) > 1 and q.get("aggregate") == "sum":
        return _sum_regions(indicators.compare(ind, regions, year, sex, ages), q)
    if kind == "value":
        return indicators.value(ind, regions[0], year, sex, ages)
    if kind == "trend":
        asked = q.get("asked") or {}
        start, end = q.get("start_year") or hi - 5, q.get("end_year") or hi
        open_end = not q.get("end_year")
        if asked.get("recent"):  # "최근 5년" → HCX가 준 기간 대신 자료 마지막 연도 기준 N개년
            start, end, open_end = hi - asked["recent"] + 1, hi, True
        elif asked.get("these_days") and not q.get("start_year"):
            start = hi - 4  # "요즘" → 최근 5개 해
        elif asked.get("from_start") and not q.get("start_year"):
            start = indicators._data_years()[0]  # "과거부터" → 수록 첫해부터
        if open_end:
            end = _with_this_year(ind, regions[0], sex, ages, end)
            # 햇수를 말했으면("최근 5년", "7년 동안") 가장 최근 자료가 있는 해를 끝으로 N개 해
            # (2026년 자료가 있으면 2020~2026, 없으면 2019~2025). 햇수가 없는 "요즘" · "추이"는
            # 연간 자료 기간 뒤에 올해 값을 덧붙인다 (11/22 세진)
            if (n := _asked_years(q)) and end > hi:
                out = indicators.trend(ind, regions[0], end - n + 1, end, sex, ages)
                series = out.get("series") or []
                if out.get("status") == "ok" and series and series[-1]["year"] == end:
                    return out
                # 올해 값이 실제로는 없다 (세대당 인구: 인구는 최신 달이 있어도 세대수는 없다)
                # → 연간 자료 마지막 해를 끝으로 N개 해
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
    """조회 지표 (facts). 남녀 값만 있는 표는 남녀를 따로, 연령 구간이 여럿이면 구간마다 계산"""
    if kind == "composition" and not any(s.get("parts_dim") for s in sp["sources"]):
        result = _compute_lookup(q, sp, "value", regions, year, sex, ages)
        return _value_instead(result, sp["name"])
    if (q.get("asked") or {}).get("latest") and kind in LATEST_KINDS and not q.get("year"):
        # "최근 서울 출생아 수": HCX가 짐작한 연도 대신 그 표의 마지막 해
        last = _lookup_span(sp, regions[0] if regions else "전국", sex)[1]
        if last != 9999:
            year = last
    if not regions and kind in ("value", "trend", "compare", "composition"):
        if not sp.get("national", True):
            return {
                "status": "missing_region",
                "indicator": sp["name"],
                "reason": f"어느 지역의 {sp['name']}인지 질문에 없습니다. "
                "전국 합계는 지역 간 이동이 서로 상쇄되어 의미가 없습니다.",
            }
        regions = ["전국"]
    if kind == "compare" and len(regions) == 1:  # 한 지역의 남녀 비교 등
        y0, y1 = q.get("start_year"), q.get("end_year")
        # "1980년과 2025년 비교"처럼 한 지역의 두 연도를 비교하면 추이 (HCX가 compare로 읽는 경우)
        kind = "trend" if y0 and y1 and y0 != y1 else "value"
    if not any(s.get("by_sex") for s in sp["sources"]):  # 성별 구분이 없는 지표 (예: 합계출산율)
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
    """추이 기간: "최근 N년"은 자료 마지막 연도 기준 N개년
    시작이 수록 기간보다 앞이면 수록 첫해부터.
    "몇 년 연속"처럼 기간이 없으면 수록 기간 전체"""
    asked = q.get("asked") or {}
    lo, hi = _lookup_span(sp, region, sex)
    end = min(q.get("end_year") or year or hi, hi)
    if asked.get("since"):
        end = hi
    if asked.get("recent"):  # HCX가 준 기간 대신 자료 마지막 연도 기준 N개년
        end = hi
        start = end - asked["recent"] + 1
    elif (asked.get("streak") or asked.get("from_start")) and not q.get("start_year"):
        start = lo
    elif asked.get("these_days") and not q.get("start_year"):
        start = end - 4  # "요즘" → 최근 5개 해
    else:
        start = q.get("start_year") or end - 5
    notes = []
    if end < lo:  # 물은 기간 전체가 수록 기간보다 앞 (예: 1970~1980년, 자료는 1990년부터)
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
    # 끝 연도를 말했을 때만 알린다. 말하지 않았으면 year는 주민등록인구 표의 마지막 해라
    # 2024년까지인 표(외국인주민 수)에 묻지도 않은 "2024년까지 봤습니다"가 붙었다
    said_end = q.get("end_year") or q.get("year")
    if said_end and said_end > hi and not asked.get("recent"):
        notes.append(f"{sp['name']} 자료가 {hi}년까지 있어 {hi}년까지 봤습니다")
    result = lookup.trend(sp, region, start, end, sex, ages)
    if result.get("status") == "ok":
        result["notes"] = result["notes"] + notes
        if q.get("aggregate") == "sum" and result.get("kind") == "count":
            return _sum_years(result)
    return result


def _lookup_span(sp, region, sex):
    """그 지역 수준에서 쓸 수 있는 가장 긴 수록 기간"""
    reg = indicators.resolve_region(region)
    level = lookup.REGION_LEVEL.get(reg.get("kind"), "시군구") if reg["status"] == "ok" else "시도"
    spans = []
    for s in sp["sources"]:
        if s.get("national_only") and level != "전국":
            continue
        if lookup.LEVELS[s["level"]] < lookup.LEVELS[level]:
            continue
        eff = lookup._effective(s, sex if sex != "계" or not s.get("by_sex") else "남")
        if eff:
            spans.append(lookup._years(eff))
    if not spans:
        return 0, 9999
    return min(a for a, _ in spans), max(b for _, b in spans)


def _combine(parts, by=None):
    """남녀·연령 구간·지역별로 따로 계산한 결과를 하나로. 하나라도 실패하면 그 결과.
    by="region"이면 여러 지역의 추이 (split_by로 화면·문장이 구분한다)"""
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


def _leaves(result):
    """따로 계산한 결과의 맨 아래 조각들 (지역마다 남녀를 또 나눈 경우까지)"""
    if not result.get("split"):
        return [result]
    return [leaf for p in result["split"] for leaf in _leaves(p)]


def _sum_regions(result, q):
    """여러 지역 값의 합 (예: 수도권 출생아 수, 해운대구 + 수영구 65세 이상 인구)"""
    if result.get("status") != "ok":
        return result
    if result.get("kind", "count") != "count" and result.get("unit") not in ("명", "건"):
        return result  # 비율은 더할 수 없어 지역별 비교로 답한다
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
    """여러 지역의 기간 합계: 지역마다 기간 합계(_sum_years)를 낸 뒤 지역끼리 더한다.
    기간 합계가 안 된 조각(비율 지표, 남녀를 나눈 결과 등)이 있거나 지역마다 기간이 다르면
    더하지 않고 지역별 추이로 답한다"""
    for p in parts:
        if p.get("status") != "ok":
            return p
    summed = all("members" in p and "value" in p for p in parts)
    if not summed or len({p.get("year") for p in parts}) > 1:
        return _combine(parts, by="region")
    values = [{"region": p.get("region"), "value": p["value"]} for p in parts]
    return _sum_regions({**parts[0], "values": values}, q)


def _sum_years(result):
    """기간 합계 (예: 최근 5년간 다문화 출생아 총 몇 명)"""
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
    """전국 대비 비중 (예: 전국 외국인주민 자녀 중 서울 비중)"""
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
    """추이 결과에 전년 대비 변화를 넣는다"""
    kind = result.get("kind") or indicators.INDICATORS[result["indicator"]]["kind"]
    for prev, cur in zip(result["series"], result["series"][1:], strict=False):
        if prev["value"] is None or cur["value"] is None:
            continue
        diff = analysis.change(prev["value"], cur["value"])
        cur["diff_from_prev"] = int(diff) if kind == "count" else round(diff, 3)
        if kind == "count" and prev["value"]:
            cur["rate_from_prev"] = round(analysis.growth_rate(prev["value"], cur["value"]), 2)


def _add_trend_facts(result):
    """추이에서 바로 답할 수 있는 사실
    연평균 변화, 가장 높은·낮은 해, 가장 최근의 연속 양수·음수 기간, 반등 시점"""
    series = [s for s in result["series"] if s["value"] is not None]
    if len(series) < 2:
        return
    c = result["change"]
    years = series[-1]["year"] - series[0]["year"]
    if years:
        c["per_year"] = round(c["diff"] / years, 3)
    first, last = series[0]["value"], series[-1]["value"]
    if "cagr" in c:  # 계산 지표는 반올림 전 값으로 이미 냈다 (indicators.trend)
        pass
    elif years and "rate" in c and c.get("diff_unit") != "%p" and first > 0 and last > 0:
        # 연평균 증감률 (복리): (마지막 ÷ 처음)^(1/햇수) − 1. 증감률이 있는 추이만 (사람 수 · 건수 ·
        # 연령 등). 비율 지표(%p)는 %p와 헷갈려 뺀다. 0 이하 값이 끼면 계산할 수 없다 (11/18)
        c["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
    ordered = sorted(series, key=lambda s: s["value"])
    result["lowest_years"] = [{"year": s["year"], "value": s["value"]} for s in ordered[:5]]
    result["highest_years"] = [{"year": s["year"], "value": s["value"]} for s in ordered[::-1][:5]]
    # 양수(예: 순유입)·음수가 가장 최근에 몇 년 이어졌는지
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
    # 감소(또는 보합) 뒤 처음 증가한 해 중 가장 최근
    v = [s["value"] for s in series]
    turns = [series[i]["year"] for i in range(2, len(v)) if v[i] > v[i - 1] <= v[i - 2]]
    if turns:
        result["rebound_year"] = turns[-1]


# ---- 답변 문장: 질문 유형별 템플릿에 계산 결과를 채운다 ----------------------------------

SEX_WORDS = {"남": "남성 ", "여": "여성 "}

# 지원하지 않는 지표의 구분 (멘토링 피드백: 데이터상 불가능한지, 미구현인지)
GAP_TEXT = {
    "not_implemented": "수집한 통계표({tables})에 관련 자료가 있지만 아직 답하는 기능이 없습니다. ",
    "not_collected": "아직 수집하지 않은 통계입니다. ",
}

# 답변 불가 사유(status)별 안내. reason이 있으면 그 뒤에 붙인다
REFUSAL_GUIDE = {
    "parse_error": "",
    "not_supported_question": "아래 지원 지표로 지역·연도를 넣어 질문해 주세요.",
    "missing_region": "지역을 넣어 다시 질문해 주세요.",
    "unknown_region": "시도나 시군구 이름으로 다시 질문해 주세요.",
    "ambiguous_region": "아래 후보 중 하나로 다시 질문해 주세요.",
    "out_of_range": "수록 기간 안의 연도로 다시 질문해 주세요.",
    "region_not_in_year": "그 지역이 있던 연도로 다시 질문해 주세요.",
    "missing_data": "다른 연도나 지역으로 질문해 주세요.",
    "data_loading": (
        "몇 분 뒤 다시 질문해 주세요. 그 전에는 아래 KOSIS 통계표에서 직접 볼 수 있습니다."
    ),
    "error": "",
}


# 받침에 맞춘 조사 (josa.py). 예전 이름으로도 쓴다 (answer_summary, dynamic_indicator)
_josa, _ro = josa, ro


def repr_q(word):
    """작은따옴표로 감싼 말 ('출산율')"""
    return f"'{word}'"


def _val(v, unit, full=False):
    """문장 속 값. 설명형 단위(예: "인구 1000명당 건")는 목록에서 반복되지 않게 숫자만 쓴다.
    full이면 단위를 풀어 쓴다: "인구 1,000명당 5.4건", "가임여성 1명당 0.799명" """
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
    """설명형 단위의 끝 단위 ("인구 1000명당 건" → "건").
    앞에서 단위를 풀어 쓴 뒤 이어 쓰는 값에 붙인다"""
    return unit.rsplit("당 ", 1)[1] if " " in unit and "당 " in unit else unit


def _meaning(name, v):
    """값의 뜻풀이 문장 (노령화지수처럼 단위만으로 뜻을 알기 어려운 지표)"""
    meaning = indicators.INDICATORS.get(name, {}).get("meaning")
    if not meaning or v is None:
        return ""
    return f" {meaning.format(value=_val(v, ''))}이라는 뜻입니다."


SEX_IN_NAME = re.compile(r"남편|아내|남자|여자|남성|여성")


def _subject(r):
    """성별을 붙인 지표 이름 (예: 여성 고령인구 비율).
    이름에 이미 성별이 있으면 붙이지 않는다 (표의 칸 '남편' → "남성 남편"이 되지 않게)"""
    name = r.get("indicator", "")
    if SEX_IN_NAME.search(name):
        return name
    return SEX_WORDS.get(r.get("sex"), "") + name


# 값이 음수일 때 뜻 (지표 이름 → 문장)
NEGATIVE_MEANING = {
    "자연증가 건수": "사망자가 출생아보다 {value}명 더 많았습니다.",
    "순이동": "전출이 전입보다 {value}명 더 많은 순유출입니다.",
}


def _when(r):
    """답의 시점: 연간 값은 "2025년", 최신 달 값은 "2026년 10월 말" (indicators._mark_monthly)"""
    return r.get("as_of") or f"{r['year']}년"


def _value_text(r):
    name, unit = _subject(r), r.get("unit", "")
    if r["value"] is None:
        return f"{_when(r)} {r['region']}의 {_josa(name, '은는')} 자료가 없습니다."
    meaning = _meaning(r["indicator"], r["value"])
    # 뜻풀이 문장이 있으면 단위는 거기서 설명한다
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
        # 남녀처럼 추이 문장이 여러 개 이어질 때 누구 얘기인지 알 수 있게 주어를 붙인다
        if {d for _, d in steps} == {direction}:
            text += f" 이 기간 동안 {_josa(name, '은는')} 매년 {direction}했습니다."
        elif turn := _turning_year(steps):
            year, before, after = turn
            text += f" {_josa(name, '은는')} {year}년까지 {before}하다가 이후 {after}했습니다."
    return text + _trend_answer(r)


def _turning_year(steps):
    """방향이 한 번만 바뀐 추이(증가 후 감소 등)면 (꺾인 해, 앞 방향, 뒤 방향)"""
    dirs = [d for _, d in steps]
    if "같음" in dirs:
        return None
    changes = [i for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1]]
    if len(changes) != 1:
        return None
    i = changes[0]
    return steps[i - 1][0], dirs[0], dirs[i]


def _trend_answer(r):
    """추이에서 질문이 콕 집어 물은 것 (가장 낮았던 해, 몇 년 연속, 다시 오른 해, 해마다 평균)"""
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
        return (
            f"{_when(r)} {_josa(name, '은는')} {_josa(high['region'], '이가')} "
            f"{low['region']}보다 {gap} 높습니다"
            f"({high['region']} {_val(high['value'], unit)}, "
            f"{low['region']} {_val(low['value'], unit)})."
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
        head = f"{_when(r)} {who} 가장 많이 {moved} {r['level']}"
    if r.get("indicator") == "순이동" and r["order"] == "낮은 순":  # 순유출이 큰 순
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
    if 1 < len(items) < r["total_regions"]:  # "3곳" 처럼 개수를 물은 경우
        return f"{head} {len(items)}곳은 {listed}입니다."
    top = items[0]
    text = f"{_josa(head, '은는')} {top['region']}({_val(top['value'], unit)})입니다."
    if len(items) == r["total_regions"] > 1:  # 전체 순위면 반대쪽 끝도 알려준다
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
    if r.get("subject"):  # 조회 지표 구성비 (출산순위별, 전입 사유별 등)
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
    """여러 지역 추이의 마지막 해 비교 문장 (지역마다 마지막 해가 같을 때만)"""
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
    """남녀·연령 구간·지역을 따로 계산한 결과의 문장. 값 조회 둘이면 어느 쪽이 높은지도"""
    parts = r["split"]
    text = " ".join(summary(p) for p in parts)
    if r.get("split_by") == "region":
        return text + _region_trend_end(r)
    vals = [p.get("value") for p in parts]
    if r.get("split_by") == "indicator":
        return text  # 다른 지표끼리는 크기를 견주지 않는다 (단위·뜻이 다르다)
    if len(parts) == 2 and None not in vals and all("value" in p for p in parts):
        a, b = parts
        if vals[0] != vals[1]:
            hi, lo = (a, b) if vals[0] > vals[1] else (b, a)
            gap = round(abs(vals[0] - vals[1]), 3)
            unit = lookup.diff_unit({"unit": hi["unit"], "kind": hi.get("kind")})
            who = hi["indicator"]
            word = "높습니다"
            same = a.get("indicator") == b.get("indicator")
            if same and {a.get("sex"), b.get("sex")} == {"남", "여"}:
                # 같은 지표를 남녀로 나눈 경우 ("서울 남녀 인구 비교"): 성별로 견준다
                who = {"남": "남성", "여": "여성"}[hi["sex"]]
                word = "많습니다" if hi.get("kind", "count") == "count" else "높습니다"
            text += f" {_josa(who, '이가')} {_val(gap, unit, full=True)} 더 {word}."
    return text


def summary(r):
    """계산 결과 → 답변 문장 (LLM을 거치지 않는다)"""
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


# ---- 숫자 목록: 코드가 계산 결과에서 그대로 만든다 -----------------------------------


def _num(v, unit):
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():  # 추계값 등 2242904.0 → 2,242,904
        v = int(v)
    # 긴 단위(예: "유소년인구 100명당 명")만 띄어 쓴다
    return f"{v:,}{unit}" if len(unit) <= 2 else f"{v:,} ({unit})"


def _signed(v, unit):
    return f"{v:+,}{unit}"


def _body(r):
    """status가 ok인 계산 결과 → 숫자 줄 목록. 값 하나짜리(조회·추계)는 문장에 있어 비운다"""
    unit = r.get("unit", "")
    name = r.get("indicator", "")
    if "split" in r:
        return [line for p in r["split"] for line in _body(p)]
    if "members" in r:  # 여러 지역·연도 합계
        lines = [f"{r['region']} {name} ({r['year']}년)"]
        lines += [f"- {m['region']}: {_num(m['value'], unit)}" for m in r["members"]]
        lines.append(f"- 합계: {_num(r['value'], unit)}")
        return lines
    if "series" in r:  # 추이
        lines = [f"{r['region']} {name} ({r['sex']})"]
        prev_year = None
        for s in r["series"]:
            line = f"- {s['year']}년: {_num(s['value'], unit)}"
            if "diff_from_prev" in s:
                ch = _signed(s["diff_from_prev"], r["change"]["diff_unit"])
                if "rate_from_prev" in s:
                    ch += f", {_signed(s['rate_from_prev'], '%')}"
                # 연도가 비면(예: 1980 → 2022, 조사 연도·표가 다름) "전년"이 아니라 앞 연도를 쓴다
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
        if (rel := c.get("relative_rate")) is not None:  # 비율 지표: %p와 구분해 적는다
            total += f" (비율 자체는 {abs(rel):g}% {'커짐' if rel >= 0 else '작아짐'})"
        lines.append(f"- {c['from_year']}년 → {c['to_year']}년 전체 변화: {total}")
        return lines
    if "values" in r:  # 지역 비교
        lines = [f"{_when(r)} {name} ({r['sex']}, 높은 순)"]
        lines += [f"- {v['region']}: {_num(v['value'], unit)}" for v in r["values"]]
        if r.get("gap") is not None:
            lines.append(
                f"- 가장 높은 {r['highest']}와 가장 낮은 {r['lowest']}의 차이: "
                f"{_num(r['gap'], r['gap_unit'])}"
            )
        return lines
    if "ranking" in r:  # 순위
        lines = [
            f"{_when(r)} {name} {r['level']} 순위 ({r['order']}, 전체 {r['total_regions']}곳 중)"
        ]
        lines += [f"{x['rank']}. {x['region']}: {_num(x['value'], unit)}" for x in r["ranking"]]
        return lines
    if "parts" in r:  # 구성비
        unit = r.get("count_unit", "명")
        title = r.get("subject") or "인구"
        lines = [
            f"{r['region']} {r['year']}년 {title} 구성 ({r['sex']}, 전체 {r['total']:,}{unit})"
        ]
        lines += [f"- {p['label']}: {p['count']:,}{unit} ({p['share']}%)" for p in r["parts"]]
        return lines
    if (ops := r.get("operands")) and all(o.get("value") is not None for o in ops):
        # 두 값을 나눈 지표의 값 답: 나눈 두 값을 보여 준다 (값 자체는 문장에 있다)
        a, b = ops
        times = " × 100" if r.get("kind") == "ratio" else ""
        if (scale := r.get("scale")) and scale != 1:
            times = f" × {scale:,}"  # 두 지표 나누기의 "인구 1,000명당"
        op = r.get("op") or "÷"  # 두 지표의 차이는 "−" (cross_indicator.py)
        return [
            f"- 계산: {a['label']} {a['value']:,}{a['unit']} {op} "
            f"{b['label']} {b['value']:,}{b['unit']}{times} = {_num(r['value'], unit)}"
        ]
    return []


def _source_result(result):
    """출처를 보여줄 계산 결과 (수록 기간 밖이라 추계값을 대신 보여주면 추계 결과)"""
    if result.get("split"):
        first = _source_result(result["split"][0])
        notes = list(dict.fromkeys(n for p in _leaves(result) for n in p.get("notes") or []))
        extra = list(first.get("extra_sources") or [])
        if result.get("split_by") == "indicator":  # 지표마다 다른 표를 썼다
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
    """계산 결과 → 답변 아래에 붙일 숫자·목록·출처·주의사항 (LLM을 거치지 않는다)"""
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
    if src.get("formula"):
        notes.insert(0, f"계산식: {src['formula']}")
    lines += [f"※ {n}" for n in notes]
    if src.get("source"):
        lines.append(f"출처: {src['source']} ({src['basis']})")
    if src.get("url"):
        lines.append(f"통계표: {src['url']}")
    for extra in src.get("extra_sources") or []:  # 기간별로 다른 표를 이어 쓴 경우 (lookup.trend)
        lines.append(f"출처: {extra['source']} ({extra['basis']})")
        lines.append(f"통계표: {extra['url']}")
    return "\n".join(lines)


def sources(result):
    """답변에 쓴 통계표 목록 [{"name", "basis", "tbl_id", "url"}]"""
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
    """최근·최신을 묻는 조회 지표 질문: 쓰는 표마다 KOSIS에 새 연도가 올라왔는지
    확인(하루 한 번)하고 있으면 다시 받는다 (table_cache.refresh_if_stale).
    바로 못 받으면 갱신 작업을 대기열에 넣고 지금 자료로 답하면서 그 사실을 주의사항으로 돌려준다.

    계산 지표(총인구 등)는 전처리로 만든 mart.population을 읽어 표를 다시 받아도 반영되지 않으므로
    확인하지 않는다."""
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


MAX_FETCH = 3  # 질문 하나에서 바로 받는 표 수 상한 (지표 하나가 표 여러 개를 쓸 수 있음)


async def _compute_fetching(parsed, trace):
    """계산 → 적재 안 된 표가 나오면 바로 받아 넣고 다시 계산 (table_cache.py)

    큰 표·받기 실패·시간 초과면 대기열에 넣고 data_loading으로 답한다 (_loading_result).
    trace["fetched"]: 이번 질문에서 바로 받은 표와 결과
    """
    for _ in range(MAX_FETCH + 1):
        try:
            return await run_in_threadpool(compute, parsed)
        except StatsTableMissing as e:
            error = e
        fetched = trace.setdefault("fetched", [])
        if len(fetched) >= MAX_FETCH or not error.tbl_id:
            break
        if any(f["tbl_id"] == error.tbl_id for f in fetched):
            break  # 넣었는데도 같은 표가 없다고 나오면 다시 받지 않는다
        try:
            info = await load_queue.table_info(error.tbl_id)
        except (load_queue.UnknownTable, SQLAlchemyError):
            break  # _loading_result가 원래 오류를 올린다
        step = {"tbl_id": error.tbl_id}
        outcome = await table_cache.fetch_now(error.tbl_id, info["org_id"], step)
        fetched.append(step)
        if outcome != "loaded":
            break
    return await _loading_result(error)


async def _loading_result(error):
    """수치가 아직 적재되지 않은 표: 적재 대기열에 넣고 data_loading으로 답한다

    표를 알 수 없거나(DB 자체가 비어 있음) 대기열에 넣지 못하면 원래 오류를 그대로 올린다
    (/ask가 503으로 답함). 넣지도 못했는데 "준비 중"이라고 답하지 않기 위해서다.
    """
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
    """질문 하나를 끝까지 처리. status, answer, parsed, result, sources, trace를 반환한다.
    status: ok 또는 답변 불가 사유 (indicators.py 결과의 status)
    trace: HCX 원본 출력과 단계별 시간(ms). 요청 로그에만 쓰고 API 응답에는 넣지 않는다
    adjust: 해석한 조건을 계산 전에 고치는 함수
            (대화에서 앞 질문의 조건을 이어받을 때, ask_session.py)"""
    trace = {}
    start = time.perf_counter()
    parsed = await understand(question, trace)
    trace["understand_ms"] = round((time.perf_counter() - start) * 1000)
    if adjust is not None:
        parsed = adjust(parsed)
    return await answer(parsed, trace, question)


def _add_data_end_note(parsed, result, today=None):
    """'최근 5년' · 'N년 동안' · 'N년 전보다' · '지금까지'는 오늘이 아니라 자료 마지막 해에서 센다.
    올해 자료가 아직 없으면 어느 해까지의 자료로 어떻게 셌는지 밝힌다 (11/22 세진):
      최근 N년      가장 최근 자료가 2025년까지라 2021~2025년(5개 연도)으로 계산했습니다
      자료가 모자람  자료가 2023~2025년만 있어 3개 연도로 계산했습니다
      N년 전보다    가장 최근 자료가 2025년까지라 2015년과 2025년을 비교했습니다
      N년 동안 등   가장 최근 자료가 2025년까지라 2015~2025년으로 계산했습니다
    추이(연도별 값)일 때만. 끝이 올해면(올해 자료가 있으면) 붙이지 않는다"""
    if result.get("status") != "ok":
        return
    today = today or date.today()
    refs = [r for r in parsed.get("time_refs") or [] if r.get("kind") in PERIOD_KINDS]
    recent = _asked_years(parsed)  # 최근 N년 · N년 동안
    kinds = {r["kind"] for r in refs}
    if not recent and not kinds - {"span_from"}:
        return  # '○년부터 N년 동안'은 사용자가 정한 기간이다
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
    holder = parts[0]  # 나눠 계산한 답은 첫 결과의 안내를 모아 보여 준다 (_source_result)
    holder["notes"] = [*(holder.get("notes") or []), note]


async def answer(parsed, trace=None, question=""):
    """해석된 조건(parsed)으로 조회·계산해 답을 만든다. ask의 2단계부터.
    대화에서 조건만 바꿔 다시 답할 때는 HCX 해석 없이 여기를 바로 부른다 (question은 비워 둔다)"""
    trace = {} if trace is None else trace
    start = time.perf_counter()
    if ask_followup.needs_year(question) and indicators.spec(parsed.get("indicator") or ""):
        # "과거와 최근 비교"처럼 연도 없이 과거만 말하면 계산하지 않고 되묻는다.
        # 지표 사전에 없는 지표면 되묻지 않고 계산으로 보내 지원하지 않는 지표라고 먼저 알린다
        result = await run_in_threadpool(ask_followup.missing_year, parsed)
    else:
        notes = await _refresh_recent(question, parsed, trace)
        # 통계 DB 조회는 동기 함수라 이벤트 루프를 막지 않도록 스레드에서 실행.
        # 적재 안 된 표는 바로 받아 넣고 다시 계산한다 (받는 시간도 compute_ms에 들어감)
        result = await _compute_fetching(parsed, trace)
        if notes and result.get("status") == "ok":
            result["notes"] = list(result.get("notes") or []) + notes
        _add_data_end_note(parsed, result)
    trace["compute_ms"] = round((time.perf_counter() - start) * 1000)
    if not parsed.get("dynamic_spec"):  # 표 칸으로 계산한 답은 dynamic_indicator._try가 남긴다
        flow.add("사전 계산", result.get("status"), flow_result(result))
    return {
        "status": result.get("status"),
        "answer": f"{summary(result)}\n\n{render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": sources(result),
        "trace": trace,
    }


def flow_result(result):
    """처리 흐름에 남길 계산 결과 한 줄 (값 · 표, 못 했으면 이유)"""
    tbl = ",".join(s.get("tbl_id") or "" for s in sources(result))[:60]
    if result.get("status") == "ok":
        value = result.get("value")
        shown = f"값={value}{result.get('unit') or ''}" if value is not None else "값 여러 개"
        return f"{result.get('indicator') or ''} {shown} 표={tbl or '-'}".strip()
    return str(result.get("reason") or result.get("indicator") or "")


# ---- 요청 기록 ---------------------------------------------------------------------------
# 질문할 때 KOSIS를 부르지 않는다. 대신 DB에 없어서 답하지 못한 질문을 모아 두고,
# 다음 회차 배치 수집에서 추가할 데이터를 정할 때 본다 (scripts/missing_requests.py)
# 모든 요청은 따로 남겨, 틀린 답이 질문 해석(HCX·보정) 문제인지 조회·계산 문제인지 추적한다
# (scripts/request_log.py)

DATA_GAP = {
    "unknown_indicator",
    "out_of_range",
    "region_not_in_year",
    "missing_data",
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
    """DB에 없는 데이터 때문에 답하지 못한 질문이면 한 줄(JSON) 추가. 기록했으면 True"""
    if res.get("status") not in DATA_GAP:
        return False
    result = res.get("result") or {}
    record = {
        "at": _now(),
        "question": question,
        "status": res["status"],
        "parsed": res.get("parsed"),
        "gap": result.get("gap"),  # 미구현(not_implemented) / 수집 안 함(not_collected)
        "related": result.get("related"),
    }
    _append(path or MISSING_LOG, record)
    return True


def log_request(question, res=None, error=None, elapsed_ms=None, path=None, user_id=None):
    """모든 /ask 요청을 한 줄(JSON)로 남긴다. 실패한 요청(res 없음)은 error에 원인.
    user_id는 로그인한 사용자 id (손님이면 None)

    단계별로 보면 원인을 나눌 수 있다:
      hcx_raw     HCX가 돌려준 원문 (JSON을 못 읽었으면 여기서 원인 확인)
      parsed      해석 결과 + corrections(코드가 고친 항목과 이유)
      result      조회·계산 결과와 status
      fetched     적재 안 된 표를 바로 받았는지: 표, 결과(loaded·too_big·failed·timeout),
                  요청 수, 시간
    """
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
    # 캐시를 썼는지: 질문 이해(같은 질문이면 HCX 0회), 계산 방법(통계표 찾기 건너뜀)
    if trace.get("understand_cached"):
        record["understand_cached"] = True
    if trace.get("flow"):  # 처리 흐름 (app/services/flow.py, scripts/show_flow.py로 본다)
        record["flow"] = trace["flow"]
    # LLM 호출 (app/services/llm_calls.py): 단계 · 토큰 · 시간 · 다시 시도
    if trace.get("llm_calls"):
        record["llm_calls"] = trace["llm_calls"]
    if trace.get("plan_cache"):
        record["plan_cache"] = trace["plan_cache"]
    if trace.get("dynamic"):  # 바로 계산이 본 표와 건너뛴 이유 (scripts/refusal_reasons.py로 센다)
        record["dynamic"] = trace["dynamic"]
    if trace.get("refreshed"):  # 최근·최신 질문에서 KOSIS 새 연도를 확인한 결과
        record["refreshed"] = trace["refreshed"]
    if "understand_usage" in trace:
        record["understand_usage"] = trace["understand_usage"]
        record["understand_max_completion_tokens"] = trace.get("understand_max_completion_tokens")
    _append(path or REQUEST_LOG, record)
