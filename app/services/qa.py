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

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from app.prompts.qa import PARSE_SYSTEM
from app.services import analysis, indicators, lookup
from app.services.hcx_client import HcxClient

MODEL = "HCX-007"


async def _chat(prompt, system, temperature=0.1):
    """HCX-007 호출. 설정은 기존 pipeline/clova.py 기본값과 같다"""
    result = await HcxClient().chat(
        MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        thinking={"effort": "low"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=5120,
        temperature=temperature,
        repetitionPenalty=1.1,
    )
    return result.content


async def understand(question, trace=None):
    """1단계: 질문 → dict. JSON을 못 읽으면 type이 parse_error
    trace(dict)를 주면 HCX 원본 출력을 담는다 (요청 로그용)"""
    lo, hi = await run_in_threadpool(indicators._data_years)
    system = PARSE_SYSTEM.replace("{INDICATORS}", _indicator_list()).replace("{LATEST}", str(hi))
    text = await _chat(question, system)
    if trace is not None:
        trace["hcx_raw"] = text
    objects = _json_objects(text)
    if not objects:
        return {"type": "parse_error", "raw": text}
    parsed = objects[0]
    # 남녀를 나눠 JSON을 두 번 쓴 경우 → 성별 구분 없이 (남녀를 함께 보여준다)
    if len({json.dumps(o.get("sex"), ensure_ascii=False) for o in objects}) > 1:
        parsed["sex"] = "계"
    return await run_in_threadpool(correct, question, parsed)


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
# 긴 표현부터 찾는다 ("재작년" 안에 "작년"이 있음)
RELATIVE_YEARS = [("재작년", -2), ("지난해", -1), ("작년", -1), ("올해", 0), ("금년", 0)]
NO_REGION_TYPES = {"rank", "other", "parse_error"}

# 연령 구간: "20대", "6~11세", "65세 이상", "15세 미만" (뒤에 '광역시'가 오는 "5대 광역시"는 제외)
AGE_RANGE = re.compile(r"(?:만\s*)?(\d+)\s*세?\s*[~\-]\s*(\d+)\s*세")
AGE_OVER = re.compile(r"(?:만\s*)?(\d+)\s*세\s*이상")
AGE_UNDER = re.compile(r"(?:만\s*)?(\d+)\s*세\s*미만")
AGE_DECADE = re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))")
# 여러 지역 묶음
# "충청도"는 묶음이 아니라 충북·충남 중 어디인지 되묻는다 (indicators.AMBIGUOUS_NAMES)
REGION_GROUPS = {
    "수도권": ["서울", "인천", "경기"],
    "충청권": ["대전", "세종", "충북", "충남"],
    "호남권": ["광주광역시", "전북", "전남"],
    "영남권": ["부산", "대구", "울산", "경북", "경남"],
    "6대 광역시": ["부산", "대구", "인천", "광주광역시", "대전", "울산"],
    "5대 광역시": ["부산", "대구", "인천", "광주광역시", "대전"],
}
# "합계출산율"의 합계는 합산이 아니다
SUM_WORDS = re.compile(
    r"합계(?!\s*출[산생])|합친|합쳐|합하면|총\s*몇|모두\s*더|더하면|총합|총\s*인원"
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
}
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
TOP_N = re.compile(r"(상위|하위)\s*(\d+)|(\d+)\s*(곳|개|위까지)(?!\s*\S*\s*중)")
RECENT = re.compile(r"최근\s*(\d+)\s*년")
SPAN = re.compile(r"((?:19|20)\d{2})\s*년?\s*(대비|부터|에서)\s*((?:19|20)\d{2})\s*년")
SINCE = re.compile(r"((?:19|20)\d{2})\s*년\s*(이후|이래|부터)(?!.*까지)")
NATIONWIDE = re.compile(r"전국|우리나라|대한민국|한국\s*(전체|의|은|는)|국내\s*(전체)?")
CAUSAL = re.compile(r"효과가?\s*(있|없|컸)|효과적|원인|왜\s|영향을?\s*(줬|주|미쳤|끼쳤)")
# "가장 많이 빠져나간 시군구", "합계출산율이 가장 높은 시도" → 순위
RANK_ASK = re.compile(
    r"(가장|제일|꼴찌|최하위)\S*\s*(\S+\s*){0,3}?(시군구|시도|지역|곳|구|시)(는|가|은|이|를|\s|$|\?)"
)
# 별칭으로 못 찾은 지표 표현 (질문 표현 → 지표 이름)
INDICATOR_PATTERNS = [
    (re.compile(r"(아이|아기|신생아)\S*\s*(\S+\s*){0,3}태어"), "출생아 수"),
    (re.compile(r"결혼(을|한)?\s*(\S+\s*){0,2}(많이|적게)"), "혼인 건수"),
]
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
    r"|몇\s*(명|건|가구)"
)


