import asyncio
import contextlib
import copy
import json
import logging
import math
import re
import time
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.agent import replan, report_offer, report_router
from app.core.config import get_settings
from app.prompts.qa import FOLLOW_SYSTEM, RESTATE_SYSTEM, TALK_SPLIT_SYSTEM
from app.services import (
    agegroup,
    agent_action,
    answer_judge,
    answer_summary,
    ask_followup,
    charts,
    conversations,
    cross_indicator,
    dynamic_indicator,
    figure,
    flow,
    indicators,
    llm_calls,
    lookup,
    multi_indicator,
    progress,
    qa,
    question_clarify,
    refusal,
    reports,
    search_llm,
    slots,
    table_check,
    table_guide,
    timeref,
    understand_merged,
)
from app.services.hcx_client import HcxClient, HcxError
from app.services.josa import josa
from app.services.log_redaction import mask, redact
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

SESSION_FOR = timedelta(hours=1)
TURNS_FOR = timedelta(days=30)
STATE_CACHE = MemoryCache("chat_state", maxsize=2048, ttl=3600)
RECENT = 10
REPLY_MAX_CHARS = 25
FOLLOW_MAX_TOKENS = 512

DETAIL = "detail"
SOURCE = "source"
CHAT = "chat"
NEW, CHANGE, CHART, SUMMARY, REPLY, UNCLEAR, REPORT = (
    "new",
    "change",
    "chart",
    "summary",
    "reply",
    "unclear",
    "report",
)
TABLES = "tables"
ASK_INDICATOR = "asked_place"
REGION_RETRY_STATUSES = ("missing_data", "no_region_level")
TYPES = {"value", "trend", "compare", "rank", "composition"}
CHART_VIEWS = ("line", "bar", "table")
NO_CONDITIONS = {"other", "parse_error", None}

# 순서대로 본다: '막대그래프'는 bar, 모양을 말하지 않은 '그래프로'는 chart(직전 차트의 기본 모양)
CHART_WORDS = {
    "table": re.compile(r"표\s*로|표\s*형태|표로\s*정리|테이블"),
    "bar": re.compile(r"막대(?![하한])"),
    "line": re.compile(r"꺾은\s*선|선\s*그래프|라인|(?<![가-힣])선\s*으로"),
    "pie": re.compile(r"원\s*그래프|원형|파이|도넛"),
    "scatter": re.compile(r"산점도|산포도|스캐터"),
    "chart": re.compile(r"그래프|차트|시각화|그림\s*으로|그려"),
}
CHART_REQUEST = re.compile(
    r"보여|바꿔|바꾸|그려|볼래|볼\s*수|보고|줘|주세요|시각화"
    r"|(?:로|으로|그래프|차트|표|막대)\s*(?:는|은|도|요|가능)?\s*[?.!~]*$"
)
CHART_INSTEAD = re.compile(r"말고|대신|아니고|아니라")
CHART_MAX_CHARS = 40
# 차트 말을 지운 나머지가 이런 말뿐이어야 그림만 바꾸는 요청이다. 무엇이든 조건이 남으면
# ('남자만' · '상위 5개만' · '비율로' …) 변경 추출에 맡긴다 (11/26 리뷰: 빼는 말 목록 방식은
# 목록에 없는 조건을 버리고 그림만 바꿨다)
_CHART_FILLER = (
    r"그럼|그러면|그렇다면|혹시|이거|그거|이걸|그걸|이것|그것|이건|그건|지금|이번엔|이번에는"
    r"|(?:이|그|위|앞선|직전)?\s*결과|다시|한\s*번|좀|형태|모양|말고|대신|아니고|아니라"
)
_CHART_VERB = (
    r"보여(?:줘요?|주세요|줄래요?|줄|주라|봐)?|바꿔(?:서|줘요?|주세요|줄래요?|줄|봐)?|바꿀|바꾸어"
    r"|그려(?:서|줘요?|주세요|줄래요?|줄)?|볼래요?|볼|보고|싶어요?|싶은데|봐요?"
    r"|해(?:서|줘요?|주세요|줄래요?|줄|봐)?|줘요?|주세요|줄래요?|주라"
    r"|가능(?:해요?|할까요?|한가요?)?|수|있어요?|있나요|있을까요?|돼요?|되나요|될까요?|변경|전환"
)
_CHART_PARTICLE = r"으로|로|는|은|도|요|가|을|를|에|서|이"
CHART_ONLY_REST = re.compile(rf"(?:{_CHART_FILLER}|{_CHART_VERB}|{_CHART_PARTICLE}|[\s?.!~,])*")
# 모양을 정하지 않는 차트 말 ('표 말고 그래프로'의 '표'). '통계표'의 표는 아니다
CHART_NOUN = re.compile(r"(?<![가-힣])표")
CHART_NAMES = {"line": "꺾은선 그래프", "bar": "막대그래프", "parts": "구성비 막대그래프"}
CHART_CANNOT = {
    "line": "꺾은선은 연도별 추이에만 씁니다",
    "pie": "원그래프는 지원하지 않습니다",
    "scatter": "산점도는 지원하지 않습니다",
}
SUMMARY_REQUEST = re.compile(
    r"^\s*((한|두|세)\s*줄로|짧게|간단히|간단하게|간략히)?\s*(요약|정리)\s*"
    r"(좀\s*)?(해\s*(줘요?|주세요|줄래요?|봐요?|주라)?|부탁\s*(해요?|합니다|드려요)?)?\s*[.!?~]*$"
)
SEX_WORDS = re.compile(r"남자|여자|남성|여성|남녀|(?<![구특])성별")

LABELS = {"indicator": "지표", "regions": "지역", "period": "기간", "sex": "성별"}
SOURCE_WORDS = {"inherited": "이어받음", "default": "기본값"}

_locks: dict[str, asyncio.Lock] = {}


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return f"A_{uuid4().hex[:12]}"


def same_owner(state: dict, user_id: int | None) -> bool:
    if "user_id" not in state:
        return False
    owner = state["user_id"]
    return owner is None or owner == user_id


def _lock(session_id: str) -> asyncio.Lock:
    if len(_locks) > 1000:
        for key in [k for k, lock in _locks.items() if not lock.locked()]:
            _locks.pop(key, None)
    return _locks.setdefault(session_id, asyncio.Lock())


def has_period(question: str) -> bool:
    return bool(
        ask_followup.has_explicit_year(question)
        or timeref.RECENT_N_YEARS.search(question)
        or timeref.N_YEARS_AGO.search(question)
        or ask_followup.FROM_PAST.search(question)
        or any(word in question for word, _ in timeref.WORDS)
    )


def sources_of(question: str, parsed: dict) -> dict[str, str]:
    return {
        "indicator": "user",
        "regions": "user" if parsed.get("regions") else "default",
        "period": "user" if has_period(question) else "default",
        "sex": "user"
        if parsed.get("sex") not in (None, "계") or SEX_WORDS.search(question)
        else "default",
    }


def period_text(c: dict) -> str | None:
    start, end = c.get("start_year"), c.get("end_year")
    if c.get("dated_months") and start and end and (months := c.get("months")):
        return f"{start}년 {months[0]}월~{end}년 {months[1]}월"
    if c.get("type") == "trend" and start and end:
        return f"{start}~{end}년" if start != end else f"{start}년"
    if c.get("type") == "rank" and c.get("change_from") and c.get("year"):
        return f"{c['change_from']}년 → {c['year']}년"
    return f"{c['year']}년" if c.get("year") else None


def applied(
    conditions: dict, sources: dict[str, str], *, include_regions: bool = True
) -> list[dict[str, str]]:
    if conditions.get("type") in NO_CONDITIONS:
        return []
    regions = conditions.get("regions") or []
    if not regions and conditions.get("type") == "rank":
        regions = [f"{conditions.get('sido') or '전국'} {conditions.get('level') or '시도'}별"]
    values = {
        "indicator": ", ".join(conditions.get("indicators") or [])
        or qa.birth_ratio_label(conditions.get("indicator"), conditions.get("birth_ratio_base")),
        "regions": ", ".join(regions) if include_regions else None,
        "period": period_text(conditions),
        "sex": conditions.get("sex"),
    }
    return [
        {"name": LABELS[key], "value": str(value), "source": sources.get(key, "default")}
        for key, value in values.items()
        if value
    ]


def _clear_period_questions(conditions: dict) -> None:
    asked = {
        k: v
        for k, v in (conditions.get("asked") or {}).items()
        if k in ("survey", "survey_missing")
    }
    if asked:
        conditions["asked"] = asked
    else:
        conditions.pop("asked", None)


def _clear_dated_months(conditions: dict) -> None:
    if conditions.pop("dated_months", False):
        conditions.pop("months", None)


def effective(parsed: dict, result: dict) -> dict:
    c = {k: v for k, v in parsed.items() if k not in ("corrections", "raw")}
    series = next((r["series"] for r in qa._leaves(result) if r.get("series")), None)
    if result.get("status") == "ok" and series:
        c["start_year"], c["end_year"] = series[0]["year"], series[-1]["year"]
        c["year"] = c["end_year"]
        _clear_period_questions(c)
    elif result.get("status") == "ok" and result.get("year"):
        c["year"] = result["year"]
        if "ranking" in result:
            c["change_from"] = result.get("change_from")
    if sex_not_split(parsed, result):
        c["sex"] = "계"
    return c


def sex_not_split(parsed: dict, result: dict) -> bool:
    leaves = qa._leaves(result) if result.get("status") == "ok" else []
    spec = parsed.get("dynamic_spec") or {}
    if spec.get("kind") == "ratio" and (spec.get("fixed") or {}).get("sex") == parsed.get("sex"):
        return False
    return (
        parsed.get("sex") in ("남", "여")
        and bool(leaves)
        and all(r.get("sex") == "계" for r in leaves)
    )


def inherited_notice(conditions: dict, sources: dict[str, str]) -> str | None:
    parts = [
        f"{row['name']} {row['value']}"
        for row in applied(conditions, sources)
        if row["source"] == "inherited"
    ]
    return f"앞 질문의 조건을 이어받았습니다: {', '.join(parts)}" if parts else None


def _names(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in values if v and str(v).strip()]


def _same_region(a: str, b: str) -> bool:
    a, b = a.replace(" ", ""), b.replace(" ", "")
    return a in b or b in a


def _year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _without_gender(term: str, sex: str) -> str:
    word = qa.GENDER_WORDS[sex].pattern
    return re.sub(rf"(?<![가-힣])(?:{word})\s*", "", term, count=1).strip()


def apply_changes(conditions: dict, changes: dict) -> tuple[dict, set[str]]:
    c = copy.deepcopy(conditions)
    changed: set[str] = set()

    if name := (changes.get("indicator") or "").strip():
        c["indicator"] = indicators.canonical_name(name) or name
        c["term"] = name
        for key in (
            "ages",
            "age_group",
            "age_note",
            "housing_condition",
            "unsupported",
            "share_of",
            "aggregate",
            "group",
            "by",
            "indicators",
            "measure_conditions",
        ):
            c.pop(key, None)
        if c["indicator"] not in qa.BIRTH_RATIO_OPPOSITE:
            c.pop("birth_ratio_base", None)
        changed.add("indicator")

    if (
        c.get("indicator") in qa.BIRTH_RATIO_OPPOSITE
        and (base := changes.get("birth_ratio_base")) is not None
    ):
        c["birth_ratio_base"] = base
        changed.add("indicator")

    region = changes.get("regions")
    if isinstance(region, dict) and (names := _names(region.get("value"))):
        current = list(c.get("regions") or [])
        op = region.get("op")
        if op == "add":
            current += [n for n in names if not any(_same_region(n, r) for r in current)]
        elif op == "remove":
            current = [r for r in current if not any(_same_region(n, r) for n in names)]
        else:
            current = names
        c["regions"] = current
        for key in ("group", "aggregate", "sub_area", "ungrounded_region"):
            c.pop(key, None)
        changed.add("regions")

    year, start, end = (_year(changes.get(k)) for k in ("year", "start_year", "end_year"))
    if start or end:
        c["start_year"] = start or c.get("start_year") or c.get("year")
        c["end_year"] = end or c.get("end_year") or c.get("year")
        if c["start_year"] and c["end_year"] and c["start_year"] > c["end_year"]:
            c["start_year"], c["end_year"] = c["end_year"], c["start_year"]
        c["year"] = c["end_year"] or c.get("year")
        changed.add("period")
    elif year:
        c["year"], c["start_year"], c["end_year"] = year, None, None
        changed.add("period")
    if "period" in changed:
        _clear_period_questions(c)
        _clear_dated_months(c)
        c.pop("time_refs", None)
    if "indicator" in changed or "regions" in changed:
        for key in ("movement_route", "movement_reason", "movement_ratio"):
            c.pop(key, None)

    if changes.get("sex") in ("계", "남", "여"):
        old_sex = c.get("sex")
        if changes["sex"] == "계" and old_sex in ("남", "여"):
            term = c.get("term") or c.get("indicator") or ""
            if not dynamic_indicator.PCT_ASK.search(term):
                c["term"] = _without_gender(term, old_sex)
        c["sex"] = changes["sex"]
        changed.add("sex")

    if (bands := changes.get("ages")) and isinstance(bands, list):
        # 새 질문 경로의 이어받기(inherit)와 같게: 연령 구간을 바꾸고 지표 이름을 기준 말로
        c["ages"] = [list(band) for band in bands]
        c.pop("age_group", None)
        c.pop("age_note", None)
        if c.get("indicator"):
            c["term"] = c["indicator"]
        changed.add("ages")

    if changes.get("type") in TYPES:
        c["type"] = changes["type"]
        changed.add("view")
    if isinstance(changes.get("top"), int) and 0 < changes["top"] <= 300:
        c["top"] = changes["top"]
        changed.add("view")
    if isinstance(changes.get("ascending"), bool):
        c["ascending"] = changes["ascending"]
        changed.add("view")
    if changes.get("level") in ("시도", "시군구"):
        c["level"] = changes["level"]
        changed.add("view")
    if "sido" in changes and (changes["sido"] is None or isinstance(changes["sido"], str)):
        c["sido"] = changes["sido"] or None
        changed.add("view")
    if "within" in changes and (changes["within"] is None or isinstance(changes["within"], str)):
        c["within"] = changes["within"] or None
        changed.add("view")

    if changed and "type" not in changes:
        _retype(c, changed)
    if changed and (measures := c.get("measure_conditions")):
        c["measure_conditions"] = {
            name: apply_changes(measure, changes)[0] for name, measure in measures.items()
        }
    return c, changed


def _retype(c: dict, changed: set[str]) -> None:
    kind = c.get("type")
    if kind == "composition" or not ({"regions", "period"} & changed):
        return
    if kind == "rank" and "regions" not in changed:
        return
    count = len(c.get("regions") or [])
    span = bool(c.get("start_year") and c.get("end_year") and c["start_year"] != c["end_year"])
    if span:
        c["type"] = "trend"
    elif count > 1:
        c["type"] = "compare"
    elif count == 1:
        c["type"] = "value"
    elif "regions" in changed:
        c["type"] = "rank"
    else:
        c["type"] = "value"


CONTEXT_YEAR = [
    (re.compile(r"전년도|전년|(?:그|직)\s*전\s*해|이전\s*해|직전\s*연도|한\s*해\s*전"), -1),
    (re.compile(r"다음\s*해|이듬해|다음\s*연도|익년|한\s*해\s*(?:뒤|후)"), 1),
]
FULL_YEAR = re.compile(rf"(?:19|20)\d{{2}}{slots.NOT_YEAR_TAIL}")
YEAR_ONLY_REPLY = re.compile(
    rf"\s*{slots.YEAR.pattern}\s*(?:년도?\s*)?(?:(?:으로|로)\s*)?(?:은|는|도|만)?\s*(?:이?요)?"
    r"(?:\s*(?:보여|알려|바꿔)\s*(?:줘요?|주세요))?\s*[?.!]*"
)
BIRTH_RATIO_REVERSE_REPLY = re.compile(r"\s*(?:그럼\s*|그러면\s*)?반대(?:로)?(?:는|도)?\s*[?.!]*")
BIRTH_RATIO_COUNT_REPLY = re.compile(
    rf"\s*(?:그럼\s*|그러면\s*)?(?P<count>{qa.BIRTH_RATIO_COUNT})\s*명"
    r"\s*(?:당|으로|[을를]?\s*기준(?:으로)?)(?:은|는|도)?\s*[?.!]*"
)


def follow_year(question: str, conditions: dict, today: date | None = None) -> int | None:
    q = timeref.COMPARE_BASE.sub(" ", question)
    if FULL_YEAR.search(q) or slots.decades(q):
        return None
    base = conditions.get("end_year") or conditions.get("year")
    for pattern, step in CONTEXT_YEAR:
        if pattern.search(q):
            return int(base) + step if base else None
    ref = timeref.resolve(q, today)
    return ref.year if ref and not ref.open_end else None


def recent_years(question: str) -> int | None:
    """숫자 연도 없는 후속 질문에서 '최근 N년'이나 'N년 동안'의 N을 찾는다."""
    if FULL_YEAR.search(question):
        return None
    for ref in timeref.find(question):
        if ref["kind"] in ("recent", "span") and ref.get("n"):
            return ref["n"]
    span = SPAN_TO.search(question)
    if span and PERIOD_CUE.search(question) and 1 < int(span[1]) <= 50:
        return int(span[1])
    return None


def with_recent(conditions: dict, changed: set[str], n: int) -> tuple[dict, set[str]]:
    """조건을 '자료 마지막 해를 포함한 N개 해' 추이로 (계산 단계가 asked.recent로 표마다 센다)"""
    c = copy.deepcopy(conditions)
    c["asked"] = {**(c.get("asked") or {}), "recent": n}
    c.pop("time_refs", None)
    _clear_dated_months(c)
    c["start_year"] = c["end_year"] = None
    if c.get("type") in ("value", "compare"):
        c["type"] = "trend"
    if measures := c.get("measure_conditions"):
        c["measure_conditions"] = {
            name: with_recent(measure, set(), n)[0] for name, measure in measures.items()
        }
    return c, changed | {"period"}


AGE_REPLY_TAIL = re.compile(
    r"\s*(?:까지\s*)?(?:사람(?:들)?|인구)?\s*(?:으로|로)?\s*(?:은|는|도|만|이?요)?\s*"
    r"(?:(?:보여|알려|바꿔)\s*(?:줘요?|주세요)|어때요?)?"
    r"\s*[?.!~]*"
)


def age_only_change(question: str, conditions: dict) -> dict | None:
    if (
        conditions.get("indicator") != "총인구"
        or conditions.get("type") not in ("value", "trend", "compare", "rank")
        or any(
            conditions.get(k)
            for k in ("cross", "indicators", "dynamic_spec", "unsupported", "housing_condition")
        )
    ):
        return None
    text = re.sub(r"^만\s*", "", agegroup.native_to_digits(question).strip())
    spans = qa.age_spans(text)
    if not spans and (single := agegroup.SINGLE_AGE.match(text)):
        age = int(single[1])
        spans = [(0, single.end(), [age, age])]
    if len(spans) != 1:
        return None
    start, end, band = spans[0]
    if start or not AGE_REPLY_TAIL.fullmatch(text[end:]):
        return None
    changed = {**conditions, "ages": [band], "term": "총인구"}
    for key in ("age_group", "age_note", "born"):
        changed.pop(key, None)
    if note := qa.decade_part_note(question):
        changed["age_note"] = note
    return changed


VIEW_WORDS = {
    "추이",
    "변화",
    "비교",
    "순위",
    "구성",
    "구성비",
    "통계",
    "수치",
    "그래프",
    "차트",
    "표",
}


def names_indicator(question: str, parsed: dict) -> bool:
    question = qa.without_survey(question)
    if (
        qa.birth_sex_ratio_reference(question)
        or indicators.find_in_text(question)
        or PEOPLE_WORDS.search(question)
    ):
        return True
    compact = question.replace(" ", "")
    words = [parsed.get("term"), parsed.get("indicator")]
    return any(_says_word(w, compact) for w in words)


def _says_word(word: Any, compact: str) -> bool:
    if not isinstance(word, str) or not word.strip() or word.strip() in VIEW_WORDS:
        return False
    if dynamic_indicator._without_age_words(agegroup.native_to_digits(word)) in ("", "로", "으로"):
        return False
    return word.replace(" ", "") in compact


# '혼인도 알려줘' · '그럼 이혼은?'처럼 낱말 하나만 말한 후속 질문
SUBJECT_ONLY = re.compile(
    r"\s*(?:그럼|그러면|그렇다면|그리고|혹시)?\s*(?P<word>[가-힣]{2,}?)\s*"
    r"(?:은|는|도|이|가|만|이요|요)?\s*"
    r"(?:(?:알려|보여|해)\s*(?:줘요?|주세요|줄래요?)|어때요?|몇\s*(?:명|건)(?:이야|이에요|인가요)?|얼마야)?"
    r"\s*[?.!~]*\s*"
)
# 낱말 하나로 물어도 볼 통계가 아닌 말 (지역 · 기간 · 성별 · 연령 · 보는 방식은 따로 본다)
NOT_SUBJECT = {
    "거기", "여기", "그곳", "이곳", "그거", "이거", "전체", "합계", "평균", "비율", "비중",
    "증가율", "감소율", "증감률", "변화율", "결과", "요즘", "최근", "작년", "올해", "재작년",
    "내년", "남녀", "성별", "연령", "나이", "지역", "시도", "시군구", "요약", "정리", "출처",
    "근거", "기준", "이유", "원인", "숫자", "단위", "자세히", "전국", "우리나라", "한국", "국내",
    "읍면동", "상위", "하위", "꼴찌", "최고", "최저", "최대", "최소", "남아", "여아", "아들",
}  # fmt: skip
# '몇명이야?' · '얼마야?' · '어때?'는 볼 통계가 아니라 묻는 말 (붙여 쓰면 통째로 낱말로 잡힌다)
ASKING_WORD = re.compile(r"^(?:몇|얼마|어때|어떻|어떤|어느|어디|언제|누구|뭐|무엇|무슨|왜)")


def said_other_subject(question: str, regions: list[str] | None = None) -> str | None:
    """사전이 모르는 낱말 하나만 말한 후속 질문이면 그 낱말 ('혼인도 알려줘' → '혼인').
    그 낱말이 볼 통계라 앞 지표를 몰래 이어받지 않는다: HCX가 지표를 비웠을 때
    출생아 수 뒤 '사망자도 알려줘'를 출생아 수로 답했다 (11/26).
    regions: HCX가 지역으로 읽은 이름 ('서울은?'의 '서울'은 지역)"""
    if not (said := SUBJECT_ONLY.fullmatch(question.strip())):
        return None
    word = said["word"]
    if (
        word in NOT_SUBJECT
        or ASKING_WORD.match(word)
        or word in VIEW_WORDS
        or word.endswith("별")
        or any(pattern.search(word) for pattern in CHART_WORDS.values())
        or NEW_VIEW.search(word)
        or SEX_WORDS.search(word)
        or qa.resolved_ages(word)
        or timeref.find(word)
        or indicators.find_regions(word)
        or any(word in str(r) or str(r) in word for r in regions or [])
    ):
        return None
    return word


def complete_question(question: str) -> bool:
    if re.search(r"같이|추가|빼|빼고|제외|대신|그대로|거기|거기에|보다|도\s", question):
        return False
    return bool(
        indicators.find_in_text(question)
        and has_period(question)
        and ("전국" in question or indicators.find_regions(question))
    )


