"""수치 답변(/ask)의 대화 이어 가기 (멀티턴)

HCX는 호출할 때마다 앞 대화를 기억하지 못한다. 그래서 대화 상태를 세션에 구조화해 두고,
새 말이 오면 "무엇이 바뀌는지"만 HCX에 묻는다. 상태를 고치고 다시 계산하는 일은 코드가 한다.
대화 전체를 HCX에 넘기지 않는다: 길어지면 예전 조건과 지금 조건이 섞이고, 앞에서 나온 숫자를
HCX가 다시 지어낼 수 있다. 숫자는 매 턴 DB에서 다시 계산한다.

세션 상태 (app.chat_sessions, "ask:{id}", 마지막 대화 뒤 SESSION_FOR 동안)
  conditions  지금 조건. 질문 해석 결과(qa.understand의 parsed) 그대로: 지표, 지역, 기간, 성별,
              보는 방식(type), 순위 개수·방향
  sources     조건의 출처: user(사용자가 말함) / inherited(앞 질문에서 이어받음) / default(기본값)
  last        직전 답 (문장, 출처, 차트). 차트 바꾸기·요약은 다시 계산하지 않고 이것으로 답한다
  pending     되묻는 중이면 무엇을 물었는지와 원래 질문 (연도 / 지역 / 통계표 고르기)
  search_session  통계표 찾기(dialog.py)의 대화 id. 통계표 선택지를 물었을 때 그 답을 이어 간다
  recent      최근 사용자 말 (기록용)

한 턴의 흐름 (handle)
  1. 되묻는 중이면 → 이번 말이 그 답인지 규칙으로 본다 (연도 / 지역 후보). 답이면 이어서 처리
  2. 이어 갈 조건이 없으면 → 새 질문 (지금까지의 해석 경로 qa.ask 그대로)
  3. "막대그래프로"·"요약해줘" → 규칙으로 알아보고 HCX·계산 없이 직전 답으로 답한다
  4. 그 밖 → HCX에 바뀐 것만 묻는다 (FOLLOW_SYSTEM)
       change   조건을 고쳐 다시 계산 (qa.answer). HCX 호출은 이 한 번
       chart / summary   3과 같다
       new      새 질문으로 해석한다 (qa.ask). 말하지 않은 지역·기간은 앞 조건에서 이어받는다
       unclear  추측하지 않고 되묻는다
  5. 답에 통계표를 붙인다 (table_guide.py): 수치로 답했으면 그 표의 카드,
     수치로 답할 수 없는 질문이면 통계표 찾기 결과 (표 카드·선택지)
  6. 상태를 저장하고 턴을 기록한다 (app.chat_turns, TURNS_FOR 동안)

이어받기: 지역은 이어받는다. 기간은 보는 방식(type)이 같을 때만 이어받는다
(추이를 보다 값 하나를 물으면 최신 연도). 성별은 이어받지 않는다. 이어받은 조건은 답 위에 알린다.
이어받은 기간으로 답할 수 없으면 기간을 빼고 다시 답한다 (사용자가 직접 말한 조건은 바꾸지 않는다).

판단(apply_changes, inherit, pick_region 등)은 DB를 모르는 함수로 두고,
저장(load, save, log_turn)만 DB를 쓴다. 저장이 실패해도 답은 한다 (이어 가기만 안 된다).
"""

import asyncio
import copy
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.prompts.qa import FOLLOW_SYSTEM
from app.services import ask_followup, charts, indicators, qa, table_guide, timeref
from app.services.hcx_client import HcxClient

logger = logging.getLogger(__name__)

SESSION_FOR = timedelta(hours=1)  # 마지막 대화 뒤 이 시간이 지나면 조건을 이어 가지 않는다
TURNS_FOR = timedelta(days=30)  # 턴 기록 보관 기간
RECENT = 10  # 세션에 남기는 최근 사용자 말 수
REPLY_MAX_CHARS = 25  # 되묻기 답은 이보다 짧다 (길면 새 질문)
FOLLOW_MAX_TOKENS = 512  # 변경 추출 응답의 최대 길이

NEW, CHANGE, CHART, SUMMARY, REPLY, UNCLEAR = (
    "new",
    "change",
    "chart",
    "summary",
    "reply",
    "unclear",
)
TABLES = "tables"  # 수치 없이 통계표만 안내한 답의 status
TYPES = {"value", "trend", "compare", "rank", "composition"}
# 조건이 의미 있는 답 (이 뒤의 말은 조건 변경일 수 있다). 통계 밖 질문·해석 실패는 이어 가지 않는다
NO_CONDITIONS = {"other", "parse_error", None}