def extract_ages(question):
    """질문 속 연령 구간 → [[시작, 끝 또는 None], ...] (나온 순서, 중복 제거)"""
    found = []
    for m in AGE_RANGE.finditer(question):
        found.append((m.start(), [int(m[1]), int(m[2])]))
    for m in AGE_OVER.finditer(question):
        found.append((m.start(), [int(m[1]), None]))
    for m in AGE_UNDER.finditer(question):
        found.append((m.start(), [0, int(m[1]) - 1]))
    covered = [m.span() for p in (AGE_RANGE, AGE_OVER, AGE_UNDER) for m in p.finditer(question)]
    for m in AGE_DECADE.finditer(question):
        if int(m[1]) % 10 == 0 and not any(a <= m.start() < b for a, b in covered):
            found.append((m.start(), [int(m[1]), int(m[1]) + 9]))
    bands = []
    for _, band in sorted(found, key=lambda x: x[0]):
        if band not in bands:
            bands.append(band)
    # "20대(20~29세)"처럼 같은 구간을 두 번 쓰면 하나로
    return bands


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


def correct(question, parsed, today=None):
    """같은 질문도 실행마다 HCX 해석이 달라서, 질문 원문에서 확실히 알 수 있는 것은 코드가 맞춘다.

    - 지표: 질문에 지표 이름·별칭이 있으면 그 지표 (예: "고령화율"을 노령화지수로 해석한 경우)
    - 지표 바꿔치기 막기: 질문에 근거가 없는 계산 지표, "몇 명"을 물었는데 비율 지표면 거절
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
    if parsed.get("sex") not in (None, "", "계", "남", "여"):  # 예: ["남", "여"]
        fix("sex", "계", "성별 값 정리")
    if parsed.get("level") in ("구", "시군구별", "구별", "군", "시군"):
        fix("level", "시군구", "지역 수준 정리")
    for area, sigungu in SUB_AREAS.items():
        # 뒤에 구·시·군이 붙으면 행정구역 이름 (예: 해운대구, 분당구)
        if re.search(re.escape(area) + r"(?!\s*[구시군])", question):
            parsed["sub_area"] = [area, sigungu]
            break
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
            names |= {name for pattern, name in INDICATOR_PATTERNS if pattern.search(question)}
        name = _pick_indicator(question, names)
        current = indicators.canonical_name(parsed.get("indicator") or "")
        if name and current != name:
            # "인구"만 찾았고 HCX가 다른 지표를 골랐으면 그대로 둔다 (예: "인구에서 고령층 비중")
            # 단, 연령 구간 인구 수를 물었으면 총인구 (예: "65세 이상 인구는?")
            if name != "총인구" or current is None or (ages and not PCT_ASK.search(question)):
                fix("indicator", name, "질문의 지표 표현")
                parsed.pop("unsupported", None)
        _guard_indicator(question, parsed, names, fix)
        kind = parsed.get("type")

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

    _read_question(question, parsed, fix)

    genders = [sex for sex, pattern in GENDER_WORDS.items() if pattern.search(question)]
    if len(genders) == 1 and parsed.get("sex") in (None, "", "계"):
        fix("sex", genders[0], "질문의 성별 표현")
    if len(genders) == 2 and parsed.get("sex") in ("남", "여"):  # "남자랑 여자 중 누가"
        fix("sex", "계", "남녀를 함께 물음")
    if kind == "composition" and parsed.get("by") != "sex" and SEX_SPLIT.search(question):
        fix("by", "sex", "질문의 남녀 구성 표현")

    _ground_regions(question, parsed, fix)

    if NATIONWIDE.search(question) and kind not in NO_REGION_TYPES:
        regions = parsed.get("regions") or []
        if not regions or all(r in NATIONAL_NAMES for r in regions):
            fix("regions", ["전국"], "질문의 전국 표현")

    for word, offset in RELATIVE_YEARS:
        if word in question and not PREV_YEAR.search(question):
            year = today.year + offset
            if kind not in ("trend", "other", "parse_error") and parsed.get("year") != year:
                fix("year", year, f"'{word}' = {today.year}년 기준")
            break

    if fixes:
        parsed["corrections"] = fixes
    return parsed


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
        parsed["ungrounded_region"] = r
        return


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


def _guard_indicator(question, parsed, names, fix):
    """지원 지표로 바꿔서 답하지 않게 한다 (0회차: 다른 지표로 답한 8문항)
    - 계산 지표(고령인구 비율 등)는 질문에 그 지표 표현이 있어야 한다
    - "몇 명"을 물었는데 비율 지표면 맞지 않는다
    맞지 않으면 연령 구간 인구 수로 답할 수 있으면 총인구, 아니면 지원하지 않는 지표"""
    sp = indicators.spec(parsed.get("indicator"))
    if sp is None:
        return
    ages = parsed.get("ages")
    k = lookup.kind_of(sp)
    asks_count = COUNT_ASK.search(question) and not PCT_ASK.search(question)
    ungrounded = sp["method"] == "computed" and sp["name"] != "총인구" and sp["name"] not in names
    if (k in ("ratio", "index") and asks_count) or ungrounded:
        if ages and "인구" in question and asks_count:
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
    if regions and level == "시군구":
        fix("sido", regions[0], "지역별 목록의 범위")
        fix("regions", [], "지역별 목록")
    if re.search(r"(별|마다)", question) and parsed.get("top"):
        fix("top", None, "지역별 목록은 전체")


def _read_question(question, parsed, fix):
    """질문 원문에서 바로 읽을 수 있는 조건 (지역 묶음, 합계, 극값 연도, 연속, 반등 등)"""
    kind = parsed.get("type")
    for word, members in REGION_GROUPS.items():
        if word in question:
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
    if (m := SPAN.search(question)) and kind in ("value", "trend", "causal"):
        start, end = int(m[1]), int(m[3])
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
        asked["recent"] = int(m[1])
    if (m := SINCE.search(question)) and not SPAN.search(question):  # "1970년 이후" → 끝은 최신
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
        and parsed.get("type") == "value"
    ):
        regions = [r for r in parsed.get("regions") or [] if r != "전국"]
        regions = regions or [r for r in indicators.find_regions(question) if r != "전국"]
        if regions:
            fix("regions", regions[:1], "전국 대비 비중")
            parsed["share_of"] = "전국"


def compute(q):
    """2~4단계: 질문 분석 결과 → indicators.py(계산 지표)·lookup.py(조회 지표) 결과"""
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

    causal = kind == "causal"
    if causal:  # 답변 불가 사례 4: 인과 판단 없이 수치 변화만 제시
        kind = "trend" if regions else "rank"
        if not indicators.spec(ind) or q.get("unsupported"):  # 지표가 없으면 전국 인구 추이
            ind, regions, kind = "총인구", ["전국"], "trend"
            q = {**q, "unsupported": False, "start_year": None, "end_year": None}

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
    sp = None if q.get("unsupported") else indicators.spec(ind)
    population = sp is None or sp["name"] == "총인구"
    if kind == "composition" and population and not q.get("unsupported"):
        # 구성비는 지표가 아니라 총인구를 나눠 보므로 지표 확인을 건너뜀
        if not regions:
            return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
        by = "sex" if q.get("by") == "sex" else "age"
        try:
            return indicators.composition(regions[0], year, by, sex)
        except Exception as e:
            return {"status": "error", "reason": f"계산 중 오류가 났습니다: {e}"}

    if sp is None:  # 지역보다 지표를 먼저 확인
        return {
            "status": "unknown_indicator",
            "indicator": q.get("term") or ind or None,
            "supported": list(indicators.specs()),
        }

    try:
        if sp["method"] == "lookup":
            result = _compute_lookup(q, sp, kind, regions, year, sex, ages)
        else:
            result = _compute_population(q, sp["name"], kind, regions, year, sex, ages, hi)
    except Exception as e:  # 계산 중 오류도 답변 단계에서 안내
        return {"status": "error", "reason": f"계산 중 오류가 났습니다: {e}"}

    for r in result.get("split") or [result]:
        if r.get("series"):
            _add_yearly_change(r)
            _add_trend_facts(r)
        r["asked"] = {**(q.get("asked") or {}), "top": q.get("top")}
    if result.get("status") == "out_of_range":
        result.setdefault("asked_year", q.get("end_year") or year)
    term = q.get("term")
    if result.get("status") == "ok" and term and indicators.canonical_name(term) == sp["name"]:
        if indicators._norm(term) != indicators._norm(sp["name"]):  # 예: 합계출생율 → 합계출산율
            for r in result.get("split") or [result]:
                note = f"'{term}'을(를) {sp['name']}(으)로 보고 답했습니다"
                r["notes"] = [note] + r.get("notes", [])
    if causal:
        result["causal_question"] = (
            "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 수치 변화만 보여드립니다."
        )
    return result


def _compute_population(q, ind, kind, regions, year, sex, ages, hi):
    """계산 지표 (주민등록인구)"""
    if kind in ("value", "trend", "compare") and not regions:
        return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
    if kind == "value" and len(regions) > 1 and q.get("aggregate") == "sum":
        return _sum_regions(indicators.compare(ind, regions, year, sex, ages), q)
    if kind == "value":
        return indicators.value(ind, regions[0], year, sex, ages)
    if kind == "trend":
        start, end = q.get("start_year") or hi - 5, q.get("end_year") or hi
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
        )
    return {"status": "parse_error", "reason": f"알 수 없는 질문 유형: {kind}"}


def _compute_lookup(q, sp, kind, regions, year, sex, ages):
    """조회 지표 (facts). 남녀 값만 있는 표는 남녀를 따로, 연령 구간이 여럿이면 구간마다 계산"""
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
        kind = "value"
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
    elif asked.get("streak") and not q.get("start_year"):
        start = lo
    else:
        start = q.get("start_year") or end - 5
    notes = []
    if start < lo:
        notes.append(f"{sp['name']} 자료가 {lo}년부터 있어 {lo}년부터 봤습니다")
        start = lo
    if (q.get("end_year") or year or hi) > hi and not asked.get("recent"):
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


def _combine(parts):
    """남녀·연령 구간별로 따로 계산한 결과를 하나로. 하나라도 실패하면 그 결과"""
    for p in parts:
        if p.get("status") != "ok":
            return p
    return {
        **{k: parts[0][k] for k in ("source", "basis", "tbl_id", "url") if k in parts[0]},
        "status": "ok",
        "split": parts,
    }


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
            "value": round(result["value"] / total["value"] * 100, 2),
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
    "error": "",
}


def _josa(word, pair):
    """받침이 있으면 pair[0], 없으면 pair[1]을 붙인다. pair 예: "은는", "이가" """
    base = re.sub(r"\([^()]*\)$", "", word) if word.endswith(")") else word
    last = base[-1] if base else ""
    if "가" <= last <= "힣":
        return word + (pair[0] if (ord(last) - ord("가")) % 28 else pair[1])
    return f"{word}{pair[0]}({pair[1]})"


def _ro(word):
    """(으)로: 받침이 있으면(ㄹ 제외) '으로'"""
    last = word[-1] if word else ""
    if "가" <= last <= "힣" and (ord(last) - ord("가")) % 28 not in (0, 8):
        return word + "으로"
    return word + "로"


def _val(v, unit):
    """문장 속 값. 설명형 단위(예: "유소년인구 100명당 명")는 뜻풀이 문장으로 따로 쓰므로 뺀다"""
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if " " in unit else f"{v:,}{unit}"


def _meaning(name, v):
    """값의 뜻풀이 문장 (노령화지수처럼 단위만으로 뜻을 알기 어려운 지표)"""
    meaning = indicators.INDICATORS.get(name, {}).get("meaning")
    if not meaning or v is None:
        return ""
    return f" {meaning.format(value=_val(v, ''))}이라는 뜻입니다."


def _subject(r):
    """성별을 붙인 지표 이름 (예: 여성 고령인구 비율)"""
    return SEX_WORDS.get(r.get("sex"), "") + r.get("indicator", "")


# 값이 음수일 때 뜻 (지표 이름 → 문장)
NEGATIVE_MEANING = {
    "자연증가 건수": "사망자가 출생아보다 {value}명 더 많았습니다.",
    "순이동": "전출이 전입보다 {value}명 더 많은 순유출입니다.",
}


def _value_text(r):
    name, unit = _subject(r), r.get("unit", "")
    if r["value"] is None:
        return f"{r['year']}년 {r['region']}의 {_josa(name, '은는')} 자료가 없습니다."
    text = (
        f"{r['year']}년 {r['region']}의 {_josa(name, '은는')} {_val(r['value'], unit)}입니다."
        + _meaning(r["indicator"], r["value"])
    )
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
        f"{r['region']}의 {_josa(name, '은는')} {c['from_year']}년 {_val(first, unit)}, "
        f"{c['to_year']}년 {_val(last, unit)}입니다. "
    )
    if c["diff"] == 0:
        return text + f"{c['from_year']}년과 같습니다."
    direction = "증가" if c["diff"] > 0 else "감소"
    text += f"{c['from_year']}년 대비 {_val(abs(c['diff']), c['diff_unit'])} {direction}했습니다"
    if "rate" in c:
        text += f"(증감률 {c['rate']:+}{c['rate_unit']})"
    text += "."
    yearly = {
        "증가" if s["diff_from_prev"] > 0 else "감소" if s["diff_from_prev"] < 0 else "같음"
        for s in r["series"]
        if "diff_from_prev" in s
    }
    if len(r["series"]) > 2 and yearly == {direction}:
        text += f" 이 기간 동안 매년 {direction}했습니다."
    return text + _trend_answer(r)


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
        return f"{r['year']}년 {name} 값이 있는 지역이 둘 이상이 아니어서 비교할 수 없습니다."
    high, low = valid[0], valid[-1]
    ratio = r.get("ratio") if (r.get("asked") or {}).get("ratio") else None
    if ratio:
        times = ratio["value"]
        tail = f"{times}배입니다." if times >= 1 else f"{times}배로 더 적습니다."
        by_region = {v["region"]: v["value"] for v in r["values"]}
        a, b = ratio["a"], ratio["b"]
        return (
            f"{r['year']}년 {a}의 {_josa(name, '은는')} {b}의 {tail}"
            f" ({a} {_val(by_region[a], unit)}, {b} {_val(by_region[b], unit)})"
        )
    if r["gap"] == 0:
        return (
            f"{r['year']}년 {_josa(name, '은는')} {_josa(high['region'], '과와')} "
            f"{_josa(low['region'], '이가')} 같습니다."
        )
    gap = _val(r["gap"], r["gap_unit"])
    if len(valid) == 2:
        return (
            f"{r['year']}년 {_josa(name, '은는')} {_josa(high['region'], '이가')} "
            f"{low['region']}보다 {gap} 높습니다"
            f"({high['region']} {_val(high['value'], unit)}, "
            f"{low['region']} {_val(low['value'], unit)})."
        )
    return (
        f"{r['year']}년 {_josa(name, '이가')} 가장 높은 지역은 "
        f"{high['region']}({_val(high['value'], unit)}), 가장 낮은 지역은 "
        f"{low['region']}({_val(low['value'], unit)})이며, 차이는 {gap}입니다."
    )


def _rank_text(r):
    name, unit = _subject(r), r.get("unit", "")
    items = r["ranking"]
    if not items:
        return f"{r['year']}년 {name} 순위를 매길 자료가 없습니다."
    order = "낮은" if r["order"] == "낮은 순" else "높은"
    if unit in ("명", "건"):
        order = {"낮은": "적은", "높은": "많은"}[order]
    head = f"{r['year']}년 {_josa(name, '이가')} 가장 {order} {r['level']}"
    if r.get("indicator") == "순이동" and r["order"] == "낮은 순":  # 순유출이 큰 순
        head = f"{r['year']}년 인구가 가장 많이 빠져나간(순유출이 가장 큰) {r['level']}"
    listed = ", ".join(f"{x['region']}({_val(x['value'], unit)})" for x in items)
    if target := r.get("target"):
        if target["rank"] is None:
            return f"{r['year']}년 {target['region']}의 {name} 값이 없어 순위를 매길 수 없습니다."
        return (
            f"{r['year']}년 {target['region']}의 {_josa(name, '은는')} "
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
            f"{r['year']}년 {r['region']}의 {r['subject']} 중 {_josa(p['label'], '은는')} "
            f"{p['count']:,}{unit}, {p['share']}%입니다."
            for p in focus
        )
        return (lead + " " if lead else "") + (
            f"{r['year']}년 {r['region']}의 {r['subject']} 구성(전체 {r['total']:,}{unit})은 "
            f"{listed}입니다. 가장 큰 비중은 {biggest['label']}입니다."
        )
    by = "성별" if r["by"] == "sex" else "연령대별"
    whom = SEX_WORDS.get(r.get("sex"), "")
    listed = ", ".join(f"{p['label']} {p['share']}%" for p in r["parts"])
    biggest = max(r["parts"], key=lambda p: p["share"])
    return (
        f"{r['year']}년 {r['region']} {whom}인구 {r['total']:,}명의 {by} 구성은 {listed}입니다. "
        f"가장 큰 비중은 {biggest['label']}입니다."
    )


def _refusal_text(r):
    status = r.get("status")
    if status == "unknown_indicator":
        if r.get("reason"):
            return f"{r['reason']} 아래 지원 지표로 다시 질문해 주세요."
        what = f"'{r['indicator']}'은(는)" if r.get("indicator") else "질문하신 지표는"
        return f"{what} 아직 지원하지 않는 지표입니다. 아래 지원 지표로 다시 질문해 주세요."
    guide = REFUSAL_GUIDE.get(status, "")
    if status == "unknown_region" and r.get("alternative"):
        guide = f"속한 시군구({r['alternative']})로 다시 질문해 주세요."
    if status == "out_of_range" and r.get("available_years"):
        lo, hi = r["available_years"]
        asked = r.get("asked_year")
        near = lo if isinstance(asked, int) and asked < lo else hi
        guide = f"{lo}~{hi}년 사이 연도로 다시 질문해 주세요 (가장 가까운 {near}년 자료가 있음)."
    parts = [r.get("reason") or "답변할 수 없는 질문입니다.", guide]
    return " ".join(p for p in parts if p)


def _split_text(r):
    """남녀·연령 구간을 따로 계산한 결과의 문장. 값 조회 둘이면 어느 쪽이 높은지도"""
    parts = r["split"]
    text = " ".join(summary(p) for p in parts)
    vals = [p.get("value") for p in parts]
    if len(parts) == 2 and None not in vals and all("value" in p for p in parts):
        a, b = parts
        if vals[0] != vals[1]:
            hi, lo = (a, b) if vals[0] > vals[1] else (b, a)
            gap = round(abs(vals[0] - vals[1]), 3)
            unit = lookup.diff_unit({"unit": hi["unit"], "kind": hi.get("kind")})
            text += f" {_josa(hi['indicator'], '이가')} {_val(gap, unit)} 더 높습니다."
    return text


def summary(r):
    """계산 결과 → 답변 문장 (LLM을 거치지 않는다)"""
    status = r.get("status")
    if status == "ok" and r.get("split"):
        text = _split_text(r)
        if r.get("causal_question"):
            text = f"{r['causal_question']} {text}"
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
        if r.get("causal_question"):
            text = f"{r['causal_question']} {text}"
        if r.get("answer_note"):
            text += " " + r["answer_note"]
        return text
    if status == "out_of_range" and r.get("suggestion", {}).get("status") == "ok":
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
        for s in r["series"]:
            line = f"- {s['year']}년: {_num(s['value'], unit)}"
            if "diff_from_prev" in s:
                ch = _signed(s["diff_from_prev"], r["change"]["diff_unit"])
                if "rate_from_prev" in s:
                    ch += f", {_signed(s['rate_from_prev'], '%')}"
                line += f" (전년 대비 {ch})"
            lines.append(line)
        c = r["change"]
        total = _signed(c["diff"], c["diff_unit"])
        if "rate" in c:
            total += f", {_signed(c['rate'], c['rate_unit'])}"
        lines.append(f"- {c['from_year']}년 → {c['to_year']}년 전체 변화: {total}")
        return lines
    if "values" in r:  # 지역 비교
        lines = [f"{r['year']}년 {name} ({r['sex']}, 높은 순)"]
        lines += [f"- {v['region']}: {_num(v['value'], unit)}" for v in r["values"]]
        if r.get("gap") is not None:
            lines.append(
                f"- 가장 높은 {r['highest']}와 가장 낮은 {r['lowest']}의 차이: "
                f"{_num(r['gap'], r['gap_unit'])}"
            )
        return lines
    if "ranking" in r:  # 순위
        lines = [
            f"{r['year']}년 {name} {r['level']} 순위 ({r['order']}, 전체 {r['total_regions']}곳 중)"
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
    return []


def _source_result(result):
    """출처를 보여줄 계산 결과 (수록 기간 밖이라 추계값을 대신 보여주면 추계 결과)"""
    if result.get("split"):
        first = result["split"][0]
        notes = list(dict.fromkeys(n for p in result["split"] for n in p.get("notes") or []))
        return {**first, "notes": notes}
    suggestion = result.get("suggestion", {})
    if result.get("status") == "out_of_range" and suggestion.get("status") == "ok":
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
    return "\n".join(lines)


def sources(result):
    """답변에 쓴 통계표 목록 [{"name", "basis", "tbl_id", "url"}]"""
    src = _source_result(result)
    if not src.get("source"):
        return []
    return [
        {
            "name": src["source"],
            "basis": src["basis"],
            "tbl_id": src.get("tbl_id"),
            "url": src.get("url"),
        }
    ]


async def ask(question):
    """질문 하나를 끝까지 처리. status, answer, parsed, result, sources, trace를 반환한다.
    status: ok 또는 답변 불가 사유 (indicators.py 결과의 status)
    trace: HCX 원본 출력과 단계별 시간(ms). 요청 로그에만 쓰고 API 응답에는 넣지 않는다"""
    trace = {}
    start = time.perf_counter()
    parsed = await understand(question, trace)
    trace["understand_ms"] = round((time.perf_counter() - start) * 1000)
    start = time.perf_counter()
    # DuckDB 조회는 동기 함수라 이벤트 루프를 막지 않도록 스레드에서 실행
    result = await run_in_threadpool(compute, parsed)
    trace["compute_ms"] = round((time.perf_counter() - start) * 1000)
    return {
        "status": result.get("status"),
        "answer": f"{summary(result)}\n\n{render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": sources(result),
        "trace": trace,
    }


# ---- 요청 기록 ---------------------------------------------------------------------------
# 질문할 때 KOSIS를 부르지 않는다. 대신 DB에 없어서 답하지 못한 질문을 모아 두고,
# 다음 회차 배치 수집에서 추가할 데이터를 정할 때 본다 (scripts/missing_requests.py)
# 모든 요청은 따로 남겨, 틀린 답이 질문 해석(HCX·보정) 문제인지 조회·계산 문제인지 추적한다
# (scripts/request_log.py)

DATA_GAP = {"unknown_indicator", "out_of_range", "region_not_in_year", "missing_data"}
REQUESTS_DIR = Path(__file__).resolve().parents[2] / "data" / "requests"
MISSING_LOG = REQUESTS_DIR / "missing.jsonl"
REQUEST_LOG = REQUESTS_DIR / "requests.jsonl"


def _append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def log_missing(question, res, path=None):
    """DB에 없는 데이터 때문에 답하지 못한 질문이면 한 줄(JSON) 추가. 기록했으면 True"""
    if res.get("status") not in DATA_GAP:
        return False
    record = {
        "at": _now(),
        "question": question,
        "status": res["status"],
        "parsed": res.get("parsed"),
    }
    _append(path or MISSING_LOG, record)
    return True


def log_request(question, res=None, error=None, elapsed_ms=None, path=None):
    """모든 /ask 요청을 한 줄(JSON)로 남긴다. 실패한 요청(res 없음)은 error에 원인

    단계별로 보면 원인을 나눌 수 있다:
      hcx_raw     HCX가 돌려준 원문 (JSON을 못 읽었으면 여기서 원인 확인)
      parsed      해석 결과 + corrections(코드가 고친 항목과 이유)
      result      조회·계산 결과와 status
    """
    res = res or {}
    trace = res.get("trace") or {}
    record = {
        "at": _now(),
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
    _append(path or REQUEST_LOG, record)