def inherit(
    parsed: dict,
    sources: dict,
    prev: dict,
    prev_sources: dict,
    question: str = "",
    said_alone: str = "",
) -> tuple[dict, dict]:
    """said_alone: 직전 지역 · 기간을 붙이기 전 사용자가 한 말 ('혼인도 알려줘').
    붙인 질문('2025년 서울 혼인도 알려줘')에서는 낱말 하나만 말했는지 알 수 없어 이 말로 본다"""
    p, s = copy.deepcopy(parsed), dict(sources)
    kind = p.get("type")
    if kind in NO_CONDITIONS or prev.get("type") in NO_CONDITIONS:
        return p, s
    alone = said_alone or question
    word = said_other_subject(alone, p.get("regions")) if alone else None
    if word and not names_indicator(question, p):
        # 앞 지표 대신 말한 낱말로: 지표가 없으면 계산 단계가 그 낱말로 표를 찾는다
        if not p.get("indicator") and not p.get("term"):
            p["term"] = word
        flow.add("지표 이어받기", "안 함", "질문이 " + josa(f"'{word}'", "을를") + " 물음")
    elif question and prev.get("indicator") and not names_indicator(question, p):
        keys = [
            "indicator",
            "term",
            "ages",
            "age_group",
            "age_note",
            "by",
            "dynamic_spec",
            "birth_ratio_base",
        ]
        if said_ages := qa.extract_ages(question):
            keys = [k for k in keys if k not in ("term", "ages", "age_group", "age_note")]
            p["ages"] = said_ages
            p.pop("age_group", None)
            p["term"] = prev["indicator"]
        for key in keys:
            if prev.get(key) is not None:
                p[key] = copy.deepcopy(prev[key])
        p.pop("unsupported", None)
        if "grounded" in prev:
            p["grounded"] = prev["grounded"]
        s["indicator"] = "inherited"
    if (
        question
        and kind == "value"
        and prev.get("type") in ("trend", "compare", "rank")
        and s.get("regions") == "default"
        and s.get("period") == "default"
        and not VIEW_ASK.search(question)
        and names_indicator(question, parsed)
    ):
        for key in KEEP_VIEW:
            p[key] = copy.deepcopy(prev.get(key))
        kind = p.get("type")
        if prev_sources.get("period") in ("user", "inherited"):
            s["period"] = "inherited"
    if s.get("regions") == "default" and kind != "rank" and prev.get("regions"):
        p["regions"] = list(prev["regions"])
        s["regions"] = "inherited"
    if kind == "rank" and question and p.get("level") == "시군구" and not said_region(question):
        p["sido"] = prev.get("sido") or sido_of(prev.get("regions") or [])
        if p["sido"]:
            s["regions"] = "inherited"
    if p.get("type") == "value" and s.get("regions") == "inherited" and len(p["regions"]) > 1:
        if p.get("aggregate") != "sum":
            p["type"] = kind = "compare"
    if (
        s.get("period") == "default"
        and prev_sources.get("period") in ("user", "inherited")
        and kind == prev.get("type")
    ):
        for key in ("year", "start_year", "end_year"):
            p[key] = prev.get(key)
        s["period"] = "inherited"
    if question:
        survey_question = question
        if not qa.survey_said(question) and s.get("indicator") == "inherited":
            previous = prev.get("asked") or {}
            if survey := previous.get("survey") or previous.get("survey_missing"):
                survey_question = f"{survey} 기준 {question}"
        qa.set_survey(survey_question, p)
    return p, s


def survey_switch(question: str, conditions: dict) -> dict | None:
    if not qa.survey_said(question) or not conditions.get("indicator"):
        return None
    rest = qa.without_survey(question)
    if indicators.find_in_text(rest) or said_region(rest):
        return None
    switched = qa.set_survey(question, copy.deepcopy(conditions))
    asked = switched.get("asked") or {}
    for measure in (switched.get("measure_conditions") or {}).values():
        qa.set_survey(question, measure)
    return switched if asked.get("survey") or asked.get("survey_missing") else None


def said_region(question: str) -> bool:
    return bool("전국" in question or indicators.find_regions(question))


def sido_of(regions: list[str]) -> str | None:
    if len(regions) != 1:
        return None
    found = indicators.resolve_region(regions[0])
    return regions[0] if found.get("status") == "ok" and found.get("kind") == "시도" else None


def without_inherited_period(
    parsed: dict, original: dict, sources: dict
) -> tuple[dict, dict] | None:
    if sources.get("period") != "inherited":
        return None
    p = copy.deepcopy(parsed)
    for key in ("year", "start_year", "end_year"):
        p[key] = original.get(key)
    return p, {**sources, "period": "default"}


def without_inherited(parsed: dict, original: dict, sources: dict):
    keys = [k for k in ("period", "regions") if sources.get(k) == "inherited"]
    tries = [(k,) for k in keys] + ([tuple(keys)] if len(keys) == 2 else [])
    for dropped in tries:
        p, source = copy.deepcopy(parsed), dict(sources)
        if "period" in dropped:
            for key in ("year", "start_year", "end_year"):
                p[key] = original.get(key)
        if "regions" in dropped:
            p["regions"] = list(original.get("regions") or [])
        source.update({key: "default" for key in dropped})
        yield p, source, dropped


def table_choice_options(found: dict) -> list[str]:
    clarify = found.get("clarify") or {}
    options = [str(o) for a in clarify.get("axes", []) for o in a.get("options", [])]
    options.extend(str(f["tbl_nm"]) for f in clarify.get("families", []) if f.get("tbl_nm"))
    return options


def is_choice_reply(question: str, options: list[str]) -> bool:
    if indicators.find_in_text(question):
        return False
    q = re.sub(r"\s+", "", question).strip(" .?!")
    if re.fullmatch(r"[1-9](?:번)?(?:이요|요)?", q):
        return bool(options)
    for option in options:
        word = re.sub(r"\s+", "", option)
        if word and q in {word, word + "요", word + "이요", word + "로", word + "으로"}:
            return True
    parts = [p.strip() for p in re.split(r"[,/·]", question)]
    return len(parts) > 1 and all(
        re.sub(r"\s+", "", part) in {re.sub(r"\s+", "", o) for o in options} for part in parts
    )


def pending_of(result: dict, question: str) -> dict | None:
    status = result.get("status")
    if status == "missing_year":
        return {"missing": "year", "original_question": question}
    if status == "ambiguous_region" and result.get("candidates"):
        return {
            "missing": "region",
            "region": result.get("region"),
            "options": list(result["candidates"]),
            "original_question": question,
        }
    return None


ALTERNATIVE_REPLY_MAX = 40
AGREE = re.compile(r"^\s*(응|네|예|그래|좋아|그걸로|그거로|그것으로|그거)\b")
REPLY_TAIL = re.compile(r"(이랑|랑|하고|까지|으로요|로요|으로|로|이요|요|도|만|을|를|은|는|이|가)$")


def alternative_by_words(reply: str, answers: list[dict]) -> str | None:
    if len(reply.strip()) > ALTERNATIVE_REPLY_MAX or not answers:
        return None
    if len(answers) == 1 and AGREE.match(reply):
        return "0"
    words = [REPLY_TAIL.sub("", w) for w in re.findall(r"[가-힣]{2,}", reply)]
    words = [w for w in words if len(w) >= 2 and w not in {"포함", "포함해서", "기준"}]

    def text(a: dict) -> str:
        notes = " ".join(map(str, ((a.get("result") or {}).get("notes") or [])))
        return f"{a.get('answer') or ''} {notes}"

    hits = [i for i, a in enumerate(answers) if words and any(w in text(a) for w in words)]
    return str(hits[0]) if len(hits) == 1 else None


def pick_region(reply: str, options: list[str]) -> str | None:
    reply = reply.strip()
    if len(reply) > REPLY_MAX_CHARS:
        return None
    if m := re.match(r"\s*([1-9])\s*번?", reply):
        index = int(m.group(1)) - 1
        return options[index] if index < len(options) else None
    said = re.sub(r"(이요|요|으로|로|입니다|이에요|예요)\s*[.!]?$", "", reply).replace(" ", "")
    if not said:
        return None
    exact = [o for o in options if o.replace(" ", "") == said]
    partial = [o for o in options if said in o.replace(" ", "")]
    for found in (exact, partial):
        if len(found) == 1:
            return found[0]
    return None


def replace_region(conditions: dict, asked: str | None, picked: str) -> dict:
    c = copy.deepcopy(conditions)
    regions = list(c.get("regions") or [])
    if asked in regions:
        regions[regions.index(asked)] = picked
    else:
        regions = [picked]
    c["regions"] = regions
    return c


def chart_request(question: str) -> str | None:
    """직전 답의 그림만 바꾸는 말이면 모양 (table · bar · line · pie · scatter · chart),
    아니면 None.
    차트 말을 지운 나머지에 요청 말 · 조사만 남아야 한다. 연도 · 지표 · 지역 · 성별 · 순위 같은
    조건이 남으면 조건 변경이라 변경 추출에 맡긴다 ('남자만 막대그래프로'를 전체 값의 막대로만
    바꿨다)"""
    q = question.strip()
    if len(q) > CHART_MAX_CHARS or not (view := chart_shape(q)):
        return None
    rest = q
    for pattern in [*CHART_WORDS.values(), CHART_NOUN]:
        rest = pattern.sub(" ", rest)
    return view if CHART_ONLY_REST.fullmatch(rest) else None


def chart_shape(question: str) -> str | None:
    """말한 차트 모양. 조건 변경과 함께 말해도 읽는다 ('남자만 막대그래프로' → bar)"""
    if not CHART_REQUEST.search(question):
        return None
    asked = CHART_INSTEAD.split(question)[-1]  # '표 말고 그래프로' → '그래프로'
    return next((view for view, pattern in CHART_WORDS.items() if pattern.search(asked)), None)


# 차트 말을 지운 뒤 홀로 남은 조사 · 모양 말 ('막대로' → '로')
CHART_LEFTOVER = re.compile(
    r"(?<!\S)(?:(?:형태|모양)\s*)?(?:으로|로)(?:는|도)?(?!\S)|(?<!\S)(?:형태|모양|말고|대신)(?!\S)"
)


def condition_part(question: str) -> str:
    """차트 말을 뺀 조건 부분 ('10개만 막대로 보여줘' → '10개만 보여줘')"""
    rest = question
    for pattern in [*CHART_WORDS.values(), CHART_NOUN]:
        rest = pattern.sub(" ", rest)
    return " ".join(CHART_LEFTOVER.sub(" ", rest).split())


COMPOSITION_REQUEST = re.compile(r"구성비|구성\s*으로|비중\s*으로")


def composition_request(question: str) -> bool:
    q = question.strip()
    return (
        len(q) <= REPLY_MAX_CHARS
        and bool(COMPOSITION_REQUEST.search(q) and CHART_REQUEST.search(q))
        and not ask_followup.has_explicit_year(q)
        and not indicators.find_in_text(q)
    )


BY_GU = re.compile(r"(시군구|구|군)\s*(별|마다)")
BY_SIDO = re.compile(r"(시도|지역)\s*(별|마다)")
REGION_SPLIT_ONLY = re.compile(
    r"(?:시군구|구|군|시도|지역)\s*(?:별|마다)(?:로)?\s*"
    r"(?:(?:더\s*)?(?:자세히|자세하게|상세히|상세하게|구체적으로)\s*)?"
    r"(?:(?:보여|알려)\s*(?:줘요?|주세요|줄래요?)?)?\s*[?.!~]*"
)


def region_split_changes(question: str, conditions: dict) -> dict | None:
    q = question.strip()
    if (
        not REGION_SPLIT_ONLY.fullmatch(q)
        or len(q) > REPLY_MAX_CHARS
        or indicators.find_in_text(q)
        or ask_followup.has_explicit_year(q)
        or said_region(q)
    ):
        return None
    regions = list(conditions.get("regions") or [])
    changes: dict[str, Any] = {"type": "rank"}
    if regions:
        changes["regions"] = {"op": "remove", "value": regions}
    if BY_GU.search(q):
        sido = conditions.get("sido") or sido_of(regions)
        return {**changes, "level": "시군구", "sido": sido} if sido else None
    if BY_SIDO.search(q):
        return {**changes, "level": "시도", "sido": None}
    return None


FOLLOW_LEAD = r"(?:그럼|그러면|그렇다면|그리고|혹시)?\s*"
REGION_ONLY_REST = re.compile(
    rf"\s*{FOLLOW_LEAD}"
    r"(?:이랑|[\s,·와과랑하고및])*(?:광역시|특별시|특별자치시|특별자치도|시|도|군|구)?\s*"
    r"(?:의\s*경우(?:에)?|쪽|에서|에선)?\s*(?:은|는|도|이면|면)?\s*요?\s*"
    r"(?:어때요?|어떤가요?|어떤데요?|어떻게\s*(?:돼요?|되나요?|돼\?))?\s*[?.!~]*\s*"
)
# 순위 뒤 '경기도 안에서는?': 그 시도 안의 시군구 순위로
IN_SIDO_REPLY = re.compile(
    rf"\s*{FOLLOW_LEAD}(?P<place>[가-힣]+?)\s*(?:안에서|안에선|내에서|내에선|안|내)\s*"
    r"(?:만|는|은|도)?\s*(?:순위|순위는|순위로)?\s*(?:어때요?|보여\s*줘요?|알려\s*줘요?)?\s*[?.!~]*\s*"
)
# 순위 뒤 '낮은 순으로' · '많은 순서로' · '오름차순'
ORDER_REPLY = re.compile(
    rf"\s*{FOLLOW_LEAD}(?:(?P<low>낮은|적은|작은)\s*(?:순|순서|쪽)|(?P<high>높은|많은|큰)\s*(?:순|순서|쪽)"
    r"|(?P<asc>오름차순|꼴찌부터)|(?P<desc>내림차순))\s*(?:부터|으로|로)?\s*(?:정렬)?\s*"
    r"(?:해|보여|바꿔)?\s*(?:줘요?|주세요)?\s*[?.!~]*\s*"
)
# 추이 뒤 '10년으로 늘려줘' · '기간을 3년으로': 기간 길이만 바꿈
SPAN_TO = re.compile(
    r"(?<![\d.])(\d{1,2})\s*년\s*(?:치|간|동안)?\s*(?:으로|로)(?=\s|$|[?.!~]|는|도|만)"
    r"(?!\s*(?:나눠|나누|나눈|묶어|묶은|끊어|단위))"
)
# 'N년으로'만으로는 기간 길이인지 줄여 말한 연도인지 모른다: '24년으로 바꿔줘'는 2024년이다.
# 기간을 말하는 낱말이 함께 있을 때만 길이로 본다 (나머지는 변경 추출이 읽는다)
PERIOD_CUE = re.compile(r"늘려|늘리|줄여|줄이|기간|최근|동안|\d\s*년\s*(?:치|간)")
PERIOD_LENGTH_REPLY = re.compile(
    rf"\s*{FOLLOW_LEAD}(?:기간을?\s*)?(?:최근\s*)?\d{{1,2}}\s*년\s*(?:치|간|동안)?\s*(?:으로|로)?\s*"
    r"(?:만|은|는|도)?\s*(?:늘려|줄여|바꿔|보여|해|봐)?\s*(?:줘요?|주세요|줄래요?|봐요?)?\s*[?.!~]*\s*"
)
ALSO = re.compile(rf"^\s*{FOLLOW_LEAD}도\s*요?\s*(?:어때요?)?\s*[?.!~]*\s*$")


def only_regions(question: str) -> tuple[list[str], str] | None:
    q = question.strip()
    if len(q) > REPLY_MAX_CHARS:
        return None
    if indicators.find_in_text(q) or ask_followup.has_explicit_year(q):
        return None
    regions = indicators.find_regions(q)
    if not regions:
        return None
    rest = q
    for name in sorted(regions, key=len, reverse=True):
        rest = rest.replace(name, " ")
    if not REGION_ONLY_REST.fullmatch(rest):
        return None
    return regions, rest


def _several_places(regions: list[str], rest: str) -> bool:
    return len(regions) > 1 and bool(re.search(r"랑|와|과|하고|및|,|·", rest))


def region_only_changes(question: str, conditions: dict) -> dict | None:
    if conditions.get("type") in ("rank", None):
        return None
    if not (found := only_regions(question)):
        return None
    regions, rest = found
    if len(regions) > 1 and not _several_places(regions, rest):
        return None
    return {"regions": {"op": "add" if ALSO.match(rest) else "set", "value": regions}}


def rank_scope_changes(question: str, conditions: dict) -> dict | None:
    """순위 뒤 '경기도 안에서는?': 그 시도 안의 시군구 순위로. 범위가 있는 목록 뒤에 지역만
    말하면('경기도 시군별 …' → '부산은?', '수원시 구별 …' → '성남시는?') 목록의 범위를 그 지역으로.
    11/27 PR #261 점검: 목록 뒤에는 지역만 바꾸는 규칙이 꺼져 있어 새 질문으로 읽고 답하지 못했다"""
    if conditions.get("type") != "rank":
        return None
    if said := IN_SIDO_REPLY.fullmatch(question):
        regions = indicators.find_regions(said["place"])
    elif (conditions.get("sido") or conditions.get("within")) and (found := only_regions(question)):
        regions = found[0]
    else:
        return None
    if len(regions) != 1:
        return None
    place = indicators.resolve_region(regions[0])
    if place.get("status") != "ok":
        return None
    if place.get("kind") == "시도":
        # 시 안의 구 목록에서 왔으면 그 범위를 지운다
        cleared = {"within": None} if conditions.get("within") else {}
        return {"sido": regions[0], "level": "시군구", **cleared}
    if conditions.get("within") and indicators.districts(place):
        return {"within": place["display"], "sido": None}
    return None


# 연령 말을 지운 뒤 남아도 되는 말 ('65세 이상 여자만' → ' 여자만', '노인은?' → ' 은?')
AGE_ONLY_REST = re.compile(
    rf"\s*{FOLLOW_LEAD}(?P<sex>남자|여자|남성|여성)?\s*(?:인구|사람|층)?\s*"
    r"(?:만|은|는|도|의|으로|로)?\s*(?:만)?\s*"
    r"(?:(?:보여|알려|바꿔|해)\s*(?:줘요?|주세요|줄래요?)?|어때요?|어떤가요)?\s*[?.!~]*\s*"
)


def _without_ages(text: str) -> str:
    text = agegroup.native_to_digits(text)
    spans = [(a, b) for a, b, _ in qa.age_spans(text)]
    spans += [(a, b) for a, b, _ in agegroup.spans(text)]
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return text


def age_only_changes(question: str, conditions: dict) -> dict | None:
    """'65세 이상만' · '노인은?' · '20대 여자만'처럼 연령(과 성별)만 바꾸는 후속 질문 → 바꿀 조건.
    변경 추출 형식에는 연령이 없어 HCX가 지표('고령인구')로 바꾸거나 새 질문으로 읽어 앞 조건을
    잃었다 (11/26 점검)"""
    if not (conditions.get("indicator") or conditions.get("term")):
        return None
    q = condition_part(question) if chart_shape(question) else question.strip()
    if len(q) > REPLY_MAX_CHARS or not (bands := qa.resolved_ages(q)):
        return None
    if not (said := AGE_ONLY_REST.fullmatch(_without_ages(q))):
        return None
    changes: dict = {"ages": bands}
    if said["sex"]:
        changes["sex"] = "남" if said["sex"] in ("남자", "남성") else "여"
    return changes


def order_changes(question: str, conditions: dict) -> dict | None:
    """순위 뒤 '낮은 순으로': 순서만 바꾸고 개수 · 단위는 그대로"""
    if conditions.get("type") != "rank" or not (said := ORDER_REPLY.fullmatch(question)):
        return None
    return {"ascending": bool(said["low"] or said["asc"])}


def period_length_reply(question: str) -> bool:
    """'10년으로 늘려줘' · '기간을 3년으로'처럼 기간 길이만 말한 후속 질문"""
    return bool(PERIOD_LENGTH_REPLY.fullmatch(question) and recent_years(question))


# 값 · 추이 · 비교 뒤 '최근 5년간 추이를 보여줘' · '지난 10년 변화는?'
RECENT_TREND_REPLY = re.compile(
    rf"\s*{FOLLOW_LEAD}(?:최근|지난)\s*\d{{1,2}}\s*년\s*(?:간|동안)?\s*(?:의\s*)?"
    r"(?:추이|추세|변화|흐름)?\s*(?:는|은|도|를|을|로|으로)?\s*"
    r"(?:어때요?|어떤가요|(?:보여|알려|해|봐)\s*(?:줘요?|주세요|줄래요?)?)?\s*[?.!~]*\s*"
)


def recent_trend_reply(question: str, conditions: dict) -> bool:
    """같은 조건의 최근 N년 추이만 물은 후속 질문. 변경 추출(HCX) 분류에 맡기면 실행마다 달랐다
    (11/26 리뷰). 순위 · 구성비 뒤에는 무엇의 추이인지 정해야 해 변경 추출에 맡긴다"""
    return (
        conditions.get("type") in ("value", "trend", "compare")
        and bool(RECENT_TREND_REPLY.fullmatch(question))
        and bool(recent_years(question))
    )


def _ask_indicator(regions: list[str], rest: str) -> dict:
    several = _several_places(regions, rest)
    place = " · ".join(regions) if several else " ".join(regions)
    first = regions[0] if several else place
    reason = f"{place}의 어떤 통계를 볼지 알려 주세요."
    return {
        "status": "unclear",
        "answer": f"{reason}\n예) 2025년 {first} 인구 / 최근 5년 {first} 출생아 수 추이 / "
        f"{first} 고령인구 비율",
        "parsed": {},
        "result": {"status": "unclear", "reason": reason},
        "sources": [],
        "chart": None,
        "request_type": UNCLEAR,
        "trace": {},
        ASK_INDICATOR: ", ".join(regions) if several else place,
    }


def with_asked_place(question: str, place: str) -> str | None:
    if indicators.find_regions(question) or qa.NATIONWIDE.search(question):
        return None
    return f"{place} {question.strip()}"


AMONG = re.compile(r"^\s*(?:그\s*중에?서?|이\s*중에?서?|그\s*가운데|그\s*안에서|거기서|여기서)\s*")
GROUP_NOUN_BEFORE_RATIO = (
    r"(?:인구|사람들?|주민들?)\s*(?:[의은는이가]\s*)?"
    r"(?=비율|비중|(?:몇\s*)?(?:%|퍼센트|프로))"
)
GENERIC_GROUP_RATIO = re.compile(rf"^(?:남성|여성|남자|여자)?\s*{GROUP_NOUN_BEFORE_RATIO}")
# 질문이 분모를 스스로 말하는 것 ("전체 가구 중", "서울 인구 중"): 직전 대상 안의 비율이 아니다
OWN_BASE = re.compile(r"[가-힣]\s*(?:중에?서?|가운데)\s")


def _with_demographics(term: str, conditions: dict) -> str:
    if ages := conditions.get("ages"):
        said_ages = qa.resolved_ages(term)
        missing = [indicators.age_label(band) for band in ages if list(band) not in said_ages]
        term = " ".join([*missing, term]).strip()
    sex = conditions.get("sex")
    if gender := {"남": "남성", "여": "여성"}.get(sex):
        if not qa.GENDER_WORDS[sex].search(term):
            other = "남" if sex == "여" else "여"
            if dynamic_indicator.PCT_ASK.search(term):
                if qa.GENDER_WORDS[other].search(term):
                    return term
            else:
                term = _without_gender(term, other)
            term = f"{gender} {term}"
    return term


def ratio_followup(question: str, state: dict) -> str | None:
    c = state.get("conditions") or {}
    q = question.strip()
    if len(q) > REPLY_MAX_CHARS + 10 or not dynamic_indicator.PCT_ASK.search(q):
        return None
    if c.get("type") not in ("value", "compare", "trend"):
        return None
    rest = AMONG.sub("", q).strip()
    if OWN_BASE.search(rest):
        # "전체 가구 중 혼자 사는 집 비율은?": 분모를 질문이 말하므로 직전 대상(인구)을 붙이면
        # "총인구 중 전체 가구 중 … 비율"이 되어 분모를 잘못 읽었다 (11/25). 변경 추출(HCX)에 맡긴다
        return None
    # "그중 65세 이상 비율"의 연령은 직전 집단을 나누는 조건이다.
    # 연령을 빼고도 지표 이름이 남으면 새 지표로 처리한다.
    indicator_text = q
    if rest != q:
        rest = qa.agegroup.native_to_digits(rest)
        indicator_text = dynamic_indicator._without_age_words(rest)
        if GENERIC_GROUP_RATIO.match(indicator_text):
            rest = " ".join(re.sub(GROUP_NOUN_BEFORE_RATIO, " ", rest, count=1).split())
            indicator_text = dynamic_indicator._without_age_words(rest)
    if names_indicator(indicator_text, {}):
        return None
    spec = c.get("dynamic_spec") or indicators.spec(c.get("indicator") or "") or {}
    if spec.get("unit") == "%" or spec.get("kind") in ("ratio", "composition"):
        return None
    term = re.sub(r"\s*수$", "", str(c.get("term") or c.get("indicator") or "")).strip()
    if not term or not rest:
        return None
    term = _with_demographics(term, c)
    if (survey := (c.get("asked") or {}).get("survey")) and not qa.survey_said(q):
        if not qa.survey_said(term):
            term = f"{survey} 기준 {term}"
    where = ", ".join(c.get("regions") or []) or "전국"
    return " ".join(p for p in (period_text(c), where, f"{term} 중 {rest}") if p)