# 그림만 바꿔 달라는 말: HCX 없이 알아본다
CHART_WORDS = {
    "table": re.compile(r"표\s*로|표\s*형태|표로\s*정리"),
    "bar": re.compile(r"막대"),
    "line": re.compile(r"꺾은\s*선|선\s*그래프|라인"),
}
CHART_REQUEST = re.compile(r"(보여|바꿔|그려|볼래|보고|줘|주세요|로$|으로$)")
SUMMARY_REQUEST = re.compile(r"^\s*(한\s*줄로\s*|짧게\s*|간단히\s*)?(요약|정리)(해|좀|\s|$)")
SEX_WORDS = re.compile(r"남자|여자|남성|여성|남녀|성별")

LABELS = {"indicator": "지표", "regions": "지역", "period": "기간", "sex": "성별"}
SOURCE_WORDS = {"inherited": "이어받음", "default": "기본값"}

_locks: dict[str, asyncio.Lock] = {}


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return f"A_{uuid4().hex[:12]}"


def _lock(session_id: str) -> asyncio.Lock:
    """같은 대화의 요청이 동시에 오면(버튼 두 번, 탭 두 개) 차례로 처리한다.
    서버 프로세스가 하나라 메모리 잠금으로 충분하다 (login_limit.py와 같은 전제)"""
    if len(_locks) > 1000:
        for key in [k for k, lock in _locks.items() if not lock.locked()]:
            _locks.pop(key, None)
    return _locks.setdefault(session_id, asyncio.Lock())


# ---- 조건의 출처와 표시 (DB 없이 동작) ----------------------------------------------------


def has_period(question: str) -> bool:
    """질문이 연도·기간을 말했는가 ("2024년", "최근 5년", "작년", "과거부터")"""
    return bool(
        ask_followup.has_explicit_year(question)
        or timeref.RECENT_N_YEARS.search(question)
        or timeref.N_YEARS_AGO.search(question)
        or ask_followup.FROM_PAST.search(question)
        or any(word in question for word, _ in timeref.WORDS)
    )


def sources_of(question: str, parsed: dict) -> dict[str, str]:
    """새 질문의 조건이 어디서 왔는지: 사용자가 말했으면 user, 아니면 default"""
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
    if c.get("type") == "trend" and start and end:
        return f"{start}~{end}년" if start != end else f"{start}년"
    return f"{c['year']}년" if c.get("year") else None


def applied(conditions: dict, sources: dict[str, str]) -> list[dict[str, str]]:
    """화면에 보여 줄 적용한 조건: [{name, value, source}]"""
    if conditions.get("type") in NO_CONDITIONS:
        return []
    regions = conditions.get("regions") or []
    if not regions and conditions.get("type") == "rank":
        regions = [f"{conditions.get('sido') or '전국'} {conditions.get('level') or '시도'}별"]
    values = {
        "indicator": conditions.get("indicator"),
        "regions": ", ".join(regions) or None,
        "period": period_text(conditions),
        "sex": conditions.get("sex"),
    }
    return [
        {"name": LABELS[key], "value": str(value), "source": sources.get(key, "default")}
        for key, value in values.items()
        if value
    ]


def effective(parsed: dict, result: dict) -> dict:
    """세션에 둘 조건: 해석 결과에서 기간을 실제로 답한 기간으로 맞춘다.
    "최근 5년"·"과거부터"는 자료의 마지막·첫 연도를 보고 계산할 때 정해진다. 그 기간을 적어 두어야
    화면에 보이는 조건이 답과 같고, 다음 턴("서울도 같이")도 같은 기간으로 이어진다"""
    c = {k: v for k, v in parsed.items() if k not in ("corrections", "raw")}
    series = next((r["series"] for r in qa._leaves(result) if r.get("series")), None)
    if result.get("status") == "ok" and series:
        c["start_year"], c["end_year"] = series[0]["year"], series[-1]["year"]
        c["year"] = c["end_year"]
        c.pop("asked", None)  # 기간 표현("최근 5년")은 연도로 바꿔 적었다
    elif result.get("status") == "ok" and result.get("year"):
        c["year"] = result["year"]
    return c


