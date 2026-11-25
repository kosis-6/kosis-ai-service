import asyncio
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

from app.agent import replan
from app.prompts.qa import FOLLOW_SYSTEM
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
    flow,
    indicators,
    llm_calls,
    lookup,
    multi_indicator,
    qa,
    question_clarify,
    refusal,
    slots,
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
NEW, CHANGE, CHART, SUMMARY, REPLY, UNCLEAR = (
    "new",
    "change",
    "chart",
    "summary",
    "reply",
    "unclear",
)
TABLES = "tables"
ASK_INDICATOR = "asked_place"
REGION_RETRY_STATUSES = ("missing_data", "no_region_level")
TYPES = {"value", "trend", "compare", "rank", "composition"}
NO_CONDITIONS = {"other", "parse_error", None}

CHART_WORDS = {
    "table": re.compile(r"표\s*로|표\s*형태|표로\s*정리"),
    "bar": re.compile(r"막대"),
    "line": re.compile(r"꺾은\s*선|선\s*그래프|라인"),
}
CHART_REQUEST = re.compile(r"(보여|바꿔|그려|볼래|보고|줘|주세요|로$|으로$)")
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
        "indicator": ", ".join(conditions.get("indicators") or []) or conditions.get("indicator"),
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
    if count >= 1 and span:
        c["type"] = "trend"
    elif count > 1:
        c["type"] = "compare"
    elif count == 1:
        c["type"] = "value"
    elif kind != "rank":
        c["type"] = "rank"


CONTEXT_YEAR = [
    (re.compile(r"전년도|전년|(?:그|직)\s*전\s*해|이전\s*해|직전\s*연도|한\s*해\s*전"), -1),
    (re.compile(r"다음\s*해|이듬해|다음\s*연도|익년|한\s*해\s*(?:뒤|후)"), 1),
]
FULL_YEAR = re.compile(rf"(?:19|20)\d{{2}}{slots.NOT_YEAR_TAIL}")


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
    if indicators.find_in_text(question) or PEOPLE_WORDS.search(question):
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


def complete_question(question: str) -> bool:
    if re.search(r"같이|추가|빼|빼고|제외|대신|그대로|거기|거기에|보다|도\s", question):
        return False
    return bool(
        indicators.find_in_text(question)
        and has_period(question)
        and ("전국" in question or indicators.find_regions(question))
    )


def inherit(
    parsed: dict, sources: dict, prev: dict, prev_sources: dict, question: str = ""
) -> tuple[dict, dict]:
    p, s = copy.deepcopy(parsed), dict(sources)
    kind = p.get("type")
    if kind in NO_CONDITIONS or prev.get("type") in NO_CONDITIONS:
        return p, s
    if question and prev.get("indicator") and not names_indicator(question, p):
        keys = ["indicator", "term", "ages", "age_group", "age_note", "by", "dynamic_spec"]
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
    q = question.strip()
    if len(q) > REPLY_MAX_CHARS or ask_followup.has_explicit_year(q) or indicators.find_in_text(q):
        return None
    for view, pattern in CHART_WORDS.items():
        if pattern.search(q) and CHART_REQUEST.search(q):
            return view
    return None


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


REGION_ONLY_REST = re.compile(
    r"(?:이랑|[\s,·와과랑하고및])*(?:광역시|특별시|특별자치시|특별자치도|시|도|군|구)?\s*"
    r"(?:의\s*경우(?:에)?|쪽|에서|에선)?\s*(?:은|는|도|이면|면)?\s*요?\s*"
    r"(?:어때요?|어떤가요?|어떻게\s*돼요?)?\s*[?.!~]*\s*"
)
ALSO = re.compile(r"^\s*도\s*요?\s*(?:어때요?)?\s*[?.!~]*\s*$")


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
    return " ".join(p for p in (period_text(parsed), where, _with_demographics(what, parsed)) if p)


def summary_request(question: str) -> bool:
    return len(question.strip()) <= REPLY_MAX_CHARS and bool(SUMMARY_REQUEST.search(question))