PEOPLE_WORDS = re.compile(r"사람|인구|주민")


def asks_other_people(question: str, conditions: dict) -> bool:
    q = qa.without_survey(question)
    if not PEOPLE_WORDS.search(q) or indicators.find_in_text(q):
        return False
    current = [str(conditions.get(k) or "") for k in ("indicator", "term")]
    if conditions.get("indicator") == "총인구":
        return False
    return not any(w and w.replace(" ", "") in q.replace(" ", "") for w in current)


def inherited_question(parsed: dict) -> str:
    where = ", ".join(parsed.get("regions") or [])
    what = parsed.get("term") or parsed.get("indicator") or ""
    if (
        parsed.get("indicator") in qa.BIRTH_RATIO_OPPOSITE
        and (base := parsed.get("birth_ratio_base")) is not None
    ):
        what = qa.birth_ratio_label(parsed["indicator"], base)
    return " ".join(p for p in (period_text(parsed), where, _with_demographics(what, parsed)) if p)


def summary_request(question: str) -> bool:
    return len(question.strip()) <= REPLY_MAX_CHARS and bool(SUMMARY_REQUEST.search(question))


# '지금까지 분석한 내용을 요약해줘' · '다 정리해줘' · '전체 내용 요약해줘': 직전 답이 아니라
# 대화 전체. 범위를 가리키는 말(때 · 양 · 대화)이 요약 · 정리 앞에 온다. '다시'의 '다',
# '전체적으로'(대체로)는 범위가 아니다
WHOLE_TALK = re.compile(
    r"(?:지금까지|여태(?:까지)?|이때까지|이제까지|그동안|처음부터|앞에서|앞서|위에서"
    r"|전체(?!적)|전부|모두|(?<![가-힣])다(?![가-힣])|대화|물어본|나눈|본\s*(?:것|거|내용))"
    r".{0,15}?(?:요약|정리)"
)
HISTORY = 10


def whole_talk_summary_request(question: str) -> bool:
    q = question.strip()
    return (
        len(q) <= CHART_MAX_CHARS
        and bool(WHOLE_TALK.search(q))
        and not indicators.find_in_text(q)
        and not indicators.find_regions(q)
        and not ask_followup.has_explicit_year(q)
    )


TALK_MAX_CHARS = 100
TALK_KINDS = ("summary", "question")


def talk_summary_words(question: str) -> bool:
    """요약 · 정리 말(대화 전체를 가리키는)이 문장 어디에든 있나. 문장을 자르지 않고 LLM 에 맡길지만
    정한다 (split_talk). 지표 · 지역 · 연도가 함께 있어도 막지 않는다 ('지금까지 요약해주고 서울
    출생아 수도 구해줘', 11/27)"""
    q = question.strip()
    return len(q) <= TALK_MAX_CHARS and bool(WHOLE_TALK.search(q))


def parse_requests(raw: str) -> list[dict] | None:
    """split_talk 의 LLM 답 → [{kind, text}] (말한 순서). 쓸 수 없으면 None"""
    objects = qa._json_objects(raw or "")
    items = (objects[0] if objects else {}).get("requests")
    if not isinstance(items, list) or not items:
        return None
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or it.get("kind") not in TALK_KINDS:
            return None
        text = str(it.get("text") or "").strip()
        if it["kind"] == "question" and not text:
            return None
        if it["kind"] == "summary" and any(o["kind"] == "summary" for o in out):
            continue
        out.append({"kind": it["kind"], "text": text if it["kind"] == "question" else ""})
    return out


async def split_talk(question: str, trace: dict) -> list[dict] | None:
    """'앞에서 본 거 정리해주고 혼인도 구해줘'처럼 대화 정리와 새 질문이 섞인 말 → 요청 목록
    (LLM 1회). 정규식으로 자르면 어순이 섞인 말을 놓쳐 LLM 에 맡긴다 (11/27 세진). 실패하면 None"""
    try:
        raw = await _chat(question, TALK_SPLIT_SYSTEM)
    except Exception as e:
        trace["talk_split"] = {"error": f"{type(e).__name__}: {e}"[:160]}
        return None
    reqs = parse_requests(raw)
    trace["talk_split"] = {"raw": raw[:300], "requests": reqs}
    return reqs


async def _talk_summary(state: dict, question: str, user_id: int | None):
    """대화 정리: 로그인 사용자는 대화 보고서(_report_turn), 담을 답이 없거나 로그인하지 않았으면
    대화 전체 요약 글"""
    if user_id is not None:
        res, sources, notice = await _report_turn(state, question, user_id)
        if res.get("answer") != REPORT_NO_TURNS:
            return res, sources, notice
    flow.add("후속 질문", "대화 전체 요약", f"답 {len(state.get('history') or [1])}개")
    return _reuse(state, whole_talk_text(state), None, SUMMARY), state["sources"], None


# 정리 + 질문으로 나눈 뒤 질문 부분을 다시 _turn 에 넣을 때 또 나누지 않게
_talk_split_inner: ContextVar[bool] = ContextVar("ask_talk_split_inner", default=False)


def whole_talk_text(state: dict) -> str:
    said = state.get("history") or [answer_line(state["last"])]
    if len(said) == 1:
        return said[0]
    return "\n".join([f"지금까지 본 내용 {len(said)}가지입니다.", *(f"- {s}" for s in said)])


def answer_line(last: dict) -> str:
    """답 하나의 핵심 문장 (요약 대상): 요약이 있으면 그 첫 문장, 없으면 답의 첫 단락"""
    return (last.get("summary") or {}).get("headline") or lead(last.get("answer") or "")


def redraw(chart: dict | None, view: str, status: str | None) -> tuple[dict | None, str]:
    """직전 차트를 view 모양으로. 그 모양으로 못 그리면 그릴 수 있는 기본 모양과 이유를 준다.
    되묻지 않는다: 무엇을 원하는지(그림 바꾸기)는 분명하다"""
    if not chart:
        prefix = f"{CHART_CANNOT[view]}. " if view in ("scatter", "pie") else ""
        if status == "ok":
            return None, (
                f"{prefix}직전 답은 값이 하나라 그래프로 그릴 수 없습니다. "
                "'최근 5년 추이로'처럼 기간을 넓히거나 '서울도 같이'처럼 지역을 더하면 "
                "그래프로 보여 드립니다."
            )
        return None, f"{prefix}직전 답에는 그래프로 그릴 수치가 없습니다."
    shown, cannot = reshape(chart, view)
    if cannot:
        return shown, f"{cannot}. {chart_name(shown)}로 보여 드립니다."
    if view == "table":
        return shown, "표로 바꿨습니다."
    if view == "chart":
        return shown, f"{chart_name(shown)}로 보여 드립니다."
    return shown, f"{chart_name(shown, view)}로 바꿨습니다."


def chart_name(chart: dict, view: str | None = None) -> str:
    if charts.crowded(chart):
        return "지역별 꺾은선 그래프"
    return CHART_NAMES["bar" if view == "bar" and chart["type"] == "line" else chart["type"]]


def reshape(chart: dict, view: str) -> tuple[dict, str | None]:
    """차트를 view 모양으로 → (차트, 못 그린 이유). 못 그리면 그릴 수 있는 기본 모양으로 둔다"""
    kind = chart["type"]
    shown = {**chart, "view": "bar" if kind == "parts" else kind}
    if view == "chart":
        return shown, None
    if charts.view_fits(chart, view):
        return {**chart, "view": view}, None
    if view == "bar" and kind == "line":
        count = len(chart["series"])
        return shown, f"{count}개 지역을 막대로 그리면 겹쳐 읽기 어렵습니다"
    return shown, CHART_CANNOT[view]


def lead(answer: str) -> str:
    return answer.split("\n\n")[0].strip()


DETAIL_REQUEST = re.compile(r"자세히|자세하게|상세히|상세하게|구체적으로|풀어서")
SPLIT_ASK = re.compile(r"나눠|나누|쪼개|구분해|별\s|별$")
NEW_VIEW = re.compile(r"추이|추세|비교|순위|몇\s*위|가장|제일|구성|별로|마다|어디(?!서)")


def about_last_answer(question: str) -> bool:
    q = question.strip()
    return not (
        len(q) > REPLY_MAX_CHARS
        or indicators.find_in_text(q)
        or ask_followup.has_explicit_year(q)
        or SEX_WORDS.search(q)
        or NEW_VIEW.search(q)
        or SPLIT_ASK.search(q)
        or indicators.find_regions(q)
    )


def detail_request(question: str) -> bool:
    return bool(DETAIL_REQUEST.search(question)) and about_last_answer(question)


_END = r"[\s.!~?ㅎㅋ^]*$"
HELLO = re.compile(r"^\s*(안녕(하세요|하십니까)?|하이|헬로|hi|hello|ㅎㅇ)" + _END, re.I)
THANKS = re.compile(
    r"^\s*(정말\s*|너무\s*)?(고마워요?|고맙습니다|감사(합니다|해요|드려요)?|땡큐|thanks?|thank you"
    r"|ㄳ|ㄱㅅ|수고(하세요|했어요|하셨습니다)?)" + _END,
    re.I,
)
OKAY = re.compile(
    r"^\s*(네|넵|넹|응|ㅇㅇ|ㅇㅋ|오케이|ok|okay|알겠어요?|알겠습니다|알았어요?|좋아요?|굿|good"
    r"|잘\s*가요?|바이|bye)" + _END,
    re.I,
)


def chat_reply(question: str) -> str | None:
    more = "더 궁금한 통계가 있으면 이어서 질문해 주세요."
    if HELLO.search(question):
        return "안녕하세요. 인구 통계를 질문해 주세요 (예: 최근 5년 부산 출생아 수 추이)."
    if THANKS.search(question):
        return f"도움이 되었다니 다행입니다. {more}"
    if OKAY.search(question):
        return f"네. {more}"
    return None


SOURCE_REQUEST = re.compile(
    r"출처|근거|어디서\s*(나온|나왔|가져|난|온)"
    r"|기준이?\s*(뭐|언제|어떻게)|언제\s*기준|계산식|어떻게\s*(계산|구한|구했)"
)
WHICH_DATA = r"(어느|무슨|어떤)\s*(통계표?|자료|조사|데이터)"
WHICH_DATA_ONLY = re.compile(
    rf"^\s*{WHICH_DATA}\s*(이?야|예요|이에요|인가요?|이?지|인지|죠)?\s*[?.!]*$"
)
WHICH_DATA_USED = re.compile(
    rf"(^|\s)(이|그|이거|그거|이건|그건|이게|그게|방금|위|직전|앞)(\s|의\s).*{WHICH_DATA}"
    rf"|{WHICH_DATA}\S*\s*(에서\s*)?(나온|나왔|쓴|썼|가져|만든|계산)"
)
LOOKING_FOR = re.compile(r"있(어|나|니|을까|습니까|는지)|보면|알\s*수|찾")
SOURCE_LINE = re.compile(r"^(※ 계산식:|출처:|통계표:)")


def source_request(question: str) -> bool:
    asked = SOURCE_REQUEST.search(question) or (
        (WHICH_DATA_ONLY.search(question) or WHICH_DATA_USED.search(question))
        and not LOOKING_FOR.search(question)
    )
    return bool(asked) and about_last_answer(question)


def source_text(last: dict) -> str | None:
    lines = [ln for ln in (last.get("answer") or "").split("\n") if SOURCE_LINE.match(ln)]
    if not lines or not last.get("sources"):
        return None
    return "\n".join(["직전 답은 아래 통계표의 수치로 만들었습니다.", *dict.fromkeys(lines)])


WHY_REQUEST = re.compile(r"(^|\s)왜(\s|[?.!~]|$|그래|그런|이래|죠|지\b)|이유|원인|때문|어째서")
NO_CAUSE = "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 수치 변화만 보여드립니다."


def why_request(question: str) -> bool:
    return bool(WHY_REQUEST.search(question)) and about_last_answer(question)


VIEW_ASK = re.compile(r"추이|변화|추세|비교|차이|순위|몇\s*위|가장|제일|구성|별로|마다|어디")
KEEP_VIEW = ("type", "year", "start_year", "end_year", "level", "sido", "top", "ascending")


TABLE_ASK = re.compile(rf"{WHICH_DATA}|통계표|(통계|자료|조사)(가|는|도)?\s*있")


def table_question(question: str) -> bool:
    return (
        bool(TABLE_ASK.search(question))
        and not indicators.find_in_text(question)
        and not source_request(question)
    )


def summary_text(last: dict, question: str) -> str:
    if last.get("summary"):
        return answer_summary.text(last["summary"], answer_summary.one_line(question))
    return lead(last["answer"])


def state_text(conditions: dict, sources: dict) -> str:
    kinds = {
        "value": "값 하나",
        "trend": "연도별 추이",
        "compare": "지역 비교",
        "rank": "지역 순위",
    }
    rows = [f"{row['name']}: {row['value']}" for row in applied(conditions, sources)]
    kind = conditions.get("type")
    rows.append(f"보는 방식: {kinds.get(kind, '구성비' if kind == 'composition' else kind)}")
    if kind == "rank":
        order = "낮은 순" if conditions.get("ascending") else "높은 순"
        rows.append(f"순위: {order}, {conditions.get('top') or '전체'}개")
    return "\n".join(rows)


def last_text(last: dict | None) -> str:
    if not last:
        return "(없음)"
    lines = [lead(last.get("answer") or "")]
    rows = (last.get("chart") or {}).get("rows") or []
    lines += [f"{i}. {r['label']}: {r['value']}" for i, r in enumerate(rows[:10], start=1)]
    return "\n".join(line for line in lines if line)


def parse_delta(raw: str) -> dict:
    objects = qa._json_objects(raw)
    data = objects[0] if objects else {}
    kind = data.get("request_type")
    changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
    unclear = data.get("unclear")
    unclear = unclear.strip() if isinstance(unclear, str) and unclear.strip() else None
    if kind not in (CHANGE, CHART, SUMMARY, NEW, REPORT):
        kind = NEW
    chart = data.get("chart") if data.get("chart") in CHART_VIEWS else None
    # HCX가 그림 모양을 조건에 넣기도 한다 ("type": "bar" · "chart": "table"). 맞지 않는 type이
    # 남으면 apply_changes가 보는 방식을 다시 맞추는 단계(_retype)를 건너뛴다
    changes = dict(changes)
    for key in ("type", "chart"):
        if key in changes and (key == "chart" or changes[key] not in TYPES):
            value = changes.pop(key)
            chart = chart or (value if value in CHART_VIEWS else None)
    if kind == CHANGE and not changes and not unclear:
        kind = CHART if chart else NEW
    return {"request_type": kind, "changes": changes, "chart": chart, "unclear": unclear}


RESTATE_RELATIONS = ("followup", "new", "unclear", REPORT)
RELATIVE_WORD = re.compile(r"(?:작년|지난해|재작년|올해|금년)(?:에|의|은|는)?\s*")


async def restate(state: dict, question: str, trace: dict) -> dict:
    """이어 묻기 LLM 복원 (FOLLOW_RESTATE, A_16): 직전 질문 · 조건 · 답을 주고 relation 과
    혼자 서는 완전한 질문을 받는다 → {"relation", "question", "why"}. 못 읽으면 relation 은 빈 글"""
    recent = state.get("recent") or []
    system = (
        RESTATE_SYSTEM.replace("{PREV_QUESTION}", recent[-1] if recent else "(없음)")
        .replace("{STATE}", state_text(state["conditions"], state["sources"]))
        .replace("{LAST}", last_text(state.get("last")))
    )
    raw = await _follow_chat(question, system)
    trace["restate_raw"] = raw
    return parse_restate(raw)


def parse_restate(raw: str) -> dict:
    objects = qa._json_objects(raw)
    data = objects[0] if objects else {}
    relation = str(data.get("relation") or "").strip().lower()
    return {
        "relation": relation if relation in RESTATE_RELATIONS else "",
        "question": " ".join(str(data.get("question") or "").split()),
        "why": str(data.get("why") or "").strip(),
    }


def verify_restate(r: dict, conditions: dict, question: str) -> tuple[str, str, str]:
    """LLM 결정의 코드 확인 (뜻 판단은 하지 않는다) → (relation, 쓸 질문, 확인 메모).
    · 읽지 못했거나 문장이 비면 새 말 그대로 새 질문
    · followup 인데 복원 문장에 직전 지표(이름 · 별칭 · 말)가 없으면 새 질문으로
    · followup 인데 연도가 빠졌고 직전에 연도가 있으면 직전 기간을 앞에 붙인다 (상대 시점 그대로)"""
    relation, restored = r.get("relation") or "", r.get("question") or ""
    if relation == "unclear":
        return relation, "", ""
    if not relation or not restored:
        return "new", question, "복원 못 읽음 → 새 말 그대로"
    if (n := recent_years(question)) and not names_indicator(question, {}):
        # "최근 5년은?"의 기간도 코드가 센다: HCX가 "2023년부터 2020년까지"처럼 거꾸로 적었다 (C08).
        # 숫자 연도를 지우고 '최근 N년'을 앞에 두면 새 질문 경로가 자료 마지막 해까지 N개 해로 푼다
        bare = re.sub(r"\d{4}\s*년\s*(?:부터|에서|~|까지|과|와|이랑|에)?\s*", "", restored)
        # LLM이 이미 쓴 '최근 N년(간)'도 지운다: 앞에 다시 붙이면 '최근 10년 최근 10년간'이 된다
        bare = re.sub(r"최근\s*\d{1,2}\s*년\s*(?:간|동안|치)?\s*", "", bare)
        bare = re.sub(r"\s{2,}", " ", bare).strip()
        return relation, f"최근 {n}년 {bare}", f"상대 기간은 코드로 최근 {n}년"
    if year := follow_year(question, conditions):
        # 새 말의 상대 시점("작년은?")은 코드가 오늘 기준으로 센다 (상대 연도 원칙). HCX는 직전
        # 연도에서 1을 빼 2023년의 작년을 2022년으로 썼다 (11/25 대화 셋 C06 · C25)
        if re.search(r"\d{4}\s*년", restored):
            restored = re.sub(r"\d{4}(\s*년)", lambda m: f"{year}{m[1]}", restored, count=1)
        else:
            bare = RELATIVE_WORD.sub("", restored).strip()
            restored = f"{year}년 {bare}"
        return relation, restored, f"상대 시점은 코드로 {year}년"
    if relation == "followup":
        prev = str(conditions.get("indicator") or conditions.get("term") or "")
        names = {n for *_, n in indicators.find_in_text(restored) or []}
        compact = restored.replace(" ", "")
        if prev and prev not in names and prev.replace(" ", "") not in compact:
            return "new", restored, f"복원 문장에 직전 지표({prev})가 없음 → 새 질문"
        if not re.search(
            r"\d{4}\s*년|작년|지난해|재작년|올해|금년|최근|년\s*전|년간|년\s*동안", restored
        ):
            if period := period_text(conditions):
                return relation, f"{period} {restored}", f"직전 기간 {period} 붙임"
    return relation, restored, ""


# unclear 일 때 사용자에게 보이는 문장. LLM 의 why 는 판단 메모라 화면에 내지 않고 기록에만 둔다
# (11/25 경계 시험: "더" → "새 말이 불완전함 바꿀 조건을…"이 그대로 나왔다)
RESTATE_UNCLEAR = "앞 대화에서 무엇을 가리키는지 알 수 없습니다."


def restate_outcome(r: dict, raw: str | None) -> str:
    """복원 결과를 어떻게 쓸까 → "ok" · "unclear" · "failed".
    · failed: 호출 실패 · JSON 이 없음 · 문장은 있는데 relation 이 셋 중 하나가 아님 → 기존 경로로
    · unclear: JSON 은 읽혔는데 문장을 채우지 못함 → 되묻기. LLM 이 무엇을 가리키는지 못 찾은 것이다
      (11/25 대화 셋 C25 "거기는 작년에?": why "새로 언급한 대상이 없음", 문장 비움)"""
    if not qa._json_objects(raw or ""):
        return "failed"
    if r.get("relation") == REPORT:  # 지금까지의 답을 묶어 달라는 말: 문장이 없는 게 정상
        return REPORT
    if r.get("relation") == "unclear" or not r.get("question"):
        return "unclear"
    return "ok" if r.get("relation") else "failed"


async def _sure_rule(state: dict, question: str) -> str | None:
    """3-a: LLM 복원보다 먼저 쓸 '확실한' 코드 규칙이 이 말을 잡는가 → 규칙 이름 (아니면 None).
    11/25 대화 셋(끔)에서 오판이 없던 셋만: 비율 후속(C10 · C12 · C13) · 지역만(C01~C05) ·
    구별로(C19). 구성비로는 규칙도 틀려(C20) 넣지 않는다. 기존 경로가 구성비로를 먼저 보므로
    그 말이면 규칙으로 보내지 않는다. 직전 답이 정상(ok)이 아니면(거절 · 표 안내 뒤) 쓰지 않는다:
    거절 뒤 "부산은?"이 거절된 조건(총인구)을 이어받았다 (C27).
    develop 의 후속 질문 규칙(출생성비 반대로 · N명 기준 · 연도만 · 끝 연도를 정한 기간)도
    기존 경로가 코드로 처리하므로 같은 조건에서 복원보다 먼저 본다. 판정은 기존 경로와 같은 식"""
    if (state.get("last") or {}).get("status") != "ok" or composition_request(question):
        return None
    if qa.BIRTH_RATIO_OPPOSITE.get(state["conditions"].get("indicator")):
        if BIRTH_RATIO_REVERSE_REPLY.fullmatch(question):
            return "출생성비 반대로"
        if BIRTH_RATIO_COUNT_REPLY.fullmatch(question):
            return "출생성비 기준 인원"
    if YEAR_ONLY_REPLY.fullmatch(question):
        return "연도만"
    if await asyncio.to_thread(ratio_followup, question, state):
        return "비율 후속"
    if await asyncio.to_thread(region_split_changes, question, state["conditions"]):
        return "구별로"
    if await asyncio.to_thread(region_only_changes, question, state["conditions"]):
        return "지역만"
    if not names_indicator(question, {}) and any(
        r["kind"] == "span_until" for r in timeref.find(question)
    ):
        return "끝 연도 기간"
    return None


async def _restated(
    state: dict, question: str, user_id: int | None
) -> tuple[dict, dict, str | None] | None:
    """FOLLOW_RESTATE: LLM 복원 → 코드 확인 → 복원 문장을 새 질문 경로로.
    복원을 쓸 수 없으면 None: 호출한 _turn 이 기존 경로(코드 규칙 + 변경 추출)로 이어 간다.
    새 말만 새 질문으로 보내면 직전 대화를 잃는다
    (11/25 HCX 42901에서 "2020년은?"이 맥락 없이 감)"""
    trace: dict = {}
    try:
        r = await restate(state, question, trace)
    except Exception:
        logger.exception("이어 묻기 복원 실패 → 기존 경로")
        r = {"relation": "", "question": "", "why": "호출 실패"}
    outcome = restate_outcome(r, trace.get("restate_raw"))
    if outcome == "failed":
        # LLM 이 실제로 무엇을 냈는지 기록에 남긴다 (다음 경로의 trace 에는 restate 가 붙지 않는다)
        raw = trace.get("restate_raw")
        said = f"원문: {raw}" if raw else r.get("why") or "응답 없음"
        flow.add("이어 묻기 판단", "실패", f"{said} → 기존 경로")
        return None
    if outcome == REPORT:
        flow.add("이어 묻기 판단", REPORT, r.get("why") or "지금까지의 답을 묶어 달라는 말")
        return await _report_turn(state, question, user_id)
    if outcome == "unclear":
        r = {**r, "relation": "unclear"}
    relation, restored, note = verify_restate(r, state["conditions"], question)
    flow.add("이어 묻기 판단", relation, " · ".join(x for x in (restored, r.get("why"), note) if x))
    if relation == "unclear":
        res = _unclear(state, RESTATE_UNCLEAR)
        res["trace"] = {"restate": {**r, "note": note, **trace}}
        return res, state["sources"], None
    res, sources, notice = await _new(restored, state)
    res["request_type"] = CHANGE if relation == "followup" else NEW
    res["question"] = restored
    if isinstance(res.get("trace"), dict):
        res["trace"]["restate"] = {**r, "note": note, "asked": question, **trace}
    return res, sources, notice