def inherited_notice(conditions: dict, sources: dict[str, str]) -> str | None:
    """이어받은 조건이 있으면 답 위에 알릴 문장"""
    parts = [
        f"{row['name']} {row['value']}"
        for row in applied(conditions, sources)
        if row["source"] == "inherited"
    ]
    return f"앞 질문의 조건을 이어받았습니다: {', '.join(parts)}" if parts else None


# ---- 조건 고치기 (DB 없이 동작) ------------------------------------------------------------


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


def apply_changes(conditions: dict, changes: dict) -> tuple[dict, set[str]]:
    """HCX가 뽑은 변경을 조건에 적용한다 → (새 조건, 바뀐 항목: indicator·regions·period·sex·view).
    말하지 않은 항목은 그대로 둔다. 모르는 항목·값은 버린다
    (HCX가 틀리게 적어도 조건이 깨지지 않게).
    지역 수나 기간이 바뀌어 보는 방식(type)이 맞지 않게 되면 맞춘다 (_retype)"""
    c = copy.deepcopy(conditions)
    changed: set[str] = set()

    if name := (changes.get("indicator") or "").strip():
        c["indicator"] = indicators.canonical_name(name) or name
        c["term"] = name
        # 앞 지표에만 맞던 세부 조건은 버린다
        for key in ("ages", "age_group", "unsupported", "share_of", "aggregate", "group", "by"):
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
        c.pop("asked", None)  # "최근 5년"·"과거부터" 같은 앞 질문의 기간 표현은 더는 맞지 않다

    if changes.get("sex") in ("계", "남", "여"):
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
    return c, changed


def _retype(c: dict, changed: set[str]) -> None:
    """지역 수·기간이 바뀐 뒤 보는 방식을 맞춘다 (사용자가 방식을 직접 말했으면 부르지 않는다)
      기간               → 추이 (지역이 여럿이면 지역마다 추이)
      지역 둘 이상 + 한 해 → 지역 비교
      지역 하나 + 한 해   → 값 하나
    순위에서 순위 조건(개수·방향)만 바꾸면 순위로 둔다. 구성비는 그대로 둔다"""
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
        c["type"] = "rank"  # 지역을 모두 뺐다 → 지역별 순위


# 대화의 연도를 기준으로 한 시점: 지금 보는 연도의 앞·뒤 해 ("그 전년도는?", "그 다음 해는?")
CONTEXT_YEAR = [
    (re.compile(r"전년도|전년|(?:그|직)\s*전\s*해|이전\s*해|직전\s*연도|한\s*해\s*전"), -1),
    (re.compile(r"다음\s*해|이듬해|다음\s*연도|익년|한\s*해\s*(?:뒤|후)"), 1),
]
FULL_YEAR = re.compile(r"(?:19|20)\d{2}\s*년?")


def follow_year(question: str, conditions: dict, today: date | None = None) -> int | None:
    """이어 묻는 말의 상대 시점 → 연도 (HCX는 오늘 날짜를 모르고 기준도 헷갈린다).
      "그 전년도", "다음 해"   지금 보는 연도 기준 (2020년을 보다가 "그 전년도는?" → 2019)
      "작년", "올해", "3년 전" 오늘 기준 (timeref, 2026년이면 "작년" → 2025)
    "전년 대비"처럼 비교 기준으로 쓴 말, 연도를 숫자로 말한 경우, "작년부터"처럼 기간이면 None"""
    q = timeref.COMPARE_BASE.sub(" ", question)
    if FULL_YEAR.search(q):
        return None
    base = conditions.get("end_year") or conditions.get("year")
    for pattern, step in CONTEXT_YEAR:
        if pattern.search(q):
            return int(base) + step if base else None
    ref = timeref.resolve(q, today)
    return ref.year if ref and not ref.open_end else None


# 지표가 아니라 보는 방식을 가리키는 말.
# HCX가 이런 말을 지표(term)로 적어도 지표를 말한 것으로 보지 않는다
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
    """새 질문이 지표를 말했는가.
    질문에 지원 지표의 이름·별칭이 있거나, 질문에 있는 말을 지표로 적었으면 말한 것.
    "2015년부터 추이로 보여줘"처럼 기간·방식만 말하면 말하지 않은 것 (앞 지표를 이어받는다).
    HCX가 지표 없는 질문에 총인구를 채워 넣기도 해서, 지표 이름이 아니라 질문의 말을 본다"""
    if indicators.find_in_text(question):
        return True
    compact = question.replace(" ", "")
    words = [parsed.get("term"), parsed.get("indicator")]
    return any(w and w.strip() not in VIEW_WORDS and w.replace(" ", "") in compact for w in words)