def chart_with_view(chart: dict | None, view: str) -> dict | None:
    if not chart:
        return None
    if view == "table" or view == chart["type"] or (chart["type"] == "line" and view == "bar"):
        return {**chart, "view": view}
    return None


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
    if kind not in (CHANGE, CHART, SUMMARY, NEW):
        kind = NEW
    if kind == CHANGE and not changes and not unclear:
        kind = NEW
    return {
        "request_type": kind,
        "changes": changes,
        "chart": data.get("chart") if data.get("chart") in ("line", "bar", "table") else None,
        "unclear": unclear,
    }


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
    question: str, state: dict | None, *, scoped_age_ratio: bool = False
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
                parsed, kept["sources"], prev, state["sources"], question
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

    if (
        HOUSEHOLD_AGE_RATIO.fullmatch(question)
        or qa.housing_population_condition(question)
        or await asyncio.to_thread(complete_question, question)
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
    if view is None and (asked := await asyncio.to_thread(ratio_followup, question, state)):
        flow.add("비율 후속 질문", "새 질문으로", asked)
        res, sources, notice = await _new(
            asked, state, scoped_age_ratio=bool(state["conditions"].get("ages"))
        )
        res["request_type"], res["question"] = CHANGE, asked
        return res, sources, notice
    if view is None and not (has_last and summary_request(question)):
        trace: dict = {}
        split = await asyncio.to_thread(region_split_changes, question, state["conditions"])
        region_only = region_only or await asyncio.to_thread(
            region_only_changes, question, state["conditions"]
        )
        if composition_request(question):
            delta = parse_delta('{"request_type": "change", "changes": {"type": "composition"}}')
        elif split:
            delta = {"request_type": CHANGE, "changes": split, "chart": None, "unclear": None}
        elif region_only:
            delta = {"request_type": CHANGE, "changes": region_only, "chart": None, "unclear": None}
        else:
            delta = await extract(state, question, trace)
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
                if status in table_guide.SEARCH_INSTEAD or retry_region:
                    res["search_question"] = inherited_question(conditions)
                sources = {**state["sources"], **{k: "user" for k in changed if k in LABELS}}
                return res, sources, None
            delta["request_type"] = NEW
        if delta["request_type"] == CHART:
            view = delta["chart"]

    if has_last and (view or (delta and delta["request_type"] == CHART)):
        chart = chart_with_view(state["last"].get("chart"), view) if view else None
        if chart is None:
            reason = (
                "직전 답은 그 모양으로 그릴 수 없습니다."
                if view
                else "어떤 모양으로 바꿀지 알 수 없습니다."
            )
            return _unclear(state, reason), state["sources"], None
        words = {"line": "꺾은선 그래프", "bar": "막대그래프", "table": "표"}
        return _reuse(state, f"{words[view]}로 바꿨습니다.", chart, CHART), state["sources"], None
    if has_last and (delta is None or delta["request_type"] == SUMMARY):
        last = state["last"]
        return (
            _reuse(state, summary_text(last, question), last.get("chart"), SUMMARY),
            state["sources"],
            None,
        )

    return await _new(question, state)


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
        return {
            "conditions": effective(parsed, {}) if numeric else {},
            "sources": sources if numeric else {},
            "last": {
                "status": res.get("status"),
                "answer": res.get("answer") or "",
                "sources": [],
                "chart": None,
            },
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
    return {
        "conditions": conditions,
        "sources": sources,
        "last": {
            "status": res.get("status"),
            "answer": res.get("answer") or "",
            "sources": res.get("sources") or [],
            "chart": res.get("chart"),
            "summary": res.get("summary"),
        },
        "pending": pending_of(res.get("result") or {}, asked),
        "search_session": prev.get("search_session"),
        "recent": recent,
        "turn_no": turn_no,
    }


async def _attach_tables(res: dict, state: dict | None, question: str, user_id: int | None) -> None:
    if res.get("tables") is not None or res.get("request_type") in (
        CHART,
        SUMMARY,
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
        and status in REGION_RETRY_STATUSES
        and res.get("search_question")
        and (res.get("parsed") or {}).get("dynamic_spec")
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


def without_tables(found: dict, used: set[str]) -> dict | None:
    """찾은 후보에서 이미 답에 쓴 표를 뺀 꾸러미 (다음 후보로 다시 계산할 때).
    지표가 여럿인 질문이거나, 쓴 표가 후보에 없거나, 남는 후보가 없으면 None"""
    targets = (found.get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None
    cands = targets[0].get("candidates") or []
    rest = [{**c, "picked": False, "review": None} for c in cands if c.get("tbl_id") not in used]
    if not rest or len(rest) == len(cands):
        return None
    out = copy.deepcopy(found)
    out["handoff"]["targets"][0]["candidates"] = rest
    return out


async def _recompute(
    res: dict, state: dict | None, asked: str, user_id: int | None
) -> tuple[dict | None, str]:
    """판정이 틀렸다고 본 답을 다른 방법으로 한 번 다시 계산한 답 → (답, 방법). 못 하면 (None, 방법)
    통계표에서 계산한 답   그 표를 빼고 다음 후보로
    사전 지표로 답한 답    원래 질문 그대로 통계표 찾기로 (검색어를 새로 쓰지 않는다:
                          11/22 Gemma 가 새로 쓴 검색어는 정답 표 1위를 42 → 29로 줄였다)"""
    parsed = res.get("parsed") or {}
    sid = res.get("search_session") or (state or {}).get("search_session")
    sid = sid or table_guide.new_search_session()
    found = _found_now.get()
    trace: dict = {}
    if parsed.get("dynamic_spec"):
        how = "다음 후보 표"
        used = {s.get("tbl_id") for s in res.get("sources") or [] if s.get("tbl_id")}
        if found is None:
            found = _keep(await table_guide.find_tables(asked, sid, None, user_id))
        found = without_tables(found, used)
        if found is None:
            return None, how
    elif indicators.spec(parsed.get("indicator") or ""):
        how = "통계표 찾기로"
        if found is None:
            found = _keep(await table_guide.find_tables(asked, sid, None, user_id))
    else:
        return None, "다시 계산할 방법 없음"
    base = {**parsed, "dynamic_spec": None}
    computed = await dynamic_indicator.compute_from_handoff(asked, base, found, trace)
    if not computed or computed.get("status") != "ok" or computed.get("clarification"):
        return None, how
    again = dict(computed)
    again.setdefault("trace", trace)
    again["search_session"] = sid
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


async def _retry(
    action,
    res: dict,
    verdict: dict,
    state: dict | None,
    asked: str,
    user_id: int | None,
    done: set[str] | None = None,
) -> tuple[dict | None, str]:
    """행동대로 한 번 다시 계산. replan 이 새 계획을 못 내면 대체 행동(표 · 경로)으로 떨어진다.
    requery · recell 은 호출부가 아직 없어 바로 대체 행동으로 간다. done: 쓴 행동 (대체도 적는다)"""
    if action.name == agent_action.REPLAN:
        again, how = await _replan_compute(res, verdict, asked, user_id)
        if again is not None or action.fallback is None:
            return again, how
        flow.add("다시 계산", "대체", f"{how}: 새 계획 없음 → {action.fallback}")
    if done is not None and action.fallback:
        done.add(action.fallback)  # 대체 행동으로 떨어졌다: 그 행동도 쓴 것으로
    return await _recompute(res, state, asked, user_id)


async def _judged(res: dict, state: dict | None, question: str, user_id: int | None) -> None:
    """답 판정 (ANSWER_JUDGE=true): 이 답이 질문에 맞는지 Gemma 가 보고, 틀렸다고 보면 한 번 다시
    계산한다. 다시 한 답이 판정을 통과할 때만 바꾸고, 아니면 처음 답을 그대로 낸다 (11/22 판정은
    맞는 답 107 중 12를 잘못 막았다. 그때 잃는 것을 줄인다). '답할 수 없는 질문' 판정은 기록만
    한다 (답변 불가는 따로 구현한다). 판정 내용은 trace["answer_judge"]"""
    if not answer_judge.enabled() or not judgeable(res):
        return
    asked = res.get("question") or question
    first = await answer_judge.verdict(asked, res.get("parsed"), res.get("result"))
    if first is None:
        return
    record: dict = {"first": first}
    res.setdefault("trace", {})["answer_judge"] = record
    flow.add("답 판정", first["verdict"], f"{first['reason']} · {first['missing']}")
    if first["verdict"] != "retry":
        return
    # 행동 고르기 (N9, 코드 규칙): 판정 이유 · 답의 경로 · 1위 후보의 ✔/✘로 다음 행동을 정한다.
    # 호출부가 있는 행동은 replan(_replan_compute) · reroute · retable(둘 다 _recompute) · give_up.
    # requery · recell은 이름만 기록하고 fallback으로 떨어진다 (게이트 · 연결 단계가 들어오면)
    # 행동 고리 (N9 → 다시 계산 → 판정, 설계서 4-1 · 4-5): 첫 행동이 실패하거나 다시 한 답도
    # 판정에 걸리면 다음 행동으로 이어 간다. 상한은 agent_action(다시 2회 · LLM 1회)과 60초.
    # 호출부가 있는 행동은 replan(_replan_compute) · reroute · retable(둘 다 _recompute) · give_up.
    # requery · recell은 이름만 기록하고 fallback으로 떨어진다 (게이트 · 연결 단계가 들어오면)
    verdict = first
    tried = llm_tried = 0
    done: set[str] = set()
    attempts: list[dict] = []
    record["attempts"] = attempts
    started = time.monotonic()
    while True:
        action = agent_action.choose(
            verdict, res, _found_now.get(), tried, llm_tried, frozenset(done)
        )
        attempt: dict = {"action": action.as_trace()}
        attempts.append(attempt)
        if len(attempts) == 1:
            record["action"] = action.as_trace()  # 첫 시도는 전처럼 action · retry · second 칸에도
        flow.add("행동 고르기", action.name, action.why)
        if action.name == agent_action.GIVE_UP:
            attempt["how"], attempt["computed"] = "give_up", False
            record.setdefault("retry", {"how": "give_up", "computed": False})
            return
        if time.monotonic() - started > agent_action.MAX_SECONDS:
            flow.add("다시 계산", "중단", f"{agent_action.MAX_SECONDS}초 넘음 → 처음 답 그대로")
            attempt["how"], attempt["computed"] = "timeout", False
            return
        done.add(action.name)
        tried += 1
        if action.name in agent_action.LLM_ACTIONS:
            llm_tried += 1
        try:
            again, how = await _retry(action, res, verdict, state, asked, user_id, done)
        except Exception:
            logger.exception("답 판정 뒤 다시 계산 실패 → 처음 답 그대로")
            attempt["error"] = True
            record.setdefault("retry", {"error": True})
            return
        attempt.update(how=how, computed=again is not None)
        if again is not None:  # 버려도 무엇이었는지 · 판정이 무엇을 봤는지 남긴다
            attempt["answer"] = lead(again.get("answer") or "")[:200]
            attempt["summary"] = answer_judge.describe(again.get("result"), again.get("parsed"))
        if len(attempts) == 1:
            record["retry"] = {k: v for k, v in attempt.items() if k != "action"}
        if again is None:
            flow.add("다시 계산", "실패", f"{how}: 계산하지 못함 → 다음 행동")
            continue
        if again.get("status") in refusal.TEMPLATES:
            # 다시 계산이 답변 불가 사유로 끝남: 판정 없이 그 사유로 바꾼다 (설계서 4절 B-1 예외)
            flow.add(
                "답변 불가 사유", again["status"], f"{how}: {refusal.NAMES.get(again['status'])}"
            )
            second = None
        else:
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
) -> dict:
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
        _found_now.set(None)
        res, sources, notice = await _turn(state, question, selections, user_id)
        await _attach_tables(res, state, question, user_id)
        await refusal.finalize(res, question)
        await _judged(res, state, question, user_id)
        if "summary" not in res:  # 직전 답을 다시 쓴 응답(_reuse)은 그 요약을 이미 들고 있다
            res["summary"] = _summary(res.get("result"))
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


async def _save(
    sid: str, after: dict, res: dict, before: dict | None, question: str, user_id
) -> None:
    try:
        await save(sid, after)
        await log_turn(sid, user_id, after.get("turn_no") or 1, question, res, before, after)
        if user_id is not None:
            await conversations.touch(sid, user_id, question)
    except (SQLAlchemyError, OSError):
        logger.exception("대화 상태 저장 실패")


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


async def save(session_id: str, state: dict) -> None:
    from app.core.db import engine
    from app.services import dialog

    async with engine.begin() as conn:
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
) -> None:
    from app.core.db import engine

    result = res.get("result") or {}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "insert into app.chat_turns (session_id, user_id, turn_no, message, request_type,"
                " status, conditions_before, conditions_after, sources, tbl_id, recomputed, answer,"
                " response, flow)"
                " values (:session_id, :user_id, :turn_no, :message, :request_type, :status,"
                " cast(:before as jsonb), cast(:after as jsonb), cast(:sources as jsonb),"
                " :tbl_id, :recomputed, :answer, cast(:response as jsonb), cast(:flow as jsonb))"
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
        await conn.execute(
            text("delete from app.chat_turns where created_at < :cutoff"),
            {"cutoff": now() - TURNS_FOR},
        )