async def extract(state: dict, question: str, trace: dict) -> dict:
    system = (
        FOLLOW_SYSTEM.replace("{STATE}", state_text(state["conditions"], state["sources"]))
        .replace("{LAST}", last_text(state.get("last")))
        .replace("{INDICATORS}", ", ".join(indicator_names(state["conditions"])))
    )
    raw = await _chat(question, system)
    trace["follow_raw"] = raw
    return parse_delta(raw)


def indicator_names(conditions: dict) -> list[str]:
    names = list(indicators.specs())
    current = conditions.get("indicator")
    if conditions.get("dynamic_spec") and current and current not in names:
        names.append(current)
    return names


async def _follow_chat(prompt: str, system: str) -> str:
    """이어 묻기 복원 LLM (FOLLOW_LLM). hcx 는 변경 추출과 같은 _chat, gemma 는
    재계획 · 판정과 같은 GemmaClient. gemma 실패를 HCX 로 넘기지 않는다
    (부른 쪽이 실패로 처리한다)"""
    if get_settings().follow_llm != "gemma":
        return await _chat(prompt, system)
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        qa.MODEL,
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        thinking={"effort": "none"},
        maxCompletionTokens=FOLLOW_MAX_TOKENS,
        temperature=0,
    )
    return result.content


@llm_calls.staged("follow_up")
async def _chat(prompt: str, system: str) -> str:
    result = await HcxClient().chat(
        qa.MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        thinking={"effort": "none"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=FOLLOW_MAX_TOKENS,
        temperature=0.1,
        repetitionPenalty=1.1,
    )
    return result.content


def _reuse(state: dict, answer: str, chart: dict | None, request_type: str) -> dict:
    last = state["last"]
    return {
        "summary": last.get("summary"),
        "status": last.get("status"),
        "answer": answer,
        "parsed": state["conditions"],
        "result": {"status": last.get("status"), "reused": True},
        "sources": last.get("sources") or [],
        "chart": chart,
        "request_type": request_type,
        "trace": {},
    }


def _unclear(state: dict, reason: str) -> dict:
    return {
        "status": "unclear",
        "answer": f"{reason} 바꿀 조건을 다시 말씀해 주세요 (예: 서울도 같이, 2022년만, 남자만).",
        "parsed": state["conditions"],
        "result": {"status": "unclear", "reason": reason},
        "sources": [],
        "chart": None,
        "request_type": UNCLEAR,
        "trace": {},
    }


def _chat_answer(state: dict | None, reply: str) -> dict:
    return {
        "status": "chat",
        "answer": reply,
        "parsed": (state or {}).get("conditions") or {},
        "result": {"status": "chat"},
        "sources": [],
        "chart": None,
        "request_type": CHAT,
        "trace": {},
    }


def _nothing_to_summarize() -> dict:
    reason = "요약할 답이 아직 없습니다."
    return {
        "status": "unclear",
        "answer": f"{reason} 먼저 통계 질문을 해 주세요 (예: 최근 5년 부산 출생아 수 추이).",
        "parsed": {},
        "result": {"status": "unclear", "reason": reason},
        "sources": [],
        "chart": None,
        "request_type": UNCLEAR,
        "trace": {},
    }


async def _answer(conditions: dict, trace: dict | None = None) -> dict:
    res = await qa.answer(conditions, trace if trace is not None else {})
    res["chart"] = charts.build(res.get("result"))
    return res


HOUSEHOLD_AGE_TABLE = "DT_1PL1502"
HOUSEHOLD_AGE_RATIO = re.compile(
    r"^\s*(?P<year>(?:19|20)\d{2})\s*년\s+(?P<region>.+?)\s+"
    r"(?:전체\s*)?1\s*인\s*가구(?:\s*수)?\s*(?:중(?:에서|에)?|가운데)\s*"
    r"(?P<age>.+?)\s*(?:의\s*)?(?:비율|비중)(?:은|이|는)?"
    r"(?:\s*(?:얼마|몇\s*퍼센트)(?:야|예요)?)?\s*[?.!]*\s*$"
)


async def _household_age_ratio(question: str) -> dict | None:
    match = HOUSEHOLD_AGE_RATIO.fullmatch(question)
    if not match:
        return None
    age = qa.agegroup.native_to_digits(match["age"].strip())
    spans = qa.age_spans(age)
    if len(spans) != 1 or spans[0][:2] != (0, len(age)):
        return None
    band = spans[0][2]
    region, year = match["region"].strip(), int(match["year"])
    try:
        table = await run_in_threadpool(lookup._table, HOUSEHOLD_AGE_TABLE)
        start, end = table.get("start_year"), table.get("end_year")
        if start is None or end is None or not start <= year <= end:
            return None
        resolved = await run_in_threadpool(indicators.resolve_region, region, year, sigungu=True)
    except Exception:
        logger.exception("인구총조사 1인 가구 비율의 표·지역 확인 실패")
        return None
    if resolved["status"] != "ok":
        return None

    term = f"1인 가구 중 {indicators.age_label(band)} 비율"
    parsed = {
        "type": "value", "indicator": term, "term": term, "regions": [region],
        "year": year, "ages": [band], "sex": "계",
    }  # fmt: skip
    canonical = inherited_question(parsed)
    trace: dict = {"dynamic": []}
    try:
        res, _ = await dynamic_indicator._try(
            HOUSEHOLD_AGE_TABLE,
            "101",
            canonical,
            parsed,
            trace,
            {},
            {"table_name": table["tbl_nm"]},
        )
    except Exception:
        logger.exception("인구총조사 1인 가구 비율 계산 실패")
        res = None
    if res is not None:
        res["chart"] = charts.build(res.get("result"))
        return res

    found = {
        "status": "ok",
        "answer": (
            "해당 비율의 분자와 분모는 인구총조사의 "
            "성 및 연령별 1인 가구 표에서 확인할 수 있습니다."
        ),
    }
    res = table_guide.tables_answer(found, {"parsed": parsed})
    res["trace"] = trace
    res["sources"] = [
        {
            "name": f"인구총조사, {table['tbl_nm']} ({HOUSEHOLD_AGE_TABLE})",
            "tbl_id": HOUSEHOLD_AGE_TABLE,
            "basis": "인구총조사",
            "url": indicators.KOSIS_URL.format(org_id="101", tbl_id=HOUSEHOLD_AGE_TABLE),
        }
    ]
    await _attach_cards(res, canonical)
    return res


async def _new(
    question: str,
    state: dict | None,
    *,
    scoped_age_ratio: bool = False,
    said_alone: str = "",
) -> tuple[dict, dict, str | None]:
    kept: dict[str, Any] = {}
    prev = state["conditions"] if state and state.get("conditions") else None

    if census := await _household_age_ratio(question):
        census.update(request_type=NEW, question=question)
        return census, sources_of(question, census["parsed"]), None

    def adjust(parsed: dict) -> dict:
        kept["original"] = parsed
        kept["sources"] = sources_of(question, parsed)
        if prev:
            parsed, kept["sources"] = inherit(
                parsed, kept["sources"], prev, state["sources"], question, said_alone
            )
        if scoped_age_ratio:
            parsed.update(unsupported=True, cross=None, indicators=None, dynamic_spec=None)
            parsed["scoped_age_ratio"] = True
            if parsed.get("type") not in ("trend", "compare"):
                parsed["type"] = "value"
        return parsed

    if refused := await table_guide.off_domain_early(question):
        flow.add("범위 검사", refused.get("status"), "인구 통계에 없는 주제 (질문 이해 전에 거절)")
        refused["request_type"], refused["question"] = NEW, question
        return refused, {}, None
    res = await qa.ask(question, adjust=adjust)
    if alt := await _composite(question, res, state):
        res = alt
    parsed = res.get("parsed") or {}
    sources = kept.get("sources") or sources_of(question, parsed)
    notice = inherited_notice(parsed, sources)
    if parsed.get("housing_condition"):
        when = period_text(parsed) if sources.get("period") == "inherited" else None
        regions = parsed.get("regions") or [] if sources.get("regions") == "inherited" else []
        res["search_question"] = " ".join(filter(None, (when, ", ".join(regions), question)))
    elif sources.get("indicator") == "inherited" and not scoped_age_ratio:
        res["search_question"] = inherited_question(parsed)
    if res.get("status") in {
        "out_of_range",
        "region_not_in_year",
        "missing_data",
        "no_region_level",
    }:
        inherited = res.get("parsed") or {}
        for conditions, retry_sources, dropped in without_inherited(
            inherited, kept.get("original") or {}, sources
        ):
            again = await qa.answer(conditions, res.get("trace"), question)
            if again.get("status") != "ok":
                continue
            res, sources = again, retry_sources
            messages = [inherited_notice(res["parsed"], sources)]
            if "period" in dropped:
                messages.append("앞 질문의 기간으로는 자료가 없어 기본 기간으로 답했습니다.")
            if "regions" in dropped:
                old = ", ".join(inherited.get("regions") or [])
                new = ", ".join(conditions.get("regions") or []) or "전국"
                messages.append(f"앞 질문의 지역({old})으로는 자료가 없어 {new}으로 답했습니다.")
            notice = " ".join(filter(None, messages))
            break
    res["chart"] = charts.build(res.get("result"))
    res["request_type"] = NEW
    return res, sources, notice


async def _composite(question: str, res: dict, state: dict | None) -> dict | None:
    """복합 질문(A_20): Gemma 판별이 복합이라 하면 통계표를 찾아 계산 계획(steps)으로 답한다.
    계획이 단순하거나(연산 하나) 못 세우거나 값을 못 내면 None (사전 답이 그대로)"""
    from app.services import composite

    parsed = res.get("parsed") or {}
    if res.get("clarification") or res.get("status") in table_guide.REFUSED:
        return None
    trace = res.get("trace") or {}
    ranked_scope = composite.RANKED_SCOPE.search(question)
    if not ranked_scope and not await composite.is_composite(question, trace):
        return None
    flow.add("복합 판별", "복합", str((trace.get("composite_gate") or {}).get("why") or "")[:120])
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    try:
        found = _keep(await table_guide.find_tables(question, sid, None, None))
        alt = await composite.answer(question, parsed, found, trace)
    except Exception:
        logger.exception("복합 질문 계산 실패")
        alt = None
    if alt is None:
        if not ranked_scope:
            return None
        flow.add("복합 계산", "답변 불가", "요청한 순위에 해당하는 지역의 값을 확인하지 못함")
        return {
            "status": "partial_match",
            "answer": "요청하신 순위에 해당하는 지역을 골라 해당 통계를 계산하지 못했습니다.",
            "parsed": parsed,
            "result": {"status": "partial_match", "final": True},
            "sources": [],
            "trace": trace,
        }
    if dropped := _indicators_dropped_by(res, alt):
        # 사전이 물은 지표를 모두 답했는데 복합 계획의 답이 그 일부만 담으면 사전 답을 둔다.
        # 11/27 PR 2: '출생아 수와 사망자 수 상위 5개 시도'를 사전이 지표마다 목록으로 냈는데
        # 복합 계획(get → rank → get → rank)이 마지막 단계(사망자 수)만 남겼다
        trace["composite_kept_dictionary"] = {"dropped": sorted(dropped)}
        return None
    alt["search_session"] = sid
    alt["trace"] = trace
    return alt


def _answered_indicators(result: dict | None) -> set[str]:
    result = result or {}
    if result.get("split_by") == "indicator":
        return {p.get("indicator") for p in result.get("split") or [] if p.get("status") == "ok"}
    if result.get("status") == "ok" and result.get("indicator"):
        return {result["indicator"]}
    return set()


def _indicators_dropped_by(res: dict, alt: dict) -> set[str]:
    """사전 답이 지표마다 답한 것 중 복합 답에 없는 지표 (복합 답이 사전 답의 일부일 때만)"""
    if res.get("status") != "ok":
        return set()
    mine = _answered_indicators(res.get("result"))
    theirs = _answered_indicators(alt.get("result"))
    if len(mine) < 2 or not theirs or not theirs < mine:
        return set()
    return mine - theirs


async def _separate_measures(question, measures, state, user_id):
    if refused := await table_guide.off_domain_early(question):
        flow.add("범위 검사", refused.get("status"), "인구 통계에 없는 주제 (질문 이해 전에 거절)")
        refused.update(request_type=NEW, question=question)
        return refused, {}, None

    async def answer_measure(asked):
        res, sources, _ = await _new(asked, state)
        await _attach_tables(res, None, asked, user_id)
        return res, sources

    outcomes = await asyncio.gather(
        *(answer_measure(asked) for _, asked in measures), return_exceptions=True
    )
    answers = []
    primary = None
    measure_conditions = {}
    errors = []
    for (name, _), outcome in zip(measures, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            logger.error("묶음 질문 계산 실패: %s", name, exc_info=outcome)
            errors.append(outcome)
            failure = {}
            if isinstance(outcome, HcxError) and outcome.is_rate_limit:
                wait = f" (약 {math.ceil(outcome.retry_after)}초 뒤)" if outcome.retry_after else ""
                failure = {
                    "status": "rate_limited",
                    "answer": "요청 한도에 도달했습니다. 잠시 후 다시 질문해 주세요." + wait,
                }
            answers.append((name, failure))
            continue
        res, sources = outcome
        answers.append((name, res))
        if res.get("status") == "ok" and res.get("result") and not res.get("clarification"):
            if primary is None:
                primary = res, sources
            measure_conditions[name] = effective(res.get("parsed") or {}, res["result"])

    if primary is None:
        first = next((out for out in outcomes if not isinstance(out, BaseException)), None)
        if first is None:
            raise errors[0]
        res, sources = first
        if not res.get("clarification"):
            res["answer"] = "\n\n".join(
                f"{name}\n{sub.get('answer') or '계산하지 못했습니다.'}" for name, sub in answers
            )
            res["question"] = question
        return res, sources, None

    res, sources = primary
    parsed = {**res["parsed"], "grounded": True}
    if len(measure_conditions) > 1:
        parsed["measure_conditions"] = measure_conditions
    combined = multi_indicator.side_by_side(
        question, parsed, answers, res.get("trace") or {}, allow_partial=True
    )
    combined.update(question=question, request_type=NEW, chart=charts.build(combined["result"]))
    return combined, sources, None


def _clarification_answer(question: str, clarification: dict) -> dict:
    return {
        "status": "clarify_question",
        "answer": clarification["prompt"],
        "parsed": {},
        "result": {"status": "clarify_question"},
        "sources": [],
        "chart": None,
        "trace": {},
        "question": question,
        "request_type": REPLY,
        "clarification": clarification,
    }


async def _clarified(question: str, state: dict):
    regions = await asyncio.to_thread(indicators.find_regions, question)
    if clarification := question_clarify.detect(question, regions):
        return _clarification_answer(question, clarification), {}, None
    res, sources, notice = await _new(question, state.get("clarification_context"))
    res["request_type"], res["question"] = REPLY, question
    return res, sources, notice


def _one_place(name: str) -> bool:
    return indicators.resolve_region(name)["status"] == "ok"


def _gave_up_asking(pending: dict) -> bool:
    return (pending.get("count") or 1) >= question_clarify.MAX_ASKS


async def _after_asking(original: str, kind: str, user_id: int | None):
    if kind == "move_scope":
        told = f"범위를 알아듣지 못해 '{question_clarify.MOVE_OPTIONS[1]}'으로 답했습니다."
        return await _movement(original, "exclude", user_id, told)
    if kind in ("table_cell", "alternative"):
        return await _find_tables(original, None, user_id)
    return (
        {
            "status": "unknown_region",
            "answer": "어느 지역인지 알 수 없어 답하지 못했습니다. "
            "지역 이름을 넣어 다시 질문해 주세요 (예: 수원시 인구).",
            "parsed": {},
            "result": {"status": "unknown_region"},
            "sources": [],
            "chart": None,
            "trace": {},
            "question": original,
            "request_type": REPLY,
        },
        {},
        None,
    )


MOVE_INDICATOR = {"exclude": "전출자 수", "include": "이동자 수", "nation": "이동자 수"}
MOVE_TYPES = ("value", "trend", "compare", "rank")


async def _movement(question: str, scope: str, user_id: int | None, told: str | None = None):
    indicator = MOVE_INDICATOR[scope]

    def adjust(parsed: dict) -> dict:
        kind = parsed.get("type") if parsed.get("type") in MOVE_TYPES else "value"
        return {**parsed, "indicator": indicator, "type": kind}

    res = await qa.ask(question, adjust=adjust)
    if res.get("status") == "ok":
        res["chart"] = charts.build(res.get("result"))
        res.update(question=question, request_type=REPLY)
        return res, sources_of(question, res.get("parsed") or {}), told
    return await _movement_tables(question_clarify.merge_scope(question, scope), user_id, told)


async def _find_tables(question: str, state: dict | None, user_id: int | None):
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    found = await table_guide.find_tables(question, sid, None, user_id)
    res = table_guide.tables_answer(found)
    res.update(question=question, request_type=NEW, search_session=sid)
    return res, {}, None


async def _movement_tables(question: str, user_id: int | None, told: str | None = None):
    sid = table_guide.new_search_session()
    found = await table_guide.find_tables(question, sid, None, user_id)
    res = table_guide.tables_answer(found)
    res.update(question=question, request_type=REPLY, search_session=sid)
    return res, {}, " ".join(filter(None, [told, "수치로 답하지 못해 통계표를 찾았습니다."]))


async def _turn(
    state: dict | None, question: str, selections: dict | None = None, user_id: int | None = None
) -> tuple[dict, dict, str | None]:
    progress.now(progress.UNDERSTAND)
    if refusal.instruction_request(question):
        flow.add("통계 질문 검사", "not_statistics", "내부 지시 요청")
        return (
            {
                "status": "not_statistics",
                "answer": refusal.INSTRUCTION_REFUSAL,
                "parsed": {},
                "result": {"status": "not_statistics"},
                "sources": [],
                "chart": None,
                "request_type": UNCLEAR,
                "trace": {"instruction_request": True},
            },
            {},
            None,
        )
    pending = (state or {}).get("pending")
    if pending and pending.get("missing") == "question_clarification":
        original = pending["original_question"]
        kind = pending["clarification"]["kind"]
        choice = question
        if selections and selections.get("clarification_kind") == kind:
            choice = selections.get("clarification_value") or question
        if kind == "local_region":
            regions = await asyncio.to_thread(indicators.find_regions, choice)
            has_indicator = bool(indicators.find_in_text(choice))
            region = question_clarify.region_reply(choice, regions, has_indicator)
            if region:
                region = (
                    await asyncio.to_thread(question_clarify.place_order, regions, _one_place)
                    or region
                )
            name = None if region or has_indicator else question_clarify.bare_region(choice)
            if name:
                found = await asyncio.to_thread(indicators.resolve_region, name)
                if found["status"] == "ok":
                    region = found["display"]
                elif found["status"] == "ambiguous_region" and not _gave_up_asking(pending):
                    again = {
                        "kind": "local_region",
                        "prompt": josa(f"'{name}'", "은는")
                        + " 여러 곳에 있습니다. 어느 지역인가요?",
                        "options": list(found["candidates"]),
                    }
                    return _clarification_answer(original, again), {}, None
            if region:
                merged = question_clarify.merge_region(original, region)
                return await _clarified(merged, state)
        elif kind == "table_cell":
            if code := dynamic_indicator.pick_option(choice, pending["clarification"]):
                trace: dict = {}
                context = pending["clarification"].get("context") or {}
                res = await dynamic_indicator.answer_choice(original, context, code, trace)
                if res is not None:
                    if not res.get("clarification"):
                        res["chart"] = charts.build(res.get("result"))
                    res.update(request_type=REPLY, question=original)
                    return res, {}, None
        elif kind == "alternative":
            answers = (pending["clarification"].get("context") or {}).get("answers") or []
            code = dynamic_indicator.pick_option(choice, pending["clarification"])
            if code is None:
                code = alternative_by_words(choice, answers)
            if code is not None:
                if 0 <= int(code) < len(answers):
                    res = copy.deepcopy(answers[int(code)])
                    res["chart"] = charts.build(res.get("result"))
                    res.update(request_type=REPLY, question=original, trace={})
                    return res, {}, None
        elif scope := question_clarify.scope_reply(choice):
            return await _movement(original, scope, user_id)
        if indicators.find_in_text(question) or re.search(
            r"몇|얼마|통계|알려|보여|안녕|취소", question
        ):
            state = None
            pending = None
        elif _gave_up_asking(pending):
            return await _after_asking(original, kind, user_id)
        else:
            return _clarification_answer(original, pending["clarification"]), {}, None

    if scope := question_clarify.stated_scope(question):
        return await _movement(question, scope, user_id)
    if (
        question_clarify.VAGUE_REGION.search(question)
        or question_clarify.MOVE_COUNT.search(question)
        or question_clarify.too_many_regions(question)
    ):
        regions = await asyncio.to_thread(indicators.find_regions, question)
        if clarification := question_clarify.detect(question, regions):
            if refused := await table_guide.off_domain_early(question):
                why = "인구 통계에 없는 주제 (되묻기 전에 거절)"
                flow.add("범위 검사", refused.get("status"), why)
                refused["request_type"], refused["question"] = NEW, question
                return refused, {}, None
            return _clarification_answer(question, clarification), {}, None
    if (state or {}).get("search_session") and (
        selections
        or (
            (pending or {}).get("missing") == "table"
            and is_choice_reply(question, pending.get("options") or [])
        )
    ):
        found = await table_guide.find_tables(
            question, state["search_session"], selections, user_id
        )
        if computed := await _after_table_choice(pending, found):
            return computed, {}, None
        res = table_guide.tables_answer(found)
        res["request_type"] = REPLY
        return res, {}, None
    if pending and pending.get("missing") == "year":
        if merged := ask_followup.merge(pending["original_question"], question):
            res, sources, notice = await _new(merged, None)
            res["request_type"], res["question"] = REPLY, merged
            return res, sources, notice
    if pending and pending.get("missing") == "region":
        if picked := pick_region(question, pending.get("options") or []):
            conditions = replace_region(state["conditions"], pending.get("region"), picked)
            res = await _answer(conditions)
            res["request_type"] = REPLY
            return res, {**state["sources"], "regions": "user"}, None

    if reply := chat_reply(question):
        return _chat_answer(state, reply), {}, None
    if early := refusal.early_answer(question):
        flow.add("통계 질문 검사", early["status"], "통계 수치를 묻는 질문이 아님 (질문 이해 전)")
        if early["status"] == "chat":
            return _chat_answer(state, early["answer"]), {}, None
        early["request_type"], early["question"] = NEW, question
        return early, {}, None
    if measures := await asyncio.to_thread(qa.separate_measures, question):
        return await _separate_measures(question, measures, state, user_id)
    usable = bool(state and (state.get("conditions") or {}).get("type") not in NO_CONDITIONS)
    region_only = None
    if usable and (state.get("last") or {}).get("status") == TABLES:
        region_only = await asyncio.to_thread(region_only_changes, question, state["conditions"])
        usable = region_only is not None
    if not usable and summary_request(question):
        if ((state or {}).get("last") or {}).get("answer"):
            last = state["last"]
            return _reuse(state, summary_text(last, question), None, SUMMARY), {}, None
        return _nothing_to_summarize(), {}, None
    if (
        not usable
        and (pending or {}).get("missing") == "indicator"
        and (merged := await asyncio.to_thread(with_asked_place, question, pending["place"]))
    ):
        flow.add("지역 되묻기 답", "지역을 붙임", merged)
        res, sources, notice = await _new(merged, None)
        res["request_type"], res["question"] = REPLY, merged
        return res, sources, notice
    if not usable and (only := await asyncio.to_thread(only_regions, question)):
        flow.add("지역만 말함", "되묻기", "이어 갈 조건이 없어 볼 통계를 묻는다")
        return _ask_indicator(*only), {}, None
    if not usable:
        return await _new(question, None)

    has_talk_request = not _talk_split_inner.get() and talk_summary_words(question)
    if (
        HOUSEHOLD_AGE_RATIO.fullmatch(question)
        or qa.housing_population_condition(question)
        or qa.birth_sex_ratio_reference(question)
        or qa.BIRTH_SEX_RATIO_WORDS.search(question)
        or (not has_talk_request and await asyncio.to_thread(complete_question, question))
    ):
        return await _new(question, state)

    if (state.get("last") or {}).get("status") == "ok" and (
        aged := age_only_change(question, state["conditions"])
    ):
        res = await _answer(aged)
        res["request_type"] = CHANGE
        return res, state["sources"], None

    if switched := await asyncio.to_thread(survey_switch, question, state["conditions"]):
        res = await _answer(switched)
        res["request_type"] = CHANGE
        return res, state["sources"], None

    has_last = bool((state.get("last") or {}).get("answer"))
    view = chart_request(question) if has_last else None
    delta = None
    if has_last and view is None and has_talk_request:
        # 대화 정리 말이 있으면 LLM 이 요청 목록으로 나눈다 (11/27): 정리만이면 정리, 정리 + 새
        # 질문이면 둘 다. 예전에는 정규식이 문장 전체를 요약으로 보거나(뒤의 '혼인도 구해줘'를 버림)
        # 지표 · 지역이 있으면 요약이 아니라고 봐 요약을 버렸다. LLM 이 실패하면 예전 규칙대로
        split_trace: dict = {}
        reqs = await split_talk(question, split_trace)
        flow.add(
            "후속 질문",
            "정리 말 나누기",
            str((split_trace.get("talk_split") or {}).get("requests")),
        )
        asks = [r["text"] for r in reqs or [] if r["kind"] == "question"]
        wants = reqs is not None and any(r["kind"] == "summary" for r in reqs)
        whole = await asyncio.to_thread(whole_talk_summary_request, question)
        if reqs is None or not asks:
            # 정리만인지는 예전 규칙을 따른다: LLM 이 '서울 인구 전체 정리해줘'(지표 · 지역이 있는
            # 말)도 대화 정리로 읽었다 (11/27 확인). LLM 은 정리 + 질문을 나누는 데만 쓴다
            wants, asks = whole, []
        if wants and not asks:
            res, sources, notice = await _talk_summary(state, question, user_id)
            res.setdefault("trace", {}).update(split_trace)
            return res, sources, notice
        if wants and asks:
            return await _talk_and_ask(state, question, reqs, selections, user_id, split_trace)
    if has_last and view is None and await asyncio.to_thread(detail_request, question):
        last = state["last"]
        return _reuse(state, last["answer"], last.get("chart"), DETAIL), state["sources"], None
    if has_last and view is None and await asyncio.to_thread(source_request, question):
        if text := source_text(state["last"]):
            return _reuse(state, text, None, SOURCE), state["sources"], None
    if view is None and await asyncio.to_thread(table_question, question):
        return await _find_tables(question, state, user_id)
    asked_why = has_last and view is None and await asyncio.to_thread(why_request, question)
    if asked_why and state["last"].get("summary"):
        text = f"{NO_CAUSE}\n{answer_summary.text(state['last']['summary'])}"
        return _reuse(state, text, state["last"].get("chart"), SUMMARY), state["sources"], None
    if (
        view is None
        and get_settings().follow_restate
        and not (has_last and summary_request(question))
    ):
        # 이어 묻기 LLM 복원 (A_16): 아래 코드 규칙(비율 후속 · 구별로 · 지역만 · 구성비 · 사람 수)
        # 과 변경 추출(FOLLOW_SYSTEM) 대신 LLM이 완전한 질문으로 복원하고 새 질문 경로로 간다.
        # 복원을 쓸 수 없으면(None) 아래 기존 경로로 이어 간다.
        # 3-a(FOLLOW_SURE_RULES): 오판이 없던 규칙이 잡는 말은 복원 없이 아래 규칙으로 간다
        rule = await _sure_rule(state, question) if get_settings().follow_sure_rules else None
        if rule:
            flow.add("이어 묻기 판단", "규칙", f"{rule} 규칙이 잡음 → LLM 복원 건너뜀")
        elif (restated := await _restated(state, question, user_id)) is not None:
            return restated
    if view is None and (asked := await asyncio.to_thread(ratio_followup, question, state)):
        flow.add("비율 후속 질문", "새 질문으로", asked)
        res, sources, notice = await _new(
            asked, state, scoped_age_ratio=bool(state["conditions"].get("ages"))
        )
        res["request_type"], res["question"] = CHANGE, asked
        return res, sources, notice
    if view is None and not (has_last and summary_request(question)):
        trace: dict = {}
        reverse_birth_ratio = qa.BIRTH_RATIO_OPPOSITE.get(state["conditions"].get("indicator"))
        if reverse_birth_ratio and BIRTH_RATIO_REVERSE_REPLY.fullmatch(question):
            delta = {
                "request_type": CHANGE,
                "changes": {"indicator": reverse_birth_ratio, "sex": "계"},
                "chart": None,
                "unclear": None,
            }
        elif reverse_birth_ratio and (reply := BIRTH_RATIO_COUNT_REPLY.fullmatch(question)):
            delta = {
                "request_type": CHANGE,
                "changes": {"birth_ratio_base": qa.birth_ratio_count(reply["count"])},
                "chart": None,
                "unclear": None,
            }
        elif year_reply := YEAR_ONLY_REPLY.fullmatch(question):
            delta = {
                "request_type": CHANGE,
                "changes": {"year": int(year_reply[1])},
                "chart": None,
                "unclear": None,
            }
            flow.add("후속 질문", "연도 변경", f"{year_reply[1]}년")
        elif period_length_reply(question) or recent_trend_reply(question, state["conditions"]):
            # 기간 길이는 아래 recent_years가 읽어 '자료 마지막 해까지 N년'으로 바꾼다
            delta = {"request_type": CHANGE, "changes": {}, "chart": None, "unclear": None}
            flow.add("후속 질문", "기간 길이 변경", f"{recent_years(question)}년")
        elif order := order_changes(question, state["conditions"]):
            delta = {"request_type": CHANGE, "changes": order, "chart": None, "unclear": None}
            flow.add("후속 질문", "순위 순서 변경", "낮은 순" if order["ascending"] else "높은 순")
        elif scope := await asyncio.to_thread(rank_scope_changes, question, state["conditions"]):
            delta = {"request_type": CHANGE, "changes": scope, "chart": None, "unclear": None}
            flow.add("후속 질문", "목록 범위", scope.get("within") or scope.get("sido"))
        elif ages := await asyncio.to_thread(age_only_changes, question, state["conditions"]):
            delta = {"request_type": CHANGE, "changes": ages, "chart": None, "unclear": None}
            label = ", ".join(indicators.age_label(band) for band in ages["ages"])
            flow.add("후속 질문", "연령 변경", label)
        elif composition_request(question):
            delta = parse_delta('{"request_type": "change", "changes": {"type": "composition"}}')
        elif split := await asyncio.to_thread(region_split_changes, question, state["conditions"]):
            delta = {"request_type": CHANGE, "changes": split, "chart": None, "unclear": None}
        elif region_only := region_only or await asyncio.to_thread(
            region_only_changes, question, state["conditions"]
        ):
            delta = {"request_type": CHANGE, "changes": region_only, "chart": None, "unclear": None}
        else:
            # 차트 말과 조건이 섞인 말은 조건 부분만 변경 추출에 준다. 섞인 채로 주면 HCX가
            # '그림 바꾸기'로 읽어 조건을 버리거나('남성만 막대로') 새 질문으로 읽었다 (11/26 점검).
            # 그림 모양은 아래에서 다시 계산한 답에 코드가 붙인다
            asked = condition_part(question) if chart_shape(question) else question
            delta = await extract(state, asked, trace)
            if delta["request_type"] == REPORT and has_last:
                flow.add("후속 질문", REPORT, "지금까지의 답을 묶어 달라는 말")
                return await _report_turn(state, question, user_id)
            if (
                delta["request_type"] == CHANGE
                and "indicator" not in delta["changes"]
                and await asyncio.to_thread(asks_other_people, question, state["conditions"])
            ):
                delta["request_type"] = NEW
        recent = None
        if delta["request_type"] != NEW and not names_indicator(question, {}):
            anchor = next((r for r in timeref.find(question) if r["kind"] == "span_until"), None)
            recent = recent_years(question) if not anchor else None
            year = follow_year(question, state["conditions"]) if not (anchor or recent) else None
            if anchor or recent or year:
                changes = {
                    k: v
                    for k, v in (delta.get("changes") or {}).items()
                    if k not in ("year", "start_year", "end_year")
                }
                if anchor:
                    period = timeref.period(anchor, date.today())
                    changes.update(start_year=period["start"], end_year=period["end"])
                    if changes.get("type") in ("value", "compare"):
                        changes["type"] = "trend"
                elif year:
                    changes["year"] = year
                delta.update(request_type=CHANGE, unclear=None, changes=changes)
        if delta["unclear"]:
            return _unclear(state, delta["unclear"]), state["sources"], None
        if delta["request_type"] == SUMMARY and delta["changes"]:
            delta["request_type"] = CHANGE
        if delta["request_type"] == CHANGE:
            conditions, changed = apply_changes(state["conditions"], delta["changes"])
            if recent:
                conditions, changed = with_recent(conditions, changed, recent)
            if changed:
                res = None
                if "regions" in changed and (state.get("last") or {}).get("status") == TABLES:
                    res = await _household_age_ratio(inherited_question(conditions))
                if res is None:
                    res = await _answer(conditions, trace)
                res["request_type"] = CHANGE
                status = res.get("status")
                retry_region = (
                    status in REGION_RETRY_STATUSES
                    and "regions" in changed
                    and conditions.get("dynamic_spec")
                )
                retry_period = "period" in changed and status == "out_of_range"
                if status in table_guide.SEARCH_INSTEAD or retry_region or retry_period:
                    res["search_question"] = inherited_question(conditions)
                sources = {**state["sources"], **{k: "user" for k in changed if k in LABELS}}
                return res, sources, with_asked_shape(res, question, delta.get("chart"))
            delta["request_type"] = NEW
        if delta["request_type"] == CHART:
            view = delta["chart"]

    if has_last and (view or (delta and delta["request_type"] == CHART)):
        last = state["last"]
        # 변경 추출이 모양을 비워 두면('그림 바꿔줘') 직전 차트의 기본 모양
        chart, text = redraw(last.get("chart"), view or "chart", last.get("status"))
        return _reuse(state, text, chart, CHART), state["sources"], None
    if has_last and (delta is None or delta["request_type"] == SUMMARY):
        last = state["last"]
        return (
            _reuse(state, summary_text(last, question), last.get("chart"), SUMMARY),
            state["sources"],
            None,
        )

    if asked := other_subject_question(question, state["conditions"]):
        flow.add("다른 지표 후속 질문", "직전 지역 · 기간을 붙임", asked)
        return await _new(asked, state, said_alone=question)
    return await _new(question, state)


def with_asked_shape(res: dict, question: str, read: str | None) -> str | None:
    """다시 계산한 답의 차트를 질문에서 함께 말한 모양으로 ('남자만 막대그래프로') → 못 그린 이유.
    말한 모양이 '그래프 · 차트'처럼 정해지지 않았으면 변경 추출이 읽은 모양(read)을 쓴다"""
    said = chart_shape(question)
    shape = said if said not in (None, "chart") else read or said
    if not (res.get("chart") and shape):
        return None
    res["chart"], cannot = reshape(res["chart"], shape)
    return f"{cannot}. {chart_name(res['chart'])}로 보여 드립니다." if cannot else None


def other_subject_question(question: str, conditions: dict) -> str | None:
    """'혼인도 알려줘'처럼 사전이 모르는 낱말 하나만 말한 후속 질문이면 직전 지역 · 기간을 앞에 붙인
    질문 ('2025년 서울 혼인도 알려줘'). 무엇을 물었는지(혼인 → 혼인 건수)는 질문 이해가 읽는다.
    낱말만 표 검색어로 넣으면 사전 지표('혼인 건수')를 두고 표 안내 · 칸 되묻기로 끝났다 (11/26).
    낱말이 지역 이름이면('강남은?') 지역을 바꾼 말이라 붙이지 않는다"""
    if not (word := said_other_subject(question)) or names_indicator(question, {}):
        return None
    if indicators.resolve_region(word).get("status") == "ok":
        return None
    where = " ".join(conditions.get("regions") or [])
    if not where and conditions.get("type") == "rank":
        where = conditions.get("sido") or ""
    lead = " ".join(x for x in (period_text(conditions), where) if x)
    return f"{lead} {question.strip()}" if lead else None


REPORT_NO_TURNS = (
    "이 대화에는 아직 보고서로 묶을 수 있는 답이 없습니다."
    " 통계 수치를 물어본 뒤 다시 말씀해 주세요."
)
REPORT_LOGIN = (
    "보고서는 로그인한 사용자만 만들 수 있습니다."
    " 로그인하면 이 내용을 보고서로 저장하고 분석 문장까지 만들어 드립니다."
)


async def _session_turns(session_id: str, user_id: int | None) -> list[dict]:
    """이 대화에서 보고서에 담을 수 있는 답(reportable · figure) → [{id, question, response}].
    순서대로, 최근 reports.MAX_ITEMS 개"""
    from app.core.db import engine

    owner = "user_id = :uid" if user_id is not None else "user_id is null"
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "select id, message as question, response from app.chat_turns"
                f" where session_id = :sid and {owner} order by turn_no, id"
            ),
            {"sid": session_id, "uid": user_id},
        )
        turns = [dict(r) for r in rows.mappings()]
    kept = [
        t for t in turns if (t["response"] or {}).get("reportable") and t["response"].get("figure")
    ]
    return kept[-reports.MAX_ITEMS :]