def inherit(
    parsed: dict, sources: dict, prev: dict, prev_sources: dict, question: str = ""
) -> tuple[dict, dict]:
    """새 질문이 말하지 않은 조건을 앞 조건에서 이어받는다 → (조건, 출처).
    지표: 질문에 지표가 없으면 이어받는다 ("2015년부터 추이로 보여줘")
    지역: 지역이 필요한 질문인데 말하지 않았으면 이어받는다
          (순위는 지역 없이 묻는 것이 보통이라 제외)
    기간: 보는 방식이 같고, 앞에서 기간을 말했을 때만 이어받는다
    성별: 이어받지 않는다"""
    p, s = copy.deepcopy(parsed), dict(sources)
    kind = p.get("type")
    if kind in NO_CONDITIONS or prev.get("type") in NO_CONDITIONS:
        return p, s
    if question and prev.get("indicator") and not names_indicator(question, p):
        for key in ("indicator", "term", "ages", "age_group", "by"):
            if prev.get(key) is not None:
                p[key] = copy.deepcopy(prev[key])
        p.pop("unsupported", None)
        s["indicator"] = "inherited"
    if s.get("regions") == "default" and kind != "rank" and prev.get("regions"):
        p["regions"] = list(prev["regions"])
        s["regions"] = "inherited"
    if (
        s.get("period") == "default"
        and prev_sources.get("period") in ("user", "inherited")
        and kind == prev.get("type")
    ):
        for key in ("year", "start_year", "end_year"):
            p[key] = prev.get(key)
        s["period"] = "inherited"
    return p, s


def without_inherited_period(
    parsed: dict, original: dict, sources: dict
) -> tuple[dict, dict] | None:
    """이어받은 기간으로 답하지 못했을 때: 기간을 이어받기 전(해석 결과)으로 되돌린 조건.
    이어받은 기간이 없으면 None"""
    if sources.get("period") != "inherited":
        return None
    p = copy.deepcopy(parsed)
    for key in ("year", "start_year", "end_year"):
        p[key] = original.get(key)
    return p, {**sources, "period": "default"}


# ---- 되묻기 답과 규칙으로 알아보는 요청 (DB 없이 동작) ---------------------------------------


def pending_of(result: dict, question: str) -> dict | None:
    """이번 답이 되묻기면 다음 말을 그 답으로 읽을 수 있게 남길 것"""
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


def pick_region(reply: str, options: list[str]) -> str | None:
    """지역 되묻기의 답 → 고른 후보. "광주광역시", "경기도 광주시요", "2번". 못 고르면 None"""
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
    """되물었던 지역 표현을 고른 지역으로 바꾼다 (다른 지역은 그대로)"""
    c = copy.deepcopy(conditions)
    regions = list(c.get("regions") or [])
    if asked in regions:
        regions[regions.index(asked)] = picked
    else:
        regions = [picked]
    c["regions"] = regions
    return c


def chart_request(question: str) -> str | None:
    """ "막대그래프로 보여줘", "표로" → 보기 방식(line / bar / table). 그림 요청이 아니면 None.
    지표나 연도를 함께 말하면("부산 인구 막대그래프로") 새 질문일 수 있어 HCX에 맡긴다"""
    q = question.strip()
    if len(q) > REPLY_MAX_CHARS or ask_followup.has_explicit_year(q) or indicators.find_in_text(q):
        return None
    for view, pattern in CHART_WORDS.items():
        if pattern.search(q) and CHART_REQUEST.search(q):
            return view
    return None


def summary_request(question: str) -> bool:
    return len(question.strip()) <= REPLY_MAX_CHARS and bool(SUMMARY_REQUEST.search(question))


def chart_with_view(chart: dict | None, view: str) -> dict | None:
    """직전 차트를 다른 모양으로. 연도별 값만 꺾은선↔막대를 오갈 수 있고, 표는 모두 된다.
    바꿀 수 없으면 None (구성비를 꺾은선으로 등)"""
    if not chart:
        return None
    if view == "table" or view == chart["type"] or (chart["type"] == "line" and view == "bar"):
        return {**chart, "view": view}
    return None