def report_text(content: dict) -> str:
    """보고서 content(report_writer.write)를 답 글로"""
    lines = [content.get("title") or reports.DEFAULT_TITLE]
    for name, key in (("요약", "summary"), ("분석", "analysis")):
        if said := [s["text"] for s in content.get(key) or [] if s.get("text")]:
            lines += ["", name, *[f"- {t}" for t in said]]
    if limits := [x for x in content.get("limits") or [] if x]:
        lines += ["", "한계", *[f"- {x}" for x in limits]]
    lines += [f"※ {w}" for w in content.get("warnings") or []]
    return "\n".join(lines)


def headlines_text(turns: list[dict]) -> str:
    """로그인하지 않은 대화: 각 답의 요약 첫 문장을 차례로 (코드가 만든 문장이라 숫자가 그대로)"""
    lines = ["지금까지의 답을 정리했습니다."]
    for t in turns:
        response = t["response"] or {}
        head = ((response.get("summary") or {}).get("headline") or "").strip()
        lines.append(f"- {head or lead(response.get('answer') or '')}")
    return "\n".join([*lines, "", REPORT_LOGIN])


async def _talk_and_ask(
    state: dict,
    question: str,
    reqs: list[dict],
    selections: dict | None,
    user_id: int | None,
    split_trace: dict,
) -> tuple[dict, dict, str | None]:
    """정리 + 새 질문: 정리는 지금까지의 답으로, 질문은 그 말만 원래 후속 질문 흐름으로 계산하고
    말한 순서대로 답을 잇는다. 직전 답(state.last)은 질문의 답이 된다 (handle 의 next_state)"""
    summary_res, _, _ = await _talk_summary(state, question, user_id)
    asked = " ".join(r["text"] for r in reqs if r["kind"] == "question")
    flow.add("후속 질문", "정리 + 질문", asked)
    token = _talk_split_inner.set(True)
    try:
        res, sources, notice = await _turn(state, asked, selections, user_id)
    finally:
        _talk_split_inner.reset(token)
    res.setdefault("trace", {}).update(split_trace)
    res["talk_summary"] = {
        "answer": summary_res.get("answer") or "",
        "first": reqs[0]["kind"],
        "request_type": summary_res.get("request_type"),
        "asked": asked,
    }
    if report := summary_res.get("report_ref"):
        res["report_ref"] = report
    return res, sources, notice


async def _report_turn(
    state: dict, question: str, user_id: int | None
) -> tuple[dict, dict, str | None]:
    """'지금까지 물어본 거 정리해줘': 이 대화의 답을 모아 보고서로 (변경 추출 · 이어 묻기 복원의
    report 선택지). 로그인 사용자는 보고서를 만들어 저장하고 문장을 만든다(reports.create ·
    generate, 화면 [보고서] 버튼과 같은 함수). 로그인하지 않았으면 저장 없이 요약 문장을 모은다"""
    progress.now(progress.REPORT)
    sid = _session_now.get()
    turns = await _session_turns(sid, user_id) if sid else []
    res = {
        "status": "ok",
        "answer": REPORT_NO_TURNS,
        "parsed": state["conditions"],
        "result": {"status": "ok", "reused": True, "report": None},
        "sources": [],
        "chart": None,
        "summary": None,
        "request_type": REPORT,
        "trace": {},
    }
    if not turns:
        flow.add("대화 보고서", "담을 답 없음", "")
        return res, state["sources"], None
    if user_id is None:
        res["answer"] = headlines_text(turns)
        flow.add("대화 보고서", "요약 모음", f"답 {len(turns)}개 (로그인 안 함)")
        return res, state["sources"], None
    try:
        made = await reports.create(user_id, turn_ids=[t["id"] for t in turns], session_id=sid)
        done = await asyncio.wait_for(
            reports.generate(user_id, made["id"]), timeout=get_settings().report_timeout
        )
    except reports.ReportError as e:
        res["answer"] = e.message
        flow.add("대화 보고서", "실패", e.message)
        return res, state["sources"], None
    except TimeoutError:
        res["answer"] = headlines_text(turns).replace(
            REPORT_LOGIN, "보고서 문장을 만드는 데 시간이 오래 걸려 각 답의 요약만 보여 드립니다."
        )
        res["report_ref"] = res["result"]["report"] = {"id": made["id"], "status": "draft"}
        flow.add("대화 보고서", "시간 초과", f"보고서 {made['id']} 초안만 저장")
        return res, state["sources"], None
    res["answer"] = report_text(done.get("content") or {})
    res["report_ref"] = res["result"]["report"] = {"id": done["id"], "status": done["status"]}
    flow.add("대화 보고서", done.get("status"), f"보고서 {done['id']} · 답 {len(turns)}개")
    return res, state["sources"], None


def next_state(state: dict | None, question: str, res: dict, sources: dict) -> dict:
    prev = state or {}
    turn_no = (prev.get("turn_no") or 0) + 1
    if (res.get("trace") or {}).get("instruction_request"):
        return {"conditions": {}, "sources": {}, **prev, "turn_no": turn_no}
    recent = [*(prev.get("recent") or []), question][-RECENT:]
    if res.get("clarification"):
        before = prev.get("pending") or {}
        again = (
            before.get("missing") == "question_clarification"
            and before.get("original_question") == res["question"]
        )
        return {
            **prev,
            "clarification_context": prev.get("clarification_context")
            or {
                "conditions": prev.get("conditions") or {},
                "sources": prev.get("sources") or {},
            },
            "conditions": {},
            "sources": {},
            "pending": {
                "missing": "question_clarification",
                "original_question": res["question"],
                "clarification": res["clarification"],
                "count": (before.get("count") or 1) + 1 if again else 1,
            },
            "recent": recent,
            "turn_no": turn_no,
        }
    if res.get("request_type") in (CHART, SUMMARY, DETAIL, SOURCE):
        last = {**prev["last"], "chart": res.get("chart") or prev["last"].get("chart")}
        return {**prev, "last": last, "recent": recent, "turn_no": turn_no}
    if res.get(ASK_INDICATOR):
        pending = {"missing": "indicator", "place": res[ASK_INDICATOR]}
        return {**prev, "pending": pending, "recent": recent, "turn_no": turn_no}
    if res.get("request_type") in (UNCLEAR, CHAT):
        return {**prev, "recent": recent, "turn_no": turn_no}
    if res.get("status") in (TABLES, *table_guide.REFUSED):
        parsed = res.get("parsed") or {}
        numeric = (
            res.get("status") == TABLES
            and res.get("request_type") in (NEW, CHANGE, REPLY)
            and parsed.get("type") not in NO_CONDITIONS
            and (parsed.get("term") or parsed.get("indicator"))
        )
        found = res.get("tables")
        last = {
            "status": res.get("status"),
            "answer": res.get("answer") or "",
            "sources": [],
            "chart": None,
        }
        return {
            "conditions": effective(parsed, {}) if numeric else {},
            "sources": sources if numeric else {},
            "last": last,
            "history": [*(prev.get("history") or []), answer_line(last)][-HISTORY:],
            "pending": {
                "missing": "table",
                "options": table_choice_options(found),
                "original_question": res.get("question"),
                "parsed": res.get("parsed") or {},
            }
            if table_guide.waiting_choice(found)
            else None,
            "search_session": (found or {}).get("session_id")
            or res.get("search_session")
            or prev.get("search_session"),
            "recent": recent,
            "turn_no": turn_no,
        }
    asked = res.get("question") or question
    conditions = effective(res.get("parsed") or {}, res.get("result") or {})
    if unconfirmed := conditions.pop("ungrounded_region", None):
        conditions["regions"] = [r for r in conditions.get("regions") or [] if r != unconfirmed]
        if conditions.get("sido") == unconfirmed:
            conditions.pop("sido")
    last = {
        "status": res.get("status"),
        "answer": res.get("answer") or "",
        "sources": res.get("sources") or [],
        "chart": res.get("chart"),
        "summary": res.get("summary"),
    }
    return {
        "conditions": conditions,
        "sources": sources,
        "last": last,
        "history": [*(prev.get("history") or []), answer_line(last)][-HISTORY:],
        "pending": pending_of(res.get("result") or {}, asked),
        "search_session": prev.get("search_session"),
        "recent": recent,
        "turn_no": turn_no,
    }


async def _attach_tables(res: dict, state: dict | None, question: str, user_id: int | None) -> None:
    if res.get("tables") is not None or res.get("request_type") in (
        CHART,
        SUMMARY,
        REPORT,
        DETAIL,
        SOURCE,
        UNCLEAR,
        CHAT,
    ):
        return
    asked = res.get("question") or question
    searched = res.get("search_question") or asked
    if res.get("status") == qa.CROSS_DYNAMIC:
        if await _cross_dynamic(res, state, asked, user_id):
            return
        res["status"] = "unknown_indicator"
    if res.get("status") in table_guide.SEARCH_INSTEAD:
        sid = (state or {}).get("search_session") or table_guide.new_search_session()
        kind, trace, parsed = res.get("request_type"), res.get("trace") or {}, res.get("parsed")
        computed = await dynamic_indicator.cached_answer(searched, parsed or {}, trace)
        if computed is not None:
            res.clear()
            res.update(computed)
            if not res.get("clarification"):
                res["chart"] = charts.build(res.get("result"))
            res["request_type"], res["question"], res["search_session"] = kind, asked, sid
            await _attach_cards(res, asked)
            return
        try:
            found = _keep(await table_guide.find_tables(searched, sid, None, user_id))
        except Exception:
            logger.exception("통계표 찾기 실패")
            return
        try:
            computed = await dynamic_indicator.compute_from_handoff(
                searched, parsed or {}, found, trace
            )
        except Exception:
            logger.exception("동적 지표 계산 실패")
            computed = None
        res.clear()
        if computed is not None:
            res.update(computed)
            res["chart"] = charts.build(res.get("result"))
            res["request_type"], res["question"], res["search_session"] = kind, asked, sid
            await _attach_cards(res, asked)
            return
        try:
            pair = (
                None
                if (parsed or {}).get("scoped_age_ratio")
                else cross_indicator.detect_partial(asked, both=True)
            )
        except Exception:
            logger.exception("두 값 나누기 감지 실패")
            pair = None
        if pair is not None:
            if pair.dynamic == "both":
                two = {"parsed": {**(parsed or {}), "cross": pair.as_dict()}, "trace": trace}
                two["request_type"] = kind
                if await _cross_dynamic(two, state, asked, user_id) and two.get("status") == "ok":
                    res.update(two)
                    return
        res.update(table_guide.tables_answer(found, {"parsed": parsed or {}}))
        res["request_type"], res["question"], res["trace"] = kind, asked, trace or {}
        res["search_session"] = sid
        return
    if ungrounded(res) and await _dynamic_instead(res, state, asked, user_id):
        return
    if searchable_after(res) and await _search_after(res, state, asked, user_id, searched):
        return
    await _attach_cards(res, asked)


def searchable_after(res: dict) -> bool:
    result = res.get("result") or {}
    kind, status = res.get("request_type"), res.get("status")
    if result.get("final"):
        return False
    if status == "out_of_range" and (res.get("parsed") or {}).get("year") == date.today().year:
        return False
    if (
        kind == CHANGE
        and res.get("search_question")
        and (
            status == "out_of_range"
            or (status in REGION_RETRY_STATUSES and (res.get("parsed") or {}).get("dynamic_spec"))
        )
    ):
        return True
    return (
        status in table_guide.SEARCH_AFTER
        and kind in (NEW, REPLY)
        and not (result.get("suggestion") or {}).get("status") == "ok"
    )