def lead(answer: str) -> str:
    """답변의 첫 문단 (핵심 문장). 요약 요청에는 이 문장을 다시 준다: 새 숫자를 만들지 않는다"""
    return answer.split("\n\n")[0].strip()


def state_text(conditions: dict, sources: dict) -> str:
    """HCX에 보여 줄 지금 조건 (FOLLOW_SYSTEM의 {STATE})"""
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
    """HCX에 보여 줄 직전 답 ({LAST}): 핵심 문장과 차트의 행 이름·값 ("거기", "1위"를 풀 때 쓴다).
    HCX는 변경 JSON만 내므로 이 숫자가 사용자에게 다시 나가지는 않는다"""
    if not last:
        return "(없음)"
    lines = [lead(last.get("answer") or "")]
    rows = (last.get("chart") or {}).get("rows") or []
    lines += [f"{i}. {r['label']}: {r['value']}" for i, r in enumerate(rows[:10], start=1)]
    return "\n".join(line for line in lines if line)


def parse_delta(raw: str) -> dict:
    """HCX 출력 → {request_type, changes, chart, unclear}. 읽지 못하면 새 질문으로 본다
    (새 질문 경로는 지금까지의 해석 그대로라 가장 안전하다)"""
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
    """새 말이 지금 조건을 어떻게 바꾸는지 HCX에 묻는다"""
    system = (
        FOLLOW_SYSTEM.replace("{STATE}", state_text(state["conditions"], state["sources"]))
        .replace("{LAST}", last_text(state.get("last")))
        .replace("{INDICATORS}", ", ".join(indicators.specs()))
    )
    raw = await _chat(question, system)
    trace["follow_raw"] = raw
    return parse_delta(raw)


async def _chat(prompt: str, system: str) -> str:
    """변경 추출용 HCX 호출. 짧은 JSON만 받으므로 추론을 끄고 응답 길이를 줄인다.
    HCX 한도(분당 6만 토큰)는 요청마다 응답 최대 길이만큼을 미리 잡는다. 질문 해석과 같은 설정
    (5,120토큰)으로 턴마다 부르면 대화 몇 번에 한도가 찬다"""
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


# ---- 한 턴 처리 ----------------------------------------------------------------------------


def _reuse(state: dict, answer: str, chart: dict | None, request_type: str) -> dict:
    """직전 답을 다시 쓰는 응답 (차트 바꾸기·요약). 계산하지 않는다"""
    last = state["last"]
    return {
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


async def _answer(conditions: dict, trace: dict | None = None) -> dict:
    res = await qa.answer(conditions, trace if trace is not None else {})
    res["chart"] = charts.build(res.get("result"))
    return res


async def _new(question: str, state: dict | None) -> tuple[dict, dict, str | None]:
    """새 질문: 지금까지의 해석 경로 그대로. 앞 조건이 있으면 말하지 않은 것을 이어받는다
    → (응답, 출처, 알림)"""
    kept: dict[str, Any] = {}
    prev = state["conditions"] if state and state.get("conditions") else None

    def adjust(parsed: dict) -> dict:
        kept["original"] = parsed
        kept["sources"] = sources_of(question, parsed)
        if prev:
            parsed, kept["sources"] = inherit(
                parsed, kept["sources"], prev, state["sources"], question
            )
        return parsed

    res = await qa.ask(question, adjust=adjust)
    sources = kept.get("sources") or sources_of(question, res.get("parsed") or {})
    notice = inherited_notice(res.get("parsed") or {}, sources)
    if res.get("status") != "ok" and (
        retry := without_inherited_period(
            res.get("parsed") or {}, kept.get("original") or {}, sources
        )
    ):
        # 이어받은 기간으로는 답할 수 없다 → 기간을 빼고 다시 답한다 (지역은 그대로)
        again = await qa.answer(retry[0], res.get("trace"), question)
        if again.get("status") == "ok":
            res, sources = again, retry[1]
            notice = " ".join(
                filter(
                    None,
                    [
                        inherited_notice(res["parsed"], sources),
                        "앞 질문의 기간으로는 자료가 없어 기본 기간으로 답했습니다.",
                    ],
                )
            )
    res["chart"] = charts.build(res.get("result"))
    res["request_type"] = NEW
    return res, sources, notice


async def _turn(
    state: dict | None, question: str, selections: dict | None = None, user_id: int | None = None
) -> tuple[dict, dict, str | None]:
    """이번 말을 처리한다 → (응답, 조건의 출처, 알림)"""
    pending = (state or {}).get("pending")
    if (state or {}).get("search_session") and (
        selections or (pending or {}).get("missing") == "table"
    ):
        # 통계표 선택지(대상·성별·판, 비슷한 표 여럿)에 대한 답: 통계표 찾기가 이어서 처리한다.
        # 선택지와 맞지 않는 말이면 dialog가 새 검색으로 처리하므로 그 결과를 그대로 쓴다
        found = await table_guide.find_tables(
            question, state["search_session"], selections, user_id
        )
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

    usable = bool(state and (state.get("conditions") or {}).get("type") not in NO_CONDITIONS)
    if not usable:
        return await _new(question, None)

    has_last = bool((state.get("last") or {}).get("answer"))
    view = chart_request(question) if has_last else None
    delta = None
    if view is None and not (has_last and summary_request(question)):
        trace: dict = {}
        delta = await extract(state, question, trace)
        year = follow_year(question, state["conditions"])
        if year and not names_indicator(question, {}):
            # 상대 시점은 HCX가 아니라 코드가 푼다 ("작년은?"을 2020년의 다음 해로 읽던 문제)
            changes = {
                k: v
                for k, v in (delta.get("changes") or {}).items()
                if k not in ("year", "start_year", "end_year")
            }
            delta.update(request_type=CHANGE, unclear=None, changes={**changes, "year": year})
        if delta["unclear"]:
            return _unclear(state, delta["unclear"]), state["sources"], None
        if delta["request_type"] == CHANGE:
            conditions, changed = apply_changes(state["conditions"], delta["changes"])
            if changed:
                res = await _answer(conditions, trace)
                res["request_type"] = CHANGE
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
            _reuse(state, lead(last["answer"]), last.get("chart"), SUMMARY),
            state["sources"],
            None,
        )

    return await _new(question, state)


def next_state(state: dict | None, question: str, res: dict, sources: dict) -> dict:
    """이번 답 뒤의 세션 상태"""
    prev = state or {}
    recent = [*(prev.get("recent") or []), question][-RECENT:]
    turn_no = (prev.get("turn_no") or 0) + 1
    if res.get("request_type") in (CHART, SUMMARY):
        last = {**prev["last"], "chart": res.get("chart") or prev["last"].get("chart")}
        return {**prev, "last": last, "recent": recent, "turn_no": turn_no}
    if res.get("request_type") == UNCLEAR:
        return {**prev, "recent": recent, "turn_no": turn_no}
    if res.get("status") == TABLES:
        # 수치 없이 표만 안내했다: 수치 조건은 이어 가지 않는다 (다음 "서울도 같이"는 새 질문).
        # 표 선택지를 물었으면 다음 말을 그 답으로 읽는다
        found = res.get("tables")
        return {
            "conditions": {},
            "sources": {},
            "last": {
                "status": TABLES,
                "answer": res.get("answer") or "",
                "sources": [],
                "chart": None,
            },
            "pending": {"missing": "table"} if table_guide.waiting_choice(found) else None,
            "search_session": res.get("search_session") or prev.get("search_session"),
            "recent": recent,
            "turn_no": turn_no,
        }
    asked = res.get("question") or question
    return {
        "conditions": effective(res.get("parsed") or {}, res.get("result") or {}),
        "sources": sources,
        "last": {
            "status": res.get("status"),
            "answer": res.get("answer") or "",
            "sources": res.get("sources") or [],
            "chart": res.get("chart"),
        },
        "pending": pending_of(res.get("result") or {}, asked),
        "search_session": prev.get("search_session"),
        "recent": recent,
        "turn_no": turn_no,
    }