def _year_found_elsewhere(res: dict, found: dict) -> str | None:
    result = res.get("result") or {}
    if res.get("status") != "out_of_range" or found.get("status") not in ("ok", "partial_match"):
        return None
    targets = (found.get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None
    picked = next(
        (
            c
            for c in targets[0].get("candidates") or []
            if (c.get("review") or {}).get("match") in ("exact", "partial")
        ),
        None,
    )
    if not picked or (picked.get("checks") or {}).get("year") is not True:
        return None
    name = (res.get("parsed") or {}).get("indicator") or result.get("indicator")
    span = result.get("available_years") or []
    asked = result.get("asked_year")
    when = f"{asked}년 자료는" if isinstance(asked, int) else "질문한 기간의 자료는"
    if not name or len(span) != 2:
        return f"{when} 아래 통계표에 있습니다."
    have = f"기본 통계의 {name} 자료는 {span[0]}~{span[1]}년만 있지만"
    return f"{have}, {when} 아래 통계표에 있습니다."


async def _search_after(
    res: dict, state: dict | None, asked: str, user_id: int | None, searched: str | None = None
) -> bool:
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    trace = res.setdefault("trace", {})
    parsed = res.get("parsed") or {}
    try:
        found = _keep(await table_guide.find_tables(searched or asked, sid, None, user_id))
        computed = await dynamic_indicator.compute_from_handoff(
            searched or asked, parsed, found, trace
        )
    except Exception:
        logger.exception("사전에 자료가 없는 지표의 통계표 찾기 · 계산 실패")
        return False
    trace["search_after"] = {
        "status": res.get("status"),
        "found": found.get("status"),
        "computed": (computed or {}).get("status"),
    }
    if computed is None or (computed.get("status") != "ok" and not computed.get("clarification")):
        recs = found.get("recommendations") or []
        if (head := _year_found_elsewhere(res, found)) is not None:
            trace["replaced"] = {
                "indicator": parsed.get("indicator"),
                "answer": lead(res.get("answer") or ""),
                "by": TABLES,
                "reason": res.get("status"),
            }
            kind = res.get("request_type")
            res.clear()
            res.update(table_guide.tables_answer(found, {"parsed": parsed}))
            res["answer"] = f"{head}\n{res['answer']}"
            res["trace"] = trace
            res["request_type"], res["question"], res["search_session"] = kind, asked, sid
            return True
        if found.get("status") in ("ok", "partial_match") and recs:
            res["tables"] = {"status": "ok", "recommendations": recs}
            res["search_session"] = sid
            return True
        return False
    trace["replaced"] = {
        "indicator": parsed.get("indicator"),
        "answer": lead(res.get("answer") or ""),
        "by": computed.get("status"),
        "reason": res.get("status"),
    }
    kind = res.get("request_type")
    res.clear()
    res.update(computed)
    if not res.get("clarification"):
        res["chart"] = charts.build(res.get("result"))
        await _attach_cards(res, asked)
    res.setdefault("trace", trace)
    res["request_type"], res["question"], res["search_session"] = kind, asked, sid
    return True


CROSS_KINDS = ("value", "trend", "compare", "rank")


def _side_question(parsed: dict, phrase: str, kind: str) -> str:
    regions = parsed.get("regions") or []
    year, y0, y1 = parsed.get("year"), parsed.get("start_year"), parsed.get("end_year")
    if kind == "trend":
        when = f"{y0}년부터 {y1}년까지" if y0 and y1 else (f"{y0}년부터" if y0 else "최근")
        return " ".join(filter(None, [when, *regions[:1], phrase, "추이"]))
    when = f"{year}년" if year else ""
    if kind == "rank":
        return " ".join(filter(None, [when, f"{parsed.get('level') or '시도'}별", phrase]))
    if kind == "compare":
        return " ".join(filter(None, [when, *regions, phrase, "비교"]))
    return " ".join(filter(None, [when, *regions[:1], phrase]))


async def _dynamic_side(
    phrase: str, parsed: dict, kind: str, sid: str, user_id: int | None, trace: dict
) -> dict | None:
    sub_q = _side_question(parsed, phrase, kind)
    sub_parsed = {
        **multi_indicator.without_unsaid_age(parsed, phrase),
        "type": kind,
        "cross": None,
        "indicators": None,
        "indicator": phrase,
        "term": phrase,
        "grounded": False,
        "dynamic_spec": None,
        "top": None if kind == "rank" else parsed.get("top"),
    }
    step = {"phrase": phrase, "question": sub_q}
    trace.setdefault("cross_sides", []).append(step)
    flow.add("사전에 없는 쪽 계산", "시작", f"'{phrase}'를 따로 물은 질문으로 ({sub_q})")
    try:
        part = await dynamic_indicator.cached_answer(sub_q, sub_parsed, trace)
        if part is None:
            found = await table_guide.find_tables(sub_q, sid, None, user_id)
            part = await dynamic_indicator.compute_from_handoff(sub_q, sub_parsed, found, trace)
    except Exception:
        logger.exception("두 값 나누기: 사전에 없는 쪽 계산 실패 (%s)", phrase)
        return None
    got = (part or {}).get("result") or {}
    step["status"] = (part or {}).get("status")
    if step["status"] != "ok" or (part or {}).get("clarification"):
        return None
    if (got.get("unit") or "") not in cross_indicator.COUNT_UNITS:
        step["skip"] = f"건수 · 인원이 아닌 값 ({got.get('unit')})"
        return None
    return {**got, "indicator": phrase}


SAME_TABLE_FILLER = {"사람", "수", "명", "출신", "국적", "국적자", "온"}
SAME_TABLE_OPTIONS_MAX = 50
SAME_TABLE_STATUSES = ("ok", "uncovered", "no_cell", "not_count")


def _leftover_words(phrase: str) -> list[str]:
    if dynamic_indicator.MOVE_CONTEXT.search(phrase):
        phrase = dynamic_indicator.SCHOOL_GOING.sub("통학", phrase)
    words = [w for w in phrase.split() if len(w) >= 2 and w not in SAME_TABLE_FILLER]
    return [dynamic_indicator.table_words(w) for w in words]


def _cell_options(tbl_id: str) -> dict[str, list[str]]:
    dims, region_dim, _ = dynamic_indicator.schema(tbl_id)
    return {
        d.name: list(d.codes.values())[:SAME_TABLE_OPTIONS_MAX] for d in dims if d.no != region_dim
    }


async def _same_table_side(
    phrase: str, known: str, parsed: dict, kind: str, trace: dict, table: str | None = None
) -> dict | None:
    if table:
        tables = [table]
    else:
        spec = indicators.spec(known)
        if not spec or spec.get("method") != "lookup":
            return None
        tables = [src["table"] for src in spec.get("sources") or []]
    sub_q = _side_question(parsed, phrase, kind)
    sub_parsed = {
        **multi_indicator.without_unsaid_age(parsed, phrase),
        "type": kind,
        "indicator": phrase,
        "term": phrase,
        "dynamic_spec": None,
    }
    if kind == "rank":
        sub_parsed["top"] = None
    items = _leftover_words(phrase)
    for tbl_id in tables:
        step: dict = {"dynamic": []}
        report: dict = {}
        slots = {"conditions": {"items": items}, "report": report}
        record = {"phrase": phrase, "same_table": tbl_id}
        try:
            part, _ = await dynamic_indicator._try(tbl_id, None, sub_q, sub_parsed, step, {}, slots)
        except Exception:
            logger.exception("두 값 나누기: 같은 표에서 찾기 실패 (%s)", tbl_id)
            continue
        got = (part or {}).get("result") or {}
        tried = step["dynamic"][-1] if step["dynamic"] else {}
        why = tried.get("skip")
        unit = got.get("unit") or None
        computed = (part or {}).get("status") == "ok" and not why
        if computed and (unit or "") in cross_indicator.COUNT_UNITS:
            trace.setdefault("cross_sides", []).append(
                {**record, "status": "ok", "leftover": [], "cells": report.get("cells") or [],
                 "why": None, "unit": unit, "options": {}}
            )  # fmt: skip
            cells = report.get("cells")
            flow.add("같은 표에서 찾기", "ok", f"'{phrase}' 표={tbl_id} 칸={cells}")
            return {**got, "indicator": phrase}
        leftover = report.get("leftover") or []
        if computed:
            status, why = "not_count", f"건수 · 인원이 아닌 값 ({unit})"
        elif leftover and report.get("cells"):
            status = "uncovered"
        else:
            status = "no_cell"
        try:
            options = await run_in_threadpool(_cell_options, tbl_id)
        except Exception:
            logger.exception("두 값 나누기: 표의 칸 이름 읽기 실패 (%s)", tbl_id)
            options = {}
        trace.setdefault("cross_sides", []).append(
            {**record, "status": status, "leftover": leftover, "cells": report.get("cells") or [],
             "why": why or "질문의 말과 맞는 칸이 없음", "unit": unit, "options": options}
        )  # fmt: skip
        flow.add("같은 표에서 찾기", status, f"'{phrase}' 표={tbl_id} {why or ''}")
    return None


async def _cross_dynamic(res: dict, state: dict | None, asked: str, user_id: int | None) -> bool:
    parsed = res.get("parsed") or {}
    trace = res.setdefault("trace", {})
    c = cross_indicator.Cross(**parsed["cross"])
    kind = parsed.get("type") if parsed.get("type") in CROSS_KINDS else "value"
    if kind == "compare" and len(parsed.get("regions") or []) <= 1:
        kind = "value"
    q = {**parsed, "type": kind, "cross": None, "indicators": None}
    if kind in ("value", "trend") and not q.get("regions"):
        q["regions"] = ["전국"]
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    sides = {"num": c.num, "den": c.den}
    unknown = ["num", "den"] if c.dynamic == "both" else [c.dynamic]
    got: dict = {}
    for side in unknown:
        other = sides["den" if side == "num" else "num"]
        got[side] = await _same_table_side(sides[side], other, q, kind, trace)
        if got[side] is None:
            got[side] = await _dynamic_side(sides[side], q, kind, sid, user_id, trace)
        if got[side] is None:
            return False
        if kind == "value" and not q.get("year") and got[side].get("year"):
            q["year"] = got[side]["year"]
    for side in ("num", "den"):
        if side in got:
            continue
        known = {**q, "indicator": sides[side], "term": sides[side]}
        if kind == "rank":
            known["top"] = None
        try:
            got[side] = await run_in_threadpool(qa.compute, known)
        except Exception:
            logger.exception("두 값 나누기: 사전 쪽 계산 실패")
            return False
        if got[side].get("status") != "ok":
            return False
    c.units = {sides[s]: got[s].get("unit") for s in unknown}
    result = cross_indicator.combine_parts(c, [got["num"]], got["den"], q)
    result["cross"] = c.as_dict()
    for r in qa._leaves(result):
        if r.get("series"):
            qa._add_yearly_change(r)
            qa._add_trend_facts(r)
        r["asked"] = {**(q.get("asked") or {}), "top": q.get("top")}
    request_type = res.get("request_type")
    res.clear()
    res.update(
        {
            "status": result.get("status"),
            "answer": f"{qa.summary(result)}\n\n{qa.render(result)}".strip(),
            "parsed": {**parsed, "cross": c.as_dict()},
            "result": result,
            "sources": qa.sources(result),
            "trace": trace,
            "chart": charts.build(result) if result.get("status") == "ok" else None,
        }
    )
    res["request_type"], res["question"], res["search_session"] = request_type, asked, sid
    if result.get("status") == "ok":
        await _attach_cards(res, asked)
    return True


async def _attach_cards(res: dict, asked: str) -> None:
    try:
        cards = await table_guide.cards_for(asked, res.get("sources") or [])
    except Exception:
        logger.exception("통계표 카드 만들기 실패")
        cards = []
    if cards:
        res["tables"] = {"status": "ok", "recommendations": cards}


DICTIONARY_ANSWERED = (
    "ok",
    "out_of_range",
    "missing_data",
    "no_region_level",
    "region_not_in_year",
    "missing_region",
)


# 이 턴의 통계표 찾기 결과 (답 판정이 다시 계산할 때 다시 찾지 않게). handle 이 턴마다 비운다
_found_now: ContextVar[dict | None] = ContextVar("ask_found_now", default=None)
# 지금 대화의 session_id: 대화 보고서(_report_turn)가 이 대화의 답을 모을 때 쓴다
_session_now: ContextVar[str | None] = ContextVar("ask_session_now", default=None)
# 다시 계산이 값을 못 냈을 때 그 이유 (status · 되묻기).
# _retable · _repick_compute · _replan_compute 가 두고 _judged 가 읽는다
_retry_outcome: ContextVar[dict | None] = ContextVar("ask_retry_outcome", default=None)
# 고리 끝 경우 1 (값은 냈는데 판정이 거절, 처음 답 유지)에 붙이는 한 줄 — 11/25 세진 확정 문구
CAUTION = "※ 이 답은 질문의 조건과 다를 수 있습니다."
NO_VALUE_ENTRY = "값 없음"  # 판정 고리에 값 없는 실패로 들어온 답 (record["entry"])


def _keep(found: dict) -> dict:
    _found_now.set(found)
    return found


def judgeable(res: dict) -> bool:
    """판정할 답: 새 질문(또는 되묻기에 답해 완성된 질문)에 값으로 답한 것.
    조건만 바꾼 이어 묻기는 그 말만으로는 질문 전체를 알 수 없어 판정하지 않는다"""
    return (
        res.get("status") == "ok"
        and res.get("request_type") in (NEW, REPLY)
        and not res.get("clarification")
        and (res.get("result") or {}).get("status") == "ok"
    )


def without_tables(found: dict, used: set[str], strict: bool = True) -> dict | None:
    """찾은 후보에서 이미 쓴 표를 뺀 꾸러미 (다른 표로 다시 계산할 때). 남는 후보가 없거나 지표가
    여럿인 질문이면 None. strict: 쓴 표가 후보에 없어도(뺀 것이 없어도) None — 표 경로 답은
    그 답의 표가 후보에 있어야 한다. 사전 답 · 값 없는 실패는 strict=False (사전 표는 후보에
    없을 수 있다)"""
    targets = (found.get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None
    cands = targets[0].get("candidates") or []
    rest = [{**c, "picked": False, "review": None} for c in cands if c.get("tbl_id") not in used]
    if not rest or (strict and len(rest) == len(cands)):
        return None
    if len(rest) == len(cands):
        return found
    out = copy.deepcopy(found)
    out["handoff"]["targets"][0]["candidates"] = rest
    return out


def _valued(res: dict) -> bool:
    return (res.get("result") or {}).get("status") == "ok"


def used_tables(res: dict) -> set[str]:
    """retable 이 뺄 표: 답에 쓴 표 · repick 이 본 표 · 사전 지표의 출처 표 ·
    (값 없는 실패면) 이미 계산해 본 후보 (검토가 다른 표를 골라 건너뛴 표는 빼지 않는다)"""
    parsed = res.get("parsed") or {}
    trace = res.get("trace") or {}
    used = {s.get("tbl_id") for s in res.get("sources") or [] if s.get("tbl_id")}
    if repicked := ((trace.get("answer_judge") or {}).get("repick") or {}).get("tbl_id"):
        used.add(repicked)
    if not parsed.get("dynamic_spec"):
        spec = indicators.spec(parsed.get("indicator") or "")
        used |= {src.get("table") for src in (spec or {}).get("sources") or [] if src.get("table")}
    if not _valued(res):
        used |= {
            st.get("tbl_id")
            for st in trace.get("dynamic") or []
            if st.get("tbl_id") and st.get("skip") != dynamic_indicator.EXACT_SKIP
        }
    return {t for t in used if t}


async def _retable(
    res: dict, state: dict | None, asked: str, user_id: int | None
) -> tuple[dict | None, str]:
    """retable: 쓴 표를 빼고 다른 표로 한 번 다시 계산한 답 → (답, 방법). 못 하면 (None, 방법).
    꾸러미가 있으면 남은 후보로, 없으면 원래 질문 그대로 통계표 찾기 (검색어를 새로 쓰지 않는다:
    11/22 Gemma 가 새로 쓴 검색어는 정답 표 1위를 42 → 29로 줄였다). 옛 retable(다음 후보) ·
    reroute(사전 답 → 통계표 찾기로)를 합쳤다 (설계 2-2 · 2-3)"""
    parsed = res.get("parsed") or {}
    via_search = bool(parsed.get("dynamic_spec"))
    if _valued(res) and not via_search and not indicators.spec(parsed.get("indicator") or ""):
        return None, "다시 계산할 방법 없음"
    sid = res.get("search_session") or (state or {}).get("search_session")
    sid = sid or table_guide.new_search_session()
    found = _found_now.get()
    how = "다음 후보 표" if found is not None else "통계표 찾기로"
    if found is None:
        found = _keep(await table_guide.find_tables(asked, sid, None, user_id))
    rest = without_tables(found, used_tables(res), strict=via_search and _valued(res))
    if rest is None:
        return None, how
    trace: dict = {}
    base = {**parsed, "dynamic_spec": None}
    computed = await dynamic_indicator.compute_from_handoff(asked, base, rest, trace)
    if not computed or computed.get("status") != "ok" or computed.get("clarification"):
        _retry_outcome.set(_outcome_of(computed))
        return None, how
    again = dict(computed)
    again.setdefault("trace", trace)
    again["search_session"] = sid
    return again, how


def repick_target(res: dict, found: dict | None) -> tuple[dict, str] | None:
    """repick 이 다시 볼 표 하나 → (후보 표, 고른 이유). 없으면 None.
    표 경로    값을 낸 표. 아래 순위 표가 값을 냈으면 1위 표 (1위가 칸에서 막혔던 것)
    사전 경로  사전 지표의 출처 표 (칸은 고정하지 않고 칸 잇기 → recell 로 다시 고른다)
    값 없음    1위 후보 표
    설계: docs/agent_redesign_2026-11-26.md 2-5 '검사할 표' · 2-7"""
    parsed = res.get("parsed") or {}
    trace = res.get("trace") or {}
    sources = [s.get("tbl_id") for s in res.get("sources") or [] if s.get("tbl_id")]
    if not _valued(res):
        # 값 없는 실패: 1위 후보 표 (칸에서 막힌 표를 칸 잇기 → recell 로 다시)
        top = agent_action.picked_candidate(found)
        return (dict(top), "값 없음: 1위 후보 표") if top else None
    if parsed.get("dynamic_spec"):
        used = ((trace.get("handoff") or {}).get("used") or {}).get("tbl_id") or next(
            iter(sources), None
        )
        top = agent_action.picked_candidate(found)
        if top and used and top.get("tbl_id") != used:
            return dict(top), "아래 순위 표가 답함 → 1위 표"
        if not used:
            return None
        cands = (((found or {}).get("handoff") or {}).get("targets") or [{}])[0].get("candidates")
        cand = next((c for c in cands or [] if c.get("tbl_id") == used), None)
        return (dict(cand) if cand else {"tbl_id": used}), "값을 낸 표"
    spec = indicators.spec(parsed.get("indicator") or "")
    tables = [src.get("table") for src in (spec or {}).get("sources") or [] if src.get("table")]
    tbl = next((t for t in sources if t in tables), None) or next(iter(tables), None)
    if not tbl:
        return None
    return {"tbl_id": tbl}, "사전 지표의 출처 표"


def wrong_cells(res: dict, tbl_id: str) -> str | None:
    """틀렸다고 판정된 답이 그 표에서 쓴 칸 (계산 기록의 cells). 사전 답이면 None"""
    steps = (res.get("trace") or {}).get("dynamic") or []
    return next(
        (
            st["cells"]
            for st in reversed(steps)
            if st.get("tbl_id") == tbl_id and st.get("cells") and st.get("status") == "ok"
        ),
        None,
    )


# 칸만 바꿔서는 못 만드는 지적 (나누기 · 더하기 · 견주기 · 빼기가 필요): repick 하지 않는다.
# 그 말이 칸 이름에도 있으면(예: '출생성비' 칸) 계산이 아니라 그 칸을 가리킨 것으로 본다
CALC_ASKED = re.compile(
    r"비율|비중|배수|성비|차이|격차|비교|견주|합산|합계를|더해|더하|나누|나눠|빼고|빼야|제외|"
    r"중에서만|증감|변화량|상승폭|감소분"
)
_UNIT_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


def _used_codes(res: dict, tbl_id: str, dims: list) -> set[tuple[int, str]]:
    """틀렸다고 판정된 답이 그 표에서 쓴 칸 (분류 번호, 코드).
    표 경로는 계산 기록의 칸 이름으로, 사전 답은 사전 지표의 출처 항목(item_id)으로"""
    out: set[tuple[int, str]] = set()
    wrong = wrong_cells(res, tbl_id) or ""
    spec = indicators.spec((res.get("parsed") or {}).get("indicator") or "")
    items = {
        str(src.get("item_id"))
        for src in (spec or {}).get("sources") or []
        if src.get("table") == tbl_id and src.get("item_id")
    }
    for d in dims:
        for code, name in d.codes.items():
            if (wrong and name and name in wrong) or code in items:
                out.add((d.no, code))
    return out


def _fix_cells(res: dict, tbl_id: str, missing: str) -> list[str]:
    """판정이 빠졌다고 본 말이 닿는 그 표의 칸 이름 (합계 · 지역 제외). 아래는 근거로 치지 않는다:
      앞 답이 쓴 칸     지적이 틀린 칸을 말한 것 ('남자 조건만 반영됨', '무국적자 수로 답함')
      지표 이름 같은 칸  주제어에 닿은 것 ('조사망률을 사용함')
      칸이 하나뿐인 분류 다시 고를 칸이 없다 ('혼인' 항목 하나짜리 표)
    표 이름은 보지 않는다: 여러 항목을 나열한 표 이름(출생아수 · 출생성비 …)이 맞는 칸까지 가린다.
    A1 판정 '다시' 71건에서 닿은 14건 중 맞는 칸은 1건뿐이었다 (나머지가 위 두 경우 · 계산)"""
    dims, region_dim, _ = dynamic_indicator.schema(tbl_id)
    used = _used_codes(res, tbl_id, dims)
    topic = search_norm((res.get("parsed") or {}).get("indicator") or "")
    by_no = {d.no: d for d in dims}
    out = []
    for no, codes in dynamic_indicator._text_hits(missing, dims, {region_dim}).items():
        for code in codes:
            name = by_no[no].codes[code]
            n = search_norm(_UNIT_TAIL.sub("", name))
            if (no, code) in used or not n or len(by_no[no].codes) == 1:
                continue  # 칸이 하나뿐인 분류는 다시 고를 칸이 없다 (계산 기록에도 안 남는다)
            if topic and (n in topic or topic in n):
                continue
            out.append(name)
    return out


def search_norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def repick_blocked(res: dict, verdict: dict) -> str | None:
    """repick 을 아예 하지 않을 답 → 이유. 여러 지표 · 복합 답은 표 하나로 다시 계산하면 나머지
    지표가 사라진다. 계산이 필요한 지적은 칸만 바꿔서 고칠 수 없다 (칸 이름에 그 말이 있으면 예외는
    repick_cells 가 본다)"""
    parsed = res.get("parsed") or {}
    if (
        (res.get("result") or {}).get("split_by") == "indicator"
        or parsed.get("composite")
        or len(parsed.get("indicators") or []) > 1
    ):
        return "여러 지표 · 복합 답"
    return None


def _check_table(res: dict, entry: dict, found: dict | None, asked: str, hits: list[str]):
    """다시 볼 표 하나를 검사 (table_check). 질문 조건은 꾸러미의 해석을 칸 잇기에 넘긴다"""
    parsed = res.get("parsed") or {}
    u = ((found or {}).get("handoff") or {}).get("understanding") or {}
    q = parsed
    if u:
        with contextlib.suppress(Exception):
            q, _ = dynamic_indicator.to_parsed(u, parsed, question=asked)
    slots = {
        "conditions": u.get("conditions") or {},
        "topic": u.get("topic"),
        "table_words": [entry.get("tbl_nm"), entry.get("target")],
    }
    dims, _, _ = dynamic_indicator.schema(entry["tbl_id"])
    used = _used_codes(res, entry["tbl_id"], dims)
    return table_check.check(entry["tbl_id"], asked, q, slots, used, hits)


async def repick_cells(
    res: dict, verdict: dict, found: dict | None, asked: str | None = None
) -> dict:
    """행동 고르기의 근거: 다시 볼 표를 검사(table_check)해 칸을 다시 고르면 고칠 수 있나
    → {"cells", "fits", "blocked", "tbl_id", "by", "check", "hits"}.
      cells   검사의 differs · cells_exist, 또는 판정 지적이 칸에 닿음(앞 답 칸 · 주제어 빼고),
              또는 아래 순위 표가 답했고 1위가 칸에서 막힘
      fits    값 종류 · 주제가 맞음 ('대상이 다름'일 때 repick 조건). 판정 지적이 그 표의 칸에
              닿으면 주제는 맞다고 본다
      blocked 여러 지표 · 복합 답, 또는 계산이 필요한 판정 지적 → repick 안 함"""
    if why_not := repick_blocked(res, verdict):
        return {"cells": False, "fits": True, "blocked": why_not, "by": why_not}
    picked = repick_target(res, found)
    if picked is None:
        return {"cells": False, "fits": True, "blocked": None, "by": "다시 볼 표 없음"}
    entry, why = picked
    tbl_id = entry["tbl_id"]
    out: dict = {"tbl_id": tbl_id, "why": why}
    judged = str(verdict.get("reason") or "") not in agent_action.NO_VALUE
    missing = str(verdict.get("missing") or "").strip() if judged else ""
    calc = [m.group(0) for m in CALC_ASKED.finditer(missing)]
    hits: list[str] = []
    checked = None
    try:
        if missing:
            hits = await run_in_threadpool(_fix_cells, res, tbl_id, missing)
        checked = await run_in_threadpool(_check_table, res, entry, found, asked or "", hits)
    except Exception:
        logger.info("검사 못 함: 표 구조 없음 %s", tbl_id)
    out["hits"] = hits[:5]
    if checked is not None:
        out["check"] = checked.as_trace()
    if calc and not any(word in search_norm(h) for word in calc for h in hits):
        return {**out, "cells": False, "fits": True, "blocked": f"계산이 필요한 지적 ({calc[0]})"}
    # 주제: 질문의 일상어('애 많이 낳는')가 칸에 글자로 안 닿아도, 판정 지적이 그 표의 칸을
    # 가리키면(합계출산율) 표는 맞다고 본다 (11/27 측정 D-071: 이렇게 repick 이 고쳤다)
    fits = checked is None or (checked.kind_ok and (checked.topic_ok or bool(hits)))
    if checked is not None and checked.cells:
        by = "검사: 앞 답과 다른 칸으로 이어짐" if checked.differs else "검사: 고를 칸이 있음"
        return {**out, "cells": True, "fits": fits, "blocked": None, "by": by}
    if hits:
        return {**out, "cells": True, "fits": fits, "blocked": None, "by": "판정 지적이 칸에 닿음"}
    if why.startswith("아래 순위") and agent_action.failed_at_cell(res.get("trace")):
        return {**out, "cells": True, "fits": fits, "blocked": None, "by": "1위 표가 칸에서 막힘"}
    return {**out, "cells": False, "fits": fits, "blocked": None, "by": "고칠 칸을 못 찾음"}


async def _repick_compute(
    res: dict, verdict: dict, asked: str, llm_ok: bool = True
) -> tuple[dict | None, str]:
    """repick: 표는 맞는데 칸이 틀린 답을 같은 표에서 칸만 다시 골라 계산한 답 → (답, 방법).
    칸 잇기 → (판정 지적을 넘긴) recell 순서. recell 은 replan 의 LLM 한도와 따로 센다
    (llm_ok=False 는 시험 · 끄기용). 칸 잇기가 되묻기만 남기면 그 되묻기를 결과로 남긴다.
    다시 고르지 못하면 (None, 방법) → _retry 가 대체 행동으로 간다.
    기록: res["trace"]["answer_judge"]["repick"] (표 · 고른 이유 · 앞 칸 · 칸 잇기 · recell)
    설계: docs/agent_redesign_2026-11-26.md 2-2 · 2-7"""
    how = "같은 표에서 칸 다시"
    record = res.setdefault("trace", {}).setdefault("answer_judge", {})
    found = _found_now.get()
    picked = repick_target(res, found)
    if picked is None:
        record["repick"] = {"skip": "다시 볼 표 없음"}
        return None, how
    entry, why = picked
    if not entry.get("tbl_nm"):
        with contextlib.suppress(Exception):
            entry["tbl_nm"] = (await run_in_threadpool(lookup._table, entry["tbl_id"]))["tbl_nm"]
    wrong = wrong_cells(res, entry["tbl_id"])
    hint = {"reason": verdict.get("reason"), "missing": verdict.get("missing"), "wrong": wrong}
    log: dict = {"tbl_id": entry["tbl_id"], "why": why, "wrong": wrong}
    record["repick"] = log
    flow.add("다시 계산", "repick", f"{entry['tbl_id']} ({why})")
    parsed = res.get("parsed") or {}
    base = {**parsed, "dynamic_spec": None}
    # 사전 답은 꾸러미가 없을 수 있다: 해석은 비워 두고 질문 · parsed 로만 잇는다
    usable = found if (found or {}).get("handoff") else {"handoff": {"understanding": {}}}
    trace: dict = {}
    computed = await dynamic_indicator.compute_from_handoff(
        asked,
        base,
        usable,
        trace,
        repick={
            "entry": entry,
            "hint": hint,
            "llm": llm_ok,
            "same": lambda result: same_number(res.get("result"), result),
        },
    )
    log.update(trace.get("repick") or {})
    log["llm"] = bool((log.get("recell_log") or {}).get("recell"))
    if not computed or computed.get("status") != "ok" or computed.get("clarification"):
        _retry_outcome.set(_outcome_of(computed))
        return None, how
    again = dict(computed)
    again.setdefault("trace", trace)
    sid = res.get("search_session") or (found or {}).get("search_session")
    again["search_session"] = sid or table_guide.new_search_session()
    return again, how


async def _replan_compute(
    res: dict, verdict: dict, asked: str, user_id: int | None
) -> tuple[dict | None, str]:
    """replan: 처음 계획 + 판정의 지적으로 계산 계획을 다시 세워(Gemma 1회) 그 계획으로 표를 찾고
    계산한 답 → (답, 방법). 새 계획을 못 세웠거나 계산하지 못하면 (None, 방법).
    이유는 res["trace"]["answer_judge"]["replan"] 에 남는다 (agent/replan.py)"""
    parsed = res.get("parsed") or {}
    found = _found_now.get()
    first = replan.first_plan_of(found, parsed)
    tried = list(
        dict.fromkeys(s.get("tbl_id") for s in res.get("sources") or [] if s.get("tbl_id"))
    )
    record = res.setdefault("trace", {}).setdefault("answer_judge", {})
    new_plan = await replan.call(asked, first, verdict, parsed, tried, record)
    how = "계획 다시"
    if new_plan is None:
        return None, how
    if (new_plan.get("combine") or {}).get("op") == replan.NOT_COMPUTABLE:
        # 공표되지 않고 만들 수도 없는 값: 숫자를 만들지 않고 답변 불가 사유로 (설계서 B-1 예외)
        return _refusal_answer(res, replan.REFUSAL_CODE, new_plan.get("changed") or ""), how
    sid = table_guide.new_search_session()  # 새 계획은 새 질문처럼 찾는다 (되묻기 상태를 잇지 않음)
    found2 = await table_guide.find_tables(asked, sid, None, user_id, plan=new_plan)
    trace: dict = {}
    base = replan.apply_to_parsed(new_plan, {**parsed, "dynamic_spec": None}, asked)
    computed = None
    if base.get("rank_axis") and base["rank_axis"] != parsed.get("rank_axis"):
        # 지역이 아닌 분류의 순위(rank axis): 사전 지표의 표에서 (_dynamic_instead와 같은 길).
        # 원래 해석에 축('나라')이 있었어도 재계획이 다른 축('국적')을 적었으면 그 축으로 (11/24)
        computed = await _axis_rank(asked, base, trace)
    targets = (found2.get("handoff") or {}).get("targets") or []
    live = [i for i in new_plan.get("indicators") or [] if i.get("scope") != "out"]
    if computed is None and len(live) == 1 and not targets and live[0].get("dict"):
        # 표 찾기가 후보를 못 냈어도(꾸러미 비어 있음) 계획이 사전 지표를 적었으면 사전으로 계산한다
        targets = [{"span": live[0].get("span"), "dict": live[0]["dict"], "candidates": []}]
    if computed is None and len(live) == 1 and len(targets) == 1:
        # 지표 하나: 사전에 있으면 사전으로(sido · aggregate · months 같은 해석 칸을 qa 가 읽는다),
        # 없으면 표로. compute_from_handoff 의 지표 하나 길은 표로만 가서 사전 지표의 시도 안 순위를
        # 놓쳤다. 지표 하나짜리 꾸러미(single)에는 계획의 dict · span 이 없어 여기서 붙인다
        target = {**targets[0], "span": live[0].get("span"), "dict": live[0].get("dict")}
        computed, sub = await multi_indicator._one(asked, base, found2, target)
        trace["dynamic"] = sub.get("dynamic") or []
        trace["by"] = sub.get("by")
    if computed is None:
        computed = await dynamic_indicator.compute_from_handoff(asked, base, found2, trace)
    if not computed or computed.get("status") != "ok" or computed.get("clarification"):
        _retry_outcome.set(_outcome_of(computed))
        record["replan"]["computed"] = {
            "via": trace.get("by") or ("handoff" if targets else "none"),
            "targets": len(targets),
            "indicator": base.get("indicator"),
            "status": (computed or {}).get("status"),
            "reason": str((computed or {}).get("reason") or (computed or {}).get("answer") or "")[
                :160
            ],
            "clarification": bool((computed or {}).get("clarification")),
            "dynamic": [d.get("skip") or d.get("reason") for d in trace.get("dynamic") or []][-3:],
        }
        return None, how
    again = dict(computed)
    again.setdefault("trace", trace)
    again["search_session"] = sid
    return again, how


def _outcome_of(computed: dict | None) -> dict:
    """다시 계산이 값을 못 낸 결과의 요약: 실패 사유 코드와, 되묻기였으면 그 답 전체"""
    if not computed:
        return {"status": None, "clarification": None}
    return {
        "status": computed.get("status"),
        "clarification": computed if computed.get("clarification") else None,
    }


def _loop_end(res: dict, record: dict, how: str, asked_back: dict | None, asked: str) -> None:
    """고리가 답을 바꾸지 못하고 끝났을 때(give_up · timeout) 처음 답을 어떻게 낼지 (11/25 확정).
    A1 판정 '다시' 71건에서 고리가 못 고친 60건을 셋으로 갈랐다:
      경우 3  다시 계산이 되묻기를 냈다           → 그 되묻기를 전달 (5건, 첫 답 정답 0)
      경우 2  어느 시도도 대신 낼 값을 못 냈다    → 답변 불가 (마지막 실패 사유, 없으면
                                                 partial_match) (20건, 첫 답 정답 0 → 잃는 것 없다)
      경우 1  값은 냈는데 그 값도 판정에 걸렸다   → 처음 답 그대로 + 주의 한 줄 (35건, 첫 답 정답
                                                 11이라 지우면 잃는다. 11/22에 맞는 답 12를 막았음)
    시도가 하나도 없었으면(첫 행동이 give_up) 경우 1처럼 둔다. 예외로 끝난 고리는 부르지 않는다."""
    attempts = record.get("attempts") or []
    real = [a for a in attempts if a.get("how") not in (None, "give_up", "timeout")]
    kind = res.get("request_type")
    if record.get("entry") == NO_VALUE_ENTRY and asked_back is None:
        # 값 없는 실패로 들어왔다: 처음 답(통계표 안내 · 사유)을 그대로 낸다. 주의 문구 · 다른
        # 답변 불가로 바꾸지 않는다
        record["end"] = {"how": how, "case": "처음 답 그대로 (값 없음)"}
        flow.add("고리 끝", "처음 답 그대로", f"{how}: 값 없는 실패를 고치지 못함")
        return
    if asked_back is not None:
        record["end"] = {"how": how, "case": "되묻기"}
        flow.add("고리 끝", "되묻기", f"{how}: 다시 계산이 낸 되묻기를 전달")
        res.clear()
        res.update(asked_back)
        res["request_type"], res["question"] = kind, asked
        res.setdefault("trace", {})["answer_judge"] = record
        return
    if real and not any(a.get("computed") for a in real):
        last = next((a.get("fail_status") for a in reversed(real) if a.get("fail_status")), None)
        code = last if last in refusal.TEMPLATES else "partial_match"
        record["end"] = {"how": how, "case": "답변 불가", "code": code, "last_status": last}
        flow.add(
            "고리 끝", "답변 불가",
            f"{how}: 판정이 틀렸다 했고 대신 낼 값이 없음 → {refusal.NAMES.get(code, code)}",
        )  # fmt: skip
        why = f"답 판정 뒤 다시 계산 {len(real)}회 모두 값 없음 ({how}, 마지막 {last or '-'})"
        again = _refusal_answer(res, code, why)
        res.clear()
        res.update(again)
        res["request_type"], res["question"] = kind, asked
        res["trace"]["answer_judge"] = record
        return
    record["end"] = {"how": how, "case": "처음 답 + 주의"}
    flow.add("고리 끝", "처음 답 + 주의", f"{how}: 다시 한 답이 판정을 통과하지 못함")
    answer = res.get("answer") or ""
    if CAUTION not in answer:
        res["answer"] = f"{answer}\n\n{CAUTION}".strip()


def _refusal_answer(res: dict, code: str, why: str) -> dict:
    """다시 계산 대신 답변 불가 사유로 끝낸 답 (refusal.TEMPLATES 의 문장, 숫자 없음)"""
    return {
        "status": code,
        "answer": refusal.TEMPLATES[code],
        "parsed": dict(res.get("parsed") or {}),
        "result": {"status": code, "refusal": code, "why": why},
        "sources": [],
        "trace": {"refusal": {"from": res.get("status"), "to": code, "why": why}},
    }


async def _act(
    name: str, res: dict, verdict: dict, state: dict | None, asked: str, user_id: int | None
) -> tuple[dict | None, str]:
    """행동 하나 실행 (설계 2-3: 행동마다 함수)"""
    if name == agent_action.REPICK:
        return await _repick_compute(res, verdict, asked)
    if name == agent_action.REPLAN:
        return await _replan_compute(res, verdict, asked, user_id)
    return await _retable(res, state, asked, user_id)


async def _retry(
    action,
    res: dict,
    verdict: dict,
    state: dict | None,
    asked: str,
    user_id: int | None,
    done: set[str] | None = None,
) -> tuple[dict | None, str]:
    """행동대로 한 번 다시 계산. 다시 계산하지 못하면 같은 회에 대체 행동(action.fallback)을 한다.
    done: 쓴 행동 (대체도 적는다)"""
    again, how = await _act(action.name, res, verdict, state, asked, user_id)
    if again is not None or action.fallback is None:
        return again, how
    flow.add("다시 계산", "대체", f"{how}: 다시 계산하지 못함 → {action.fallback}")
    if done is not None:
        done.add(action.fallback)  # 대체 행동으로 떨어졌다: 그 행동도 쓴 것으로
    first = _retry_outcome.get() or {}
    _retry_outcome.set(None)
    try:
        again, how = await _act(action.fallback, res, verdict, state, asked, user_id)
    except Exception:
        if not first.get("clarification"):
            raise
        logger.exception("대체 계산 실패 → 먼저 찾은 되묻기 유지")
        _retry_outcome.set(first)
        return None, how
    if again is None:
        last = _retry_outcome.get() or first
        if first.get("clarification") and not last.get("clarification"):
            last = {**last, "clarification": first["clarification"]}
        _retry_outcome.set(last)
    return again, how


FROM_TABLES = "tables"


def failed_retryable(res: dict) -> str | None:
    """값 없는 실패 입구 (설계 2-8): 표 경로가 값을 못 내고 통계표 안내로 끝난 새 질문 → 실패 이유.
    refusal.finalize 가 통계표 안내를 partial_match(후보 조건 일부 ✘)로 바꾼 것도 들인다.
    되묻기 · 진짜 답변 불가(비공개 · 범위 밖 · 모집단 · 연령 구간) · 사전 경로 실패 · 여러 지표 ·
    계산을 시도하지 않은 통계표 안내는 들이지 않는다.
    꾸러미(_found_now)와 계산 기록이 있어야 한다"""
    if res.get("request_type") not in (NEW, REPLY) or res.get("clarification"):
        return None
    status = res.get("status")
    came = ((res.get("trace") or {}).get("refusal") or {}).get("from")
    if not (status == FROM_TABLES or (status == "partial_match" and came == FROM_TABLES)):
        return None
    if repick_blocked(res, {}):
        return None
    found = _found_now.get()
    if agent_action.picked_candidate(found) is None:
        return None
    steps = (res.get("trace") or {}).get("dynamic") or []
    if not steps:
        return None  # 계산을 시도하지 않은 통계표 안내 (통계표 자체를 물음 · 고정 안내 · 표 고르기)
    tried = [st for st in steps if st.get("skip") != dynamic_indicator.EXACT_SKIP]
    if status == "partial_match" or (
        tried and all(str(st.get("skip") or "").startswith("통계표 찾기 조건") for st in tried)
    ):
        return agent_action.NO_VALUE_CHECKS
    return agent_action.NO_VALUE_CELLS


def _failure_missing(res: dict) -> str:
    """값 없는 실패의 '빠진 것': 계산 기록의 실패 이유 (recell 에 판정 지적 대신 넘긴다)"""
    steps = (res.get("trace") or {}).get("dynamic") or []
    skips = [str(st["skip"]) for st in steps if st.get("skip")]
    return " · ".join(dict.fromkeys(skips))[:200]


async def _judged(res: dict, state: dict | None, question: str, user_id: int | None) -> None:
    """답 판정 (ANSWER_JUDGE=true): 이 답이 질문에 맞는지 Gemma 가 보고, 틀렸다고 보면 한 번 다시
    계산한다. 다시 한 답이 판정을 통과할 때만 바꾸고, 아니면 처음 답을 그대로 낸다 (11/22 판정은
    맞는 답 107 중 12를 잘못 막았다. 그때 잃는 것을 줄인다). '답할 수 없는 질문' 판정은 기록만
    한다 (답변 불가는 따로 구현한다). 판정 내용은 trace["answer_judge"]"""
    judging = answer_judge.enabled()
    failure = None
    if not (judging and judgeable(res)):
        failure = failed_retryable(res) if get_settings().agent_on_failure else None
        if failure is None:
            return
    asked = res.get("question") or question
    if failure is None:
        progress.now(progress.CHECK)
        first = await answer_judge.verdict(asked, res.get("parsed"), res.get("result"))
        if first is None:
            return
        record: dict = {"first": first}
        flow.add("답 판정", first["verdict"], f"{first['reason']} · {first['missing']}")
    else:
        # 값 없는 실패 (설계 2-8): Gemma 판정 없이 코드의 실패 이유로 행동을 고른다
        first = {"verdict": "retry", "reason": failure, "missing": _failure_missing(res)}
        record = {"first": first, "entry": NO_VALUE_ENTRY}
        flow.add("값 없는 실패", failure, first["missing"] or "-")
    res.setdefault("trace", {})["answer_judge"] = record
    if first["verdict"] != "retry":
        return
    # 행동 고르기 (agent_action 3-3, 코드 규칙): 판정 · 실패 이유 × 검사(table_check)로 행동을
    # 정한다. 행동 넷 repick(칸) → retable(표) → replan(계획) → give_up, 좁은 것부터.
    # 첫 행동이 실패하거나 다시 한 답도 판정에 걸리면 다음 행동으로 이어 간다.
    # 상한은 agent_action(다시 2회 · replan 1회 · repick recell 은 따로 1회)과 60초.
    # 검사는 이유가 '조건이 빠짐 · 대상이 다름 · 값 없음(칸)'일 때만 돌린다
    verdict = first
    tried = llm_tried = 0
    no_value = failure is not None
    done: set[str] = set()
    attempts: list[dict] = []
    record["attempts"] = attempts
    asked_back: dict | None = None  # 다시 계산이 낸 되묻기 (고리 끝 경우 3)
    started = time.monotonic()
    while True:
        cells, fits, blocked = False, True, False
        if verdict.get("reason") in agent_action.REPICK_REASONS and agent_action.REPICK not in done:
            evidence = await repick_cells(res, verdict, _found_now.get(), asked)
            cells, fits = bool(evidence["cells"]), bool(evidence.get("fits", True))
            blocked = bool(evidence.get("blocked"))
            record.setdefault("repick_cells", []).append(evidence)
        llm_tried = int(agent_action.REPLAN in done)  # 대체로 돈 replan 도 센다
        action = agent_action.choose(
            verdict,
            tried,
            llm_tried,
            frozenset(done),
            cells=cells,
            fits=fits,
            repick_ok=not blocked,
        )
        attempt: dict = {"action": action.as_trace()}
        attempts.append(attempt)
        if len(attempts) == 1:
            record["action"] = action.as_trace()  # 첫 시도는 전처럼 action · retry · second 칸에도
        flow.add("행동 고르기", action.name, action.why)
        if action.name == agent_action.GIVE_UP:
            attempt["how"], attempt["computed"] = "give_up", False
            record.setdefault("retry", {"how": "give_up", "computed": False})
            return _loop_end(res, record, "give_up", asked_back, asked)
        if time.monotonic() - started > agent_action.MAX_SECONDS:
            flow.add("다시 계산", "중단", f"{agent_action.MAX_SECONDS}초 넘음")
            attempt["how"], attempt["computed"] = "timeout", False
            return _loop_end(res, record, "timeout", asked_back, asked)
        done.add(action.name)
        tried += 1
        _retry_outcome.set(None)
        try:
            again, how = await _retry(action, res, verdict, state, asked, user_id, done)
        except Exception:
            logger.exception("답 판정 뒤 다시 계산 실패")
            attempt["error"] = True
            record.setdefault("retry", {"error": True})
            if asked_back is not None:
                _loop_end(res, record, "error", asked_back, asked)
            return
        attempt.update(how=how, computed=again is not None)
        if again is not None:  # 버려도 무엇이었는지 · 판정이 무엇을 봤는지 남긴다
            attempt["answer"] = lead(again.get("answer") or "")[:200]
            attempt["summary"] = answer_judge.describe(again.get("result"), again.get("parsed"))
        if len(attempts) == 1:
            record["retry"] = {k: v for k, v in attempt.items() if k != "action"}
        if again is None:
            outcome = _retry_outcome.get() or {}
            if outcome.get("status"):
                attempt["fail_status"] = outcome["status"]
            if outcome.get("clarification"):
                asked_back = outcome["clarification"]  # 뒤에 성공한 답이 없으면 이 되묻기를 낸다
                attempt["clarification"] = True
                flow.add("다시 계산", "되묻기", f"{how}: 되물어야 함 → 다음 행동, 없으면 전달")
                continue
            flow.add("다시 계산", "실패", f"{how}: 계산하지 못함 → 다음 행동")
            continue
        if no_value and again.get("status") != "ok":
            # 값 없는 실패를 다른 답변 불가 · 값 없는 답으로 바꾸지 않는다 (처음 답이 이미 표 안내)
            flow.add("다시 계산", "실패", f"{how}: {again.get('status')} → 다음 행동")
            attempt["fail_status"] = again.get("status")
            continue
        if again.get("status") in refusal.TEMPLATES:
            if no_value:
                # 값 없는 실패를 다른 답변 불가로 바꾸지 않는다 (처음 답이 이미 표 안내 · 사유)
                flow.add(
                    "다시 계산", "실패", f"{how}: 답변 불가 사유 {again['status']} → 다음 행동"
                )
                attempt["fail_status"] = again["status"]
                continue
            # 다시 계산이 답변 불가 사유로 끝남: 판정 없이 그 사유로 바꾼다 (설계서 4절 B-1 예외)
            flow.add(
                "답변 불가 사유", again["status"], f"{how}: {refusal.NAMES.get(again['status'])}"
            )
            second = None
        elif no_value and not judging:
            # 판정을 끈 채 값 없는 실패를 고쳤다: 값이 없던 답을 그 값으로 바꾼다
            flow.add("다시 계산", "판정 없이 받음", f"{how}: 처음 답에 값이 없었음")
            second = None
        else:
            progress.now(progress.CHECK)
            second = await answer_judge.verdict(asked, again.get("parsed"), again.get("result"))
            attempt["second"] = second
            if len(attempts) == 1:
                record["second"] = second
            if not second or second["verdict"] != "ok":
                why = f"{second['reason']} · {second['missing']}" if second else "판정하지 못함"
                flow.add(
                    "다시 계산", "버림", f"{how}: 다시 한 답도 판정에 걸림 ({why}) → 다음 행동"
                )
                if second:
                    verdict = second  # 새 판정 이유로 다음 행동을 고른다
                continue
        record["replaced"] = {
            "indicator": (res.get("parsed") or {}).get("indicator"),
            "answer": lead(res.get("answer") or ""),
            "attempt": len(attempts),
        }
        flow.add("다시 계산", "ok", f"{how}: {lead(again.get('answer') or '')}")
        kind = res.get("request_type")
        res.clear()
        res.update(again)
        res["trace"]["answer_judge"] = record
        res["chart"] = charts.build(res.get("result"))
        await _attach_cards(res, asked)
        res["request_type"], res["question"] = kind, asked
        return


def ungrounded(res: dict) -> bool:
    parsed = res.get("parsed") or {}
    return (
        res.get("status") in DICTIONARY_ANSWERED
        and res.get("request_type") == NEW
        and parsed.get("grounded") is False
        and not parsed.get("dynamic_spec")
    )


def _monthly_scope(result: dict) -> set[tuple]:
    return {
        (tuple(r["months"]), r.get("year"), r.get("sex") or "계")
        for r in qa._leaves(result)
        if r.get("months")
    }


async def _dynamic_instead(res: dict, state: dict | None, asked: str, user_id: int | None) -> bool:
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    trace = res.get("trace") or {}
    parsed = res.get("parsed") or {}
    found = None
    if parsed.get("rank_axis") and (axis := await _axis_rank(asked, parsed, trace)):
        kind = res.get("request_type")
        res.clear()
        res.update(axis)
        res["chart"] = charts.build(res.get("result"))
        await _attach_cards(res, asked)
        res["request_type"], res["question"], res["search_session"] = kind, asked, sid
        return True
    if parsed.get("also_asked") and res.get("status") == "ok":
        return await _with_also_asked(res, parsed, asked, sid, user_id, trace)
    try:
        computed = await dynamic_indicator.cached_answer(asked, parsed, trace)
        if computed is None:
            found = _keep(await table_guide.find_tables(asked, sid, None, user_id))
            computed = await dynamic_indicator.compute_from_handoff(asked, parsed, found, trace)
    except Exception:
        logger.exception("근거 없는 지표의 바로 계산 실패")
        return False
    if computed is None and found is None:
        return False
    handed_off = bool(parsed.get("gate_reasons"))
    if (
        res.get("status") == "ok"
        and not handed_off
        and (same := _same_dictionary_table(parsed, computed, found))
    ):
        trace["dynamic_kept"] = {"indicator": parsed.get("indicator"), "tbl_id": same}
        return False
    if res.get("status") == "ok" and same_number(res.get("result"), (computed or {}).get("result")):
        # 바로 계산이 다른 표에서 같은 값 · 단위 · 연도를 다시 냈다: 사전 답을 둔다 (이름이 깔끔하고
        # 부부 단위 같은 안내가 붙는다). 11/24 평가: 혼인 건수를 부부 혼인종류 표의 남편 · 아내
        # 총계로 다시 내고, 판정이 그 요약을 막아 51초를 썼다. 값이 다르면 전처럼 바꾼다
        trace["dynamic_kept"] = {
            "indicator": parsed.get("indicator"),
            "same_value": (res.get("result") or {}).get("value"),
        }
        return False
    months = _monthly_scope(res.get("result") or {})
    if (
        res.get("status") == "ok"
        and months
        and not months <= _monthly_scope((computed or {}).get("result") or {})
    ):
        trace["dynamic_kept"] = {"indicator": parsed.get("indicator"), "months": list(months)}
        return False
    # 바꾼 원래 답 (요청 기록·회귀 평가에서 사전 지표 답과 비교한다)
    trace["replaced"] = {
        "indicator": parsed.get("indicator"),
        "answer": lead(res.get("answer") or ""),
        "by": (computed or {}).get("status") or "tables",
    }
    kind = res.get("request_type")
    res.clear()
    if computed is not None:
        res.update(computed)
        if not res.get("clarification"):
            res["chart"] = charts.build(res.get("result"))
            await _attach_cards(res, asked)
    else:
        res.update(table_guide.tables_answer(found, {"parsed": parsed}))
        res["trace"] = trace
    res["request_type"], res["question"], res["search_session"] = kind, asked, sid
    return True


async def _with_also_asked(
    res: dict, parsed: dict, asked: str, sid: str, user_id: int | None, trace: dict
) -> bool:
    also = parsed["also_asked"]
    question = qa.measure_question(asked, also)
    target = {"span": also, "unsaid_age": not multi_indicator.says_age(question)}
    sub = multi_indicator.target_parsed(parsed, target)
    if not target["unsaid_age"] and parsed.get("also_asked_ages"):
        sub.update(parsed["also_asked_ages"])
    computed = found = None
    try:
        computed = await dynamic_indicator.cached_answer(question, sub, trace)
        if computed is None:
            found = await table_guide.find_tables(question, sid, None, user_id)
            computed = await dynamic_indicator.compute_from_handoff(question, sub, found, trace)
    except Exception:
        logger.exception("함께 물은 지표의 바로 계산 실패: %s", also)
    trace["also_asked"] = {
        "term": also,
        "question": question,
        "by": (computed or {}).get("status") or (found or {}).get("status"),
    }
    pair = [(parsed.get("indicator") or "", dict(res)), (also, computed)]
    both = (
        multi_indicator.side_by_side(asked, parsed, pair, trace)
        if _one_indicator(computed)
        else None
    )
    if both is None:
        res["answer"] = f"{res.get('answer') or ''}\n※ {_also_asked_note(also, found)}"
        res["also_missing"] = also  # 한쪽만 답함: 보고서에 담지 않는다 (figure.reportable)
        return False
    kind = res.get("request_type")
    res.clear()
    res.update(both)
    res["chart"] = charts.build(res.get("result"))
    await _attach_cards(res, asked)
    res["request_type"], res["question"], res["search_session"] = kind, asked, sid
    return True


def _also_asked_note(also: str, found: dict | None) -> str:
    said = josa(f"'{also}'", "은는")
    recs = (found or {}).get("recommendations") or []
    if (found or {}).get("status") not in ("out_of_scope", None) and recs:
        where = f"'{recs[0].get('tbl_nm') or recs[0].get('tbl_id')}' 표에서 확인할 수 있습니다"
        return f"함께 물은 {said} 바로 계산하지 못했습니다 ({where})."
    return f"함께 물은 {said} 맞는 통계표를 찾지 못해 답에 넣지 못했습니다."


async def _after_table_choice(pending: dict | None, found: dict) -> dict | None:
    original = (pending or {}).get("original_question")
    parsed = (pending or {}).get("parsed")
    if not original or not parsed or table_guide.waiting_choice(found):
        return None
    trace: dict = {}
    try:
        computed = await dynamic_indicator.compute_from_handoff(original, parsed, found, trace)
    except Exception:
        logger.exception("표를 고른 뒤 바로 계산 실패")
        return None
    if not computed or computed.get("status") != "ok":
        return None
    res = dict(computed)
    res["chart"] = charts.build(res.get("result"))
    await _attach_cards(res, original)
    res.update(request_type=REPLY, question=original, trace=trace)
    return res


async def _axis_rank(asked: str, parsed: dict, trace: dict) -> dict | None:
    spec = indicators.spec(parsed.get("indicator") or "")
    if not spec or spec.get("method") != "lookup":
        return None
    q = {**parsed, "regions": parsed.get("regions") or ["전국"], "dynamic_spec": None}
    step: dict = {"dynamic": []}
    for src in spec.get("sources") or []:
        try:
            part, _ = await dynamic_indicator._try(src["table"], None, asked, q, step, {})
        except Exception:
            logger.exception("분류 순위 계산 실패 (%s)", src.get("table"))
            continue
        if (part or {}).get("status") == "ok" and part.get("answer"):
            trace["axis_rank"] = {"tbl_id": src["table"], "axis": parsed.get("rank_axis")}
            return part
    return None


def same_number(a: dict | None, b: dict | None) -> bool:
    """두 결과가 값 하나짜리이고 값 · 단위 · 연도가 같은가 (사전 답과 표 경로 답이 같은 숫자인지)"""
    if not a or not b or a.get("status") != "ok" or b.get("status") != "ok":
        return False
    va, vb = a.get("value"), b.get("value")
    if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
        return False
    if a.get("unit") != b.get("unit") or a.get("year") != b.get("year"):
        return False
    return abs(float(va) - float(vb)) <= max(1e-9, 1e-6 * abs(float(va)))


def _one_indicator(computed: dict | None) -> bool:
    return bool(
        computed
        and computed.get("status") == "ok"
        and not computed.get("clarification")
        and (computed.get("result") or {}).get("split_by") != "indicator"
    )


def _same_dictionary_table(parsed: dict, computed: dict | None, found: dict | None) -> str | None:
    spec = indicators.spec(parsed.get("indicator") or "")
    tables = {src.get("table") for src in (spec or {}).get("sources") or []}
    if not tables:
        return None
    picked: list[str | None] = []
    if computed:
        picked += [s.get("tbl_id") for s in computed.get("sources") or []]
        context = (computed.get("clarification") or {}).get("context") or {}
        picked.append(context.get("tbl_id"))
    elif found:
        picked += [r.get("tbl_id") for r in (found.get("recommendations") or [])[:1]]
    return next((t for t in picked if t and t in tables), None)


def _attach_figure(res: dict) -> None:
    """대표 수치(figure)와 보고서에 담을 수 있는지(reportable)를 붙인다. 답은 바꾸지 않는다"""
    try:
        res["figure"] = figure.figure_of(res.get("result"), res.get("parsed"))
        res["reportable"] = figure.reportable(res)
    except Exception:
        logger.exception("대표 수치 만들기 실패")
        res["figure"], res["reportable"] = None, False


def _summary(result: dict | None) -> dict | None:
    try:
        return answer_summary.build(result)
    except Exception:
        logger.exception("답변 요약 실패")
        return None


async def handle(
    session_id: str | None,
    question: str,
    user_id: int | None = None,
    selections: dict | None = None,
    listed: bool = True,
    chart_view: str | None = None,
) -> dict:
    """listed=False: 지난 대화 목록에 남기지 않는다 (보고서 에이전트가 따로 조회할 때).
    chart_view: 로그인하지 않은 화면이 보낸 마지막 차트의 보기 (로그인하면 저장한 보기를 읽는다)"""
    sid = session_id or new_id()
    steps = flow.start()
    calls = llm_calls.start()
    understand_merged.start()
    async with _lock(sid):
        state, expired = await _load(sid)
        moved = state is not None and not same_owner(state, user_id)
        if state is None and session_id:
            sid = new_id()
        if moved:
            sid, state, expired = new_id(), None, False
        if user_id is not None and state is not None:
            await _restore_chart_view(sid, state, user_id)
        elif state is not None:
            # 로그인하지 않은 대화는 보기를 저장할 곳이 없어 화면이 질문과 함께 보낸다.
            # 없으면 막대로 바꾼 뒤 '자세히 말해줘'가 처음 모양(꺾은선)으로 돌아갔다
            _show_chart_as(state, chart_view)
        # 보고서 말은 에이전트로 (#249 의 '정리해줘' 보고서는 _turn 안에서 따로 간다)
        if listed and report_router.wants(question, state):
            standalone_stat = bool(
                report_router.STAT_REQUEST.search(question)
                and await asyncio.to_thread(indicators.find_in_text, question)
            )
            if report_router.wants(question, state, standalone_stat=standalone_stat):
                return await _agent_turn(
                    sid, state, question, user_id, steps, calls, expired, moved
                )
            if state and (agent := report_router.agent_state(state)).get("pending"):
                state = {**state, "report_agent": {**agent, "pending": None, "candidates": []}}
        _found_now.set(None)
        _session_now.set(sid)
        res, sources, notice = await _turn(state, question, selections, user_id)
        talk = res.get("talk_summary")
        report_ref = res.get("report_ref")
        asked = talk["asked"] if talk else question
        if res.get("request_type") == NEW:
            # 새 질문도 함께 말한 차트 모양으로 ('부산 출생아 수 최근 5년 막대그래프로').
            # 새 질문 경로는 여러 곳에서 나가 여기서 한 번 붙인다 (조건 변경은 _turn이 붙인다)
            told = with_asked_shape(res, asked, None)
            notice = " ".join(filter(None, [notice, told])) or None
        await _attach_tables(res, state, asked, user_id)
        await refusal.finalize(res, asked)
        await _judged(res, state, asked, user_id)
        progress.now(progress.WRAP_UP)
        if "summary" not in res:  # 직전 답을 다시 쓴 응답(_reuse)은 그 요약을 이미 들고 있다
            res["summary"] = _summary(res.get("result"))
        _attach_figure(res)
        if sex_not_split(res.get("parsed") or {}, res.get("result") or {}):
            word = "남자" if res["parsed"]["sex"] == "남" else "여자"
            told = f"이 지표는 성별로 나뉜 자료가 없어 {word}만 보지 못하고 전체 값으로 답했습니다."
            notice = " ".join(filter(None, [notice, told]))
        if not res.get("chart") and (reason := charts.omission_reason(res.get("result"))):
            notice = " ".join(filter(None, [notice, reason]))
        if expired:
            notice = " ".join(filter(None, ["시간이 지나 이전 조건이 초기화됐습니다.", notice]))
        if moved:
            notice = " ".join(
                filter(None, ["로그인 상태가 바뀌어 새 대화로 시작했습니다.", notice])
            )
        after = next_state(state, question, res, sources)
        after["user_id"] = user_id
        if listed:
            report_offer.update(state, res, after, user_id)
        if talk:
            parts = [talk["answer"], res.get("answer") or ""]
            if talk["first"] == "question":
                parts.reverse()
            res["answer"] = "\n\n".join(p for p in parts if p).strip()
            res["talk_summary"] = talk
        if report_ref:
            res["report_ref"] = report_ref
        res.setdefault("question", question)
        res["session_id"] = sid
        res["applied"] = (
            []
            if res.get("status") in (TABLES, *table_guide.REFUSED)
            else applied(
                after.get("conditions") or {},
                after.get("sources") or {},
                include_regions=(res.get("result") or {}).get("status")
                not in {
                    "unknown_region",
                    "ambiguous_region",
                    "missing_region",
                    "region_not_in_year",
                },
            )
        )
        res["notice"] = notice
        first_line = (res.get("answer") or "").strip().split("\n")[0][:80]
        flow.add("최종 답", res.get("status"), f"{res.get('request_type') or ''} · {first_line}")
        res.setdefault("trace", {})["flow"] = list(steps)
        res["trace"]["llm_calls"] = list(calls)
        await _save(sid, after, res, state, question, user_id, listed)
    return res


async def _agent_turn(
    sid: str,
    state: dict | None,
    question: str,
    user_id: int | None,
    steps: list,
    calls: list,
    expired: bool,
    moved: bool,
) -> dict:
    """보고서 경로 (명세서 7장). 앞 질문의 조건은 그대로 둬서 다음 통계 질문이 이어진다"""
    from app.agent import report_agent

    progress.now(progress.REPORT)
    res = await report_agent.run(question, sid, state, user_id)
    mine = res.pop("report_agent_state")
    prev = state or {}
    turn_no = (prev.get("turn_no") or 0) + 1
    before = prev.get("report_agent") or {}
    after = {
        **prev,
        "turn_no": turn_no,
        "user_id": user_id,
        "report_agent": {**before, **mine, "last_turn": turn_no},
    }
    notes = []
    if expired:
        notes.append("시간이 지나 이전 조건이 초기화됐습니다.")
    if moved:
        notes.append("로그인 상태가 바뀌어 새 대화로 시작했습니다.")
    record = res["trace"]["report_agent"]
    flow.add(
        "보고서 에이전트",
        record["end"],
        " → ".join(str(x.get("action")) for x in record["steps"]) or "-",
    )
    res.update(
        session_id=sid, applied=[], notice=" ".join(notes) or None, figure=None, reportable=False
    )
    res["trace"]["flow"] = list(steps)
    res["trace"]["llm_calls"] = list(calls)
    await _save(sid, after, res, state, question, user_id)
    return res


def _key(session_id: str) -> str:
    return f"ask:{session_id}"


async def _load(session_id: str) -> tuple[dict | None, bool]:
    try:
        return await load(session_id, now())
    except (SQLAlchemyError, OSError):
        logger.exception("대화 상태 읽기 실패")
        return None, False


def _show_chart_as(state: dict, view: str | None) -> None:
    """직전 차트를 화면에서 고른 보기로 둔다. 그 차트로 그릴 수 없는 보기는 버린다"""
    last = state.get("last") or {}
    if view and (chart := last.get("chart")) and charts.view_fits(chart, view):
        last["chart"] = {**chart, "view": view}


async def _restore_chart_view(sid: str, state: dict, user_id: int) -> None:
    last = state.get("last") or {}
    if not last.get("chart"):
        return
    try:
        # 차트 보기는 캐시된 대화 상태와 별도로 저장되므로 원본 턴에서 읽는다.
        view = await conversations.chart_view(user_id, sid, state.get("turn_no") or 0, last)
        _show_chart_as(state, view)
    except (SQLAlchemyError, OSError):
        logger.exception("직전 차트 보기 읽기 실패")


async def _save(
    sid: str,
    after: dict,
    res: dict,
    before: dict | None,
    question: str,
    user_id,
    listed: bool = True,
) -> None:
    continuing = user_id is not None and before is not None
    try:
        async with turn_writer(sid, user_id, continuing) as conn:
            if conn is None:
                # 이어 가는 대화를 답을 만드는 사이 사용자가 지웠다. 그대로 저장하면 같은 ID로
                # 다시 생겨 지운 대화가 목록에 되살아났다 (11/26 리뷰)
                logger.info("답을 만드는 사이 지운 대화라 저장하지 않음")
                return
            turn_no = after.get("turn_no") or 1
            turn_id = await log_turn(sid, user_id, turn_no, question, res, before, after, conn)
            last = after.get("last") or {}
            if user_id is not None and res.get("chart") and last.get("chart") is res["chart"]:
                last["chart_turn_id"] = turn_id
            await save(sid, after, conn)
            if user_id is not None and listed:  # 보고서 에이전트의 따로 조회는 목록에 남기지 않는다
                await conversations.touch(sid, user_id, question, conn)
        if user_id is not None:
            res["turn_id"] = turn_id  # 저장이 끝난 로그인 답만 보고서에 담을 수 있다
    except (SQLAlchemyError, OSError):
        STATE_CACHE.delete(sid)  # 되돌린 저장을 캐시에 남기지 않는다
        logger.exception("대화 상태 저장 실패")


@contextlib.asynccontextmanager
async def turn_writer(session_id: str, user_id: int | None, continuing: bool):
    """한 턴의 저장(상태 · 턴 기록 · 대화 목록)을 트랜잭션 하나로 → 연결, 지운 대화면 None.
    이어 가는 대화는 대화 행을 잠근다(for update). 지우기(conversations.delete)도 대화 행부터
    지우므로 둘이 겹치면 한쪽이 끝날 때까지 기다린다: 지우기가 먼저면 행이 없어 저장하지 않고,
    저장이 먼저면 지우기가 새 턴까지 함께 지운다"""
    from app.core.db import engine

    async with engine.begin() as conn:
        if continuing and not await _conversation_held(conn, session_id, user_id):
            yield None
        else:
            yield conn


async def _conversation_held(conn, session_id: str, user_id: int | None) -> bool:
    held = await conn.execute(
        text(
            "select 1 from app.conversations where session_id = :sid and user_id = :uid for update"
        ),
        {"sid": session_id, "uid": user_id},
    )
    if held.first():
        return True
    # 대화 행이 처음부터 없는 대화(첫 턴의 목록 저장이 실패함)는 상태가 남아 있으면 이어 간다.
    # 지운 대화는 상태도 함께 지워졌다
    state = await conn.execute(
        text("select 1 from app.chat_sessions where session_id = :k"), {"k": _key(session_id)}
    )
    return state.first() is not None


@contextlib.asynccontextmanager
async def _within(conn=None):
    """conn이 있으면 그 트랜잭션 안에서, 없으면 새 트랜잭션으로"""
    if conn is not None:
        yield conn
        return
    from app.core.db import engine

    async with engine.begin() as own:
        yield own


def keeps_for(state: dict) -> timedelta:
    return TURNS_FOR if state.get("user_id") is not None else SESSION_FOR


async def load(session_id: str, at: datetime) -> tuple[dict | None, bool]:
    cached = STATE_CACHE.get(session_id)
    if cached is not MISSING:
        slots, updated_at = cached
    else:
        from app.core.db import engine
        from app.services import dialog

        async with engine.begin() as conn:
            await dialog._ensure(conn)
            row = (
                await conn.execute(
                    text("select slots, updated_at from app.chat_sessions where session_id = :k"),
                    {"k": _key(session_id)},
                )
            ).first()
        if row is None or not isinstance(row.slots, dict) or "conditions" not in row.slots:
            return None, False
        slots, updated_at = row.slots, row.updated_at
        STATE_CACHE.set(session_id, (slots, updated_at))
    if at - updated_at > keeps_for(slots):
        return None, True
    return copy.deepcopy(slots), False


async def save(session_id: str, state: dict, conn=None) -> None:
    from app.services import dialog

    async with _within(conn) as conn:
        await dialog._ensure(conn)
        await conn.execute(
            text(dialog.UPSERT),
            {
                "session_id": _key(session_id),
                "status": (state.get("last") or {}).get("status"),
                "family_id": None,
                "slots": json.dumps(state, ensure_ascii=False, default=str),
                "asked": None,
                "turns": 0,
            },
        )
        await conn.execute(
            text(
                "delete from app.chat_sessions where session_id like 'ask:%' and ("
                " (slots->>'user_id' is null and updated_at < now() - interval '1 day')"
                " or updated_at < now() - make_interval(days => :days))"
            ),
            {"days": TURNS_FOR.days},
        )
    STATE_CACHE.set(session_id, (copy.deepcopy(state), now()))


async def log_turn(
    session_id: str,
    user_id: int | None,
    turn_no: int,
    question: str,
    res: dict,
    before: dict | None,
    after: dict,
    conn=None,
) -> int | None:
    """질문 하나를 app.chat_turns에 남기고 그 행의 id를 돌려준다"""
    result = res.get("result") or {}
    async with _within(conn) as conn:
        row = await conn.execute(
            text(
                "insert into app.chat_turns (session_id, user_id, turn_no, message, request_type,"
                " status, conditions_before, conditions_after, sources, tbl_id, recomputed, answer,"
                " response, flow)"
                " values (:session_id, :user_id, :turn_no, :message, :request_type, :status,"
                " cast(:before as jsonb), cast(:after as jsonb), cast(:sources as jsonb),"
                " :tbl_id, :recomputed, :answer, cast(:response as jsonb), cast(:flow as jsonb))"
                " returning id"
            ),
            {
                "session_id": session_id,
                "user_id": user_id,
                "turn_no": turn_no,
                "message": mask(question),
                "request_type": res.get("request_type"),
                "status": res.get("status"),
                "before": json.dumps(
                    redact((before or {}).get("conditions")), ensure_ascii=False, default=str
                ),
                "after": json.dumps(
                    redact(after.get("conditions")), ensure_ascii=False, default=str
                ),
                "sources": json.dumps(redact(after.get("sources")), ensure_ascii=False),
                "tbl_id": result.get("tbl_id"),
                "recomputed": not result.get("reused") and res.get("request_type") != UNCLEAR,
                "answer": mask(lead(res.get("answer") or "")),
                "response": json.dumps(
                    redact(conversations.replay(res)), ensure_ascii=False, default=str
                ),
                "flow": json.dumps(
                    redact((res.get("trace") or {}).get("flow")), ensure_ascii=False, default=str
                ),
            },
        )
        turn_id = row.scalar()
        await conn.execute(
            text("delete from app.chat_turns where created_at < :cutoff"),
            {"cutoff": now() - TURNS_FOR},
        )
    return turn_id