async def _attach_tables(res: dict, state: dict | None, question: str, user_id: int | None) -> None:
    """답에 통계표를 붙인다 (table_guide.py). 표 찾기가 실패해도 수치 답은 그대로 준다"""
    if res.get("tables") is not None or res.get("request_type") in (CHART, SUMMARY, UNCLEAR):
        return
    asked = res.get("question") or question
    if res.get("status") in table_guide.SEARCH_INSTEAD:
        sid = (state or {}).get("search_session") or table_guide.new_search_session()
        try:
            found = await table_guide.find_tables(asked, sid, None, user_id)
        except Exception:
            logger.exception("통계표 찾기 실패")
            return
        kind, trace, parsed = res.get("request_type"), res.get("trace"), res.get("parsed")
        res.clear()
        res.update(table_guide.tables_answer(found, {"parsed": parsed or {}}))
        # 수치 답을 하려다 표 찾기로 넘어온 경위(질문 해석)는 요청 기록에 남긴다
        res["request_type"], res["question"], res["trace"] = kind, asked, trace or {}
        res["search_session"] = sid
        return
    try:
        cards = await table_guide.cards_for(asked, res.get("sources") or [])
    except Exception:
        logger.exception("통계표 카드 만들기 실패")
        cards = []
    if cards:
        res["tables"] = {"status": "ok", "recommendations": cards}


async def handle(
    session_id: str | None,
    question: str,
    user_id: int | None = None,
    selections: dict | None = None,
) -> dict:
    """/ask 한 턴. qa.ask의 응답에 더하는 것:
    session_id, question, chart, request_type, applied(적용한 조건), notice(알림)"""
    sid = session_id or new_id()
    async with _lock(sid):
        state, expired = await _load(sid)
        res, sources, notice = await _turn(state, question, selections, user_id)
        await _attach_tables(res, state, question, user_id)
        if expired:
            notice = " ".join(filter(None, ["시간이 지나 이전 조건이 초기화됐습니다.", notice]))
        after = next_state(state, question, res, sources)
        res.setdefault("question", question)
        res["session_id"] = sid
        res["applied"] = applied(after.get("conditions") or {}, after.get("sources") or {})
        res["notice"] = notice
        await _save(sid, after, res, state, question, user_id)
    return res


# ---- 저장 (app.chat_sessions, app.chat_turns) ---------------------------------------------


def _key(session_id: str) -> str:
    return f"ask:{session_id}"


async def _load(session_id: str) -> tuple[dict | None, bool]:
    """(세션 상태, 만료됐는가). 읽지 못하면 상태 없이 답한다"""
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
    except (SQLAlchemyError, OSError):
        logger.exception("대화 상태 저장 실패")


async def load(session_id: str, at: datetime) -> tuple[dict | None, bool]:
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
    if at - row.updated_at > SESSION_FOR:
        return None, True
    return row.slots, False


async def save(session_id: str, state: dict) -> None:
    """세션 상태를 남긴다. 만료된 지 하루가 지난 수치 대화는 이때 지운다"""
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
                "delete from app.chat_sessions"
                " where session_id like 'ask:%' and updated_at < now() - interval '1 day'"
            )
        )


async def log_turn(
    session_id: str,
    user_id: int | None,
    turn_no: int,
    question: str,
    res: dict,
    before: dict | None,
    after: dict,
) -> None:
    """턴 기록 (app.chat_turns): 무엇을 요청했고 조건이 어떻게 바뀌었는지.
    디버깅과 멀티턴 채점(유지할 조건·바뀔 조건·다시 계산했는지)에 쓴다.
    TURNS_FOR이 지난 기록은 지운다"""
    from app.core.db import engine

    result = res.get("result") or {}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "insert into app.chat_turns (session_id, user_id, turn_no, message, request_type,"
                " status, conditions_before, conditions_after, sources, tbl_id, recomputed, answer)"
                " values (:session_id, :user_id, :turn_no, :message, :request_type, :status,"
                " cast(:before as jsonb), cast(:after as jsonb), cast(:sources as jsonb),"
                " :tbl_id, :recomputed, :answer)"
            ),
            {
                "session_id": session_id,
                "user_id": user_id,
                "turn_no": turn_no,
                "message": question,
                "request_type": res.get("request_type"),
                "status": res.get("status"),
                "before": json.dumps(
                    (before or {}).get("conditions"), ensure_ascii=False, default=str
                ),
                "after": json.dumps(after.get("conditions"), ensure_ascii=False, default=str),
                "sources": json.dumps(after.get("sources"), ensure_ascii=False),
                "tbl_id": result.get("tbl_id"),
                "recomputed": not result.get("reused") and res.get("request_type") != UNCLEAR,
                "answer": lead(res.get("answer") or ""),
            },
        )
        await conn.execute(
            text("delete from app.chat_turns where created_at < :cutoff"),
            {"cutoff": now() - TURNS_FOR},
        )
