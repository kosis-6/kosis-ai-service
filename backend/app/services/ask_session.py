"""수치 답변(/ask)의 대화 이어 가기 (멀티턴)

HCX는 호출할 때마다 앞 대화를 기억하지 못한다. 그래서 대화 상태를 세션에 구조화해 두고,
새 말이 오면 "무엇이 바뀌는지"만 HCX에 묻는다. 상태를 고치고 다시 계산하는 일은 코드가 한다.
대화 전체를 HCX에 넘기지 않는다: 길어지면 예전 조건과 지금 조건이 섞이고, 앞에서 나온 숫자를
HCX가 다시 지어낼 수 있다. 숫자는 매 턴 DB에서 다시 계산한다.

세션 상태 (app.chat_sessions, "ask:{id}". 마지막 대화 뒤 손님은 SESSION_FOR,
로그인 유저는 TURNS_FOR 동안 이어 간다.
서버 메모리(memory_cache)에 사본을 두어 턴마다 DB를 읽지 않는다)
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
  6. 상태를 저장하고 턴을 기록한다 (app.chat_turns, TURNS_FOR 동안. 화면에 보여 준 답 전체도 남긴다)
     로그인 유저면 대화 목록(app.conversations)에도 남긴다 → 나중에 다시 열어 이어 간다
     (conversations.py, /conversations API)

이어받기: 지역은 이어받는다. 기간은 보는 방식(type)이 같을 때만 이어받는다
(추이를 보다 값 하나를 물으면 최신 연도). 성별은 이어받지 않는다. 이어받은 조건은 답 위에 알린다.
이어받은 조건으로 답할 수 없으면 기간·지역·둘 다를 차례로 빼고 다시 답한다.
사용자가 직접 말한 조건은 바꾸지 않는다.

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
from starlette.concurrency import run_in_threadpool

from app.prompts.qa import FOLLOW_SYSTEM
from app.services import (
    answer_summary,
    ask_followup,
    charts,
    conversations,
    cross_indicator,
    dynamic_indicator,
    flow,
    indicators,
    llm_calls,
    qa,
    question_clarify,
    table_guide,
    timeref,
    understand_merged,
)
from app.services.hcx_client import HcxClient
from app.services.josa import josa
from app.services.log_redaction import mask, redact
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

SESSION_FOR = timedelta(hours=1)  # 손님: 마지막 대화 뒤 이 시간이 지나면 조건을 이어 가지 않는다
TURNS_FOR = timedelta(days=30)  # 턴 기록 보관 기간. 로그인 유저의 대화는 이 동안 이어 간다
# 대화 상태의 서버 메모리 사본 {session_id: (상태, 마지막 저장 시각)}. DB가 원본이다
STATE_CACHE = MemoryCache("chat_state", maxsize=2048, ttl=3600)
RECENT = 10  # 세션에 남기는 최근 사용자 말 수
REPLY_MAX_CHARS = 25  # 되묻기 답은 이보다 짧다 (길면 새 질문)
FOLLOW_MAX_TOKENS = 512  # 변경 추출 응답의 최대 길이

DETAIL = "detail"  # 직전 답을 자세히 (문장 + 수치 목록 그대로)
SOURCE = "source"  # 직전 답의 출처·계산식
CHAT = "chat"  # 인사·감사 (통계 질문이 아님)
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
# 요약만 해 달라는 말. "요약 통계 보여줘", "정리해고 통계"처럼 뒤에 다른 말이 오면 질문이다
SUMMARY_REQUEST = re.compile(
    r"^\s*((한|두|세)\s*줄로|짧게|간단히|간단하게|간략히)?\s*(요약|정리)\s*"
    r"(좀\s*)?(해\s*(줘요?|주세요|줄래요?|봐요?|주라)?|부탁\s*(해요?|합니다|드려요)?)?\s*[.!?~]*$"
)
SEX_WORDS = re.compile(r"남자|여자|남성|여성|남녀|성별")

LABELS = {"indicator": "지표", "regions": "지역", "period": "기간", "sex": "성별"}
SOURCE_WORDS = {"inherited": "이어받음", "default": "기본값"}

_locks: dict[str, asyncio.Lock] = {}


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return f"A_{uuid4().hex[:12]}"


def same_owner(state: dict, user_id: int | None) -> bool:
    """이 사용자가 이어 가도 되는 대화인가.
    로그인한 사용자가 시작한 대화는 그 사용자만 이어 간다
    (대화 id 를 알아도 남의 조건·답을 볼 수 없다).
    손님이 시작한 대화는 손님도, 도중에 로그인한 사용자도 이어 간다
    (그 뒤로는 그 사용자의 대화다)"""
    if "user_id" not in state:
        return False  # 소유자를 저장하기 전의 대화는 손님 대화로 간주하지 않는다.
    owner = state["user_id"]
    return owner is None or owner == user_id


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
        "indicator": ", ".join(conditions.get("indicators") or []) or conditions.get("indicator"),
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
    if sex_not_split(parsed, result):
        c["sex"] = "계"  # 화면의 조건도 실제로 답한 대로
    return c


def sex_not_split(parsed: dict, result: dict) -> bool:
    """남자·여자만 보자고 했는데 성별로 나뉜 자료가 없어 전체 값으로 답했는가"""
    leaves = qa._leaves(result) if result.get("status") == "ok" else []
    return (
        parsed.get("sex") in ("남", "여")
        and bool(leaves)
        and all(r.get("sex") == "계" for r in leaves)
    )


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
        for key in (
            "ages",
            "age_group",
            "unsupported",
            "share_of",
            "aggregate",
            "group",
            "by",
            "indicators",
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
FULL_YEAR = re.compile(r"(?:19|20)\d{2}(?!\d|\s*세대)\s*년?")  # "2030세대"는 연도가 아니다


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


def complete_question(question: str) -> bool:
    """지표·지역·기간을 모두 말하면 변경 추출 없이 새 질문으로 해석한다.

    추가·제외·비교처럼 직전 조건을 참조하는 말은 변경 추출에 남긴다.
    "막대그래프로" 같은 표현이 들어 있어도 지표와 조건을 말했으면 수치 질문이다.
    """
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
    if (
        question
        and kind == "value"
        and prev.get("type") in ("trend", "compare", "rank")
        and s.get("regions") == "default"
        and s.get("period") == "default"
        and not VIEW_ASK.search(question)
        and names_indicator(question, parsed)
    ):
        # "사망자 수는?"처럼 지표만 말했다: 보는 방식(추이·순위)과 기간도 그대로 이어받는다.
        # 전에는 값 하나로 읽어, 두 지역 추이 뒤에 물으면 첫 지역의 한 해 값만 답했다
        for key in KEEP_VIEW:
            p[key] = copy.deepcopy(prev.get(key))
        kind = p.get("type")
        if prev_sources.get("period") in ("user", "inherited"):
            s["period"] = "inherited"
    if s.get("regions") == "default" and kind != "rank" and prev.get("regions"):
        p["regions"] = list(prev["regions"])
        s["regions"] = "inherited"
    if kind == "rank" and question and p.get("level") == "시군구" and not said_region(question):
        # "구별로 보여줘": 질문에 없는 시도를 HCX가 채워 넣기도 한다 (부산을 보다 물었는데 서울 구
        # 순위로 답함). 앞에서 보던 시도 안의 시군구로 본다
        p["sido"] = prev.get("sido") or sido_of(prev.get("regions") or [])
        if p["sido"]:
            s["regions"] = "inherited"
    if p.get("type") == "value" and s.get("regions") == "inherited" and len(p["regions"]) > 1:
        if p.get("aggregate") != "sum":
            p["type"] = kind = "compare"  # 값 하나는 한 지역만 본다: 이어받은 지역이 여럿이면 비교
    if (
        s.get("period") == "default"
        and prev_sources.get("period") in ("user", "inherited")
        and kind == prev.get("type")
    ):
        for key in ("year", "start_year", "end_year"):
            p[key] = prev.get(key)
        s["period"] = "inherited"
    return p, s


def said_region(question: str) -> bool:
    return bool("전국" in question or indicators.find_regions(question))


def sido_of(regions: list[str]) -> str | None:
    """보던 지역이 시도 하나면 그 이름, 아니면 None"""
    if len(regions) != 1:
        return None
    found = indicators.resolve_region(regions[0])
    return regions[0] if found.get("status") == "ok" and found.get("kind") == "시도" else None


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


def without_inherited(parsed: dict, original: dict, sources: dict):
    """이어받은 기간, 지역, 둘 다를 순서대로 되돌린 재시도 후보."""
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
    """짧다는 이유만으로 새 질문을 선택지 응답으로 처리하지 않는다."""
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


ALTERNATIVE_REPLY_MAX = 40  # 대안을 말로 고른 답 ("귀화자랑 자녀까지 다 포함한 주민으로요")
AGREE = re.compile(r"^\s*(응|네|예|그래|좋아|그걸로|그거로|그것으로|그거)\b")
REPLY_TAIL = re.compile(r"(이랑|랑|하고|까지|으로요|로요|으로|로|이요|요|도|만|을|를|은|는|이|가)$")


def alternative_by_words(reply: str, answers: list[dict]) -> str | None:
    """대안 되묻기의 답을 말로 고른 경우 → 고른 대안의 번호(문자열). 못 고르면 None.
    대안이 하나면 "응 · 그걸로"도 그 대안이다. 아니면 답의 낱말이 한 대안의 답 글에만 있을 때"""
    if len(reply.strip()) > ALTERNATIVE_REPLY_MAX or not answers:
        return None
    if len(answers) == 1 and AGREE.match(reply):
        return "0"
    words = [REPLY_TAIL.sub("", w) for w in re.findall(r"[가-힣]{2,}", reply)]
    words = [w for w in words if len(w) >= 2 and w not in {"포함", "포함해서", "기준"}]

    def text(a: dict) -> str:  # 답 글과 결과의 주석 (귀화자 · 자녀 포함 같은 기준)
        notes = " ".join(map(str, ((a.get("result") or {}).get("notes") or [])))
        return f"{a.get('answer') or ''} {notes}"

    hits = [i for i, a in enumerate(answers) if words and any(w in text(a) for w in words)]
    return str(hits[0]) if len(hits) == 1 else None


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


# 구성비로 바꿔 보자는 말. HCX는 이 말을 그림 바꾸기로 읽고 모양을 정하지 못한다
COMPOSITION_REQUEST = re.compile(r"구성비|구성\s*으로|비중\s*으로")


def composition_request(question: str) -> bool:
    q = question.strip()
    return (
        len(q) <= REPLY_MAX_CHARS
        and bool(COMPOSITION_REQUEST.search(q) and CHART_REQUEST.search(q))
        and not ask_followup.has_explicit_year(q)
        and not indicators.find_in_text(q)
    )


# 지역별로 나눠 보자는 말 ("구별로 보여줘", "시도별로"). HCX 해석이 실행마다 달라 규칙으로 읽는다
# (부산을 보다 물었는데 서울 구 순위로 답하거나 통계표 안내로 넘어갔다)
BY_GU = re.compile(r"(시군구|구|군)\s*(별|마다)")
BY_SIDO = re.compile(r"(시도|지역)\s*(별|마다)")
REGION_SPLIT_ONLY = re.compile(
    r"(?:시군구|구|군|시도|지역)\s*(?:별|마다)(?:로)?\s*"
    r"(?:(?:더\s*)?(?:자세히|자세하게|상세히|상세하게|구체적으로)\s*)?"
    r"(?:(?:보여|알려)\s*(?:줘요?|주세요|줄래요?)?)?\s*[?.!~]*"
)


def region_split_changes(question: str, conditions: dict) -> dict | None:
    """지역별로 나눠 보자는 말이면 조건에 적용할 변경, 아니면 None.
    시군구별은 보던 지역이 시도 하나일 때만 (그 시도 안의 시군구 순위)"""
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
    """답변의 첫 문단 (핵심 문장). 턴 기록과 HCX에 보여 줄 직전 답에 쓴다"""
    return answer.split("\n\n")[0].strip()


# 직전 답을 더 자세히 보여 달라는 말. HCX는 이 말을 요약 요청으로 읽는다 ("더 자세히 설명해줘")
DETAIL_REQUEST = re.compile(r"자세히|자세하게|상세히|상세하게|구체적으로|풀어서")
# 나누는 기준을 함께 말하면 직전 답이 아니라 새 답을 바라는 말이다 ("구별로 자세히 보여줘")
SPLIT_ASK = re.compile(r"나눠|나누|쪼개|구분해|별\s|별$")
# 보는 방식을 새로 말함 (VIEW_ASK와 같되 "어디서 나온 거야?"의 "어디서"는 뺀다)
NEW_VIEW = re.compile(r"추이|추세|비교|순위|몇\s*위|가장|제일|구성|별로|마다|어디(?!서)")


def about_last_answer(question: str) -> bool:
    """직전 답만 가리키는 짧은 말인가: 지표·지역·연도·성별·보는 방식을 새로 말하지 않았다.
    자세히·출처·원인 규칙은 HCX를 거치지 않고 바로 답하므로, 조건을 바꾸거나 새로 묻는 말이
    섞여 있으면 규칙으로 잡지 않고 HCX에 맡긴다 (PR #136 리뷰: "구별로 자세히", "왜관읍은?")"""
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
    """자세히만 보여 달라는 말 ("서울 출생아 수 자세히", "연령별로 자세하게"는 질문이다)"""
    return bool(DETAIL_REQUEST.search(question)) and about_last_answer(question)


# 인사·감사·맞장구만 한 말. 통계 질문으로 해석하지 않는다
# (HCX가 질문으로 읽어 10초쯤 뒤 "통계표를 찾지 못했습니다"로 답하던 문제)
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
    """인사·감사·맞장구면 바로 돌려줄 말, 아니면 None"""
    more = "더 궁금한 통계가 있으면 이어서 질문해 주세요."
    if HELLO.search(question):
        return "안녕하세요. 인구 통계를 질문해 주세요 (예: 최근 5년 부산 출생아 수 추이)."
    if THANKS.search(question):
        return f"도움이 되었다니 다행입니다. {more}"
    if OKAY.search(question):
        return f"네. {more}"
    return None


# 직전 답의 출처·기준·계산식을 묻는 말 ("이 통계 출처가 뭐야?", "언제 기준이야?")
SOURCE_REQUEST = re.compile(
    r"출처|근거|어디서\s*(나온|나왔|가져|난|온)"
    r"|기준이?\s*(뭐|언제|어떻게)|언제\s*기준|계산식|어떻게\s*(계산|구한|구했)"
)
# "어떤 통계", "어느 통계표"는 통계표를 찾을 때 가장 흔한 말이다 ("다문화 관련 어떤 통계가 있어?").
# 직전 답을 가리킬 때만 출처 질문으로 본다: 가리키는 말(이·그·방금)이나 "나온·쓴"이 함께 있거나,
# 그 말뿐일 때 ("무슨 자료야?")
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
    """직전 답의 출처·계산식 줄 (답변의 수치 목록 아래에 이미 있는 줄 그대로). 없으면 None"""
    lines = [ln for ln in (last.get("answer") or "").split("\n") if SOURCE_LINE.match(ln)]
    if not lines or not last.get("sources"):
        return None
    return "\n".join(["직전 답은 아래 통계표의 수치로 만들었습니다.", *dict.fromkeys(lines)])


# 직전 답의 원인을 묻는 말 ("왜 줄어든 거야?"). 지표·지역을 새로 말하면 새 질문이다
# "왜"는 낱말로 떨어져 있을 때만 ("왜관읍은?"은 지역이다). "효과"·"영향"은 새 질문에도 흔해
# ("고령화 영향 큰 곳은?") 받지 않는다
WHY_REQUEST = re.compile(r"(^|\s)왜(\s|[?.!~]|$|그래|그런|이래|죠|지\b)|이유|원인|때문|어째서")
NO_CAUSE = "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 수치 변화만 보여드립니다."


def why_request(question: str) -> bool:
    return bool(WHY_REQUEST.search(question)) and about_last_answer(question)


# 보는 방식을 말한 질문 ("사망자 수 추이는?"). 없으면 지표만 바꾼 말로 본다 ("사망자 수는?")
VIEW_ASK = re.compile(r"추이|변화|추세|비교|차이|순위|몇\s*위|가장|제일|구성|별로|마다|어디")
KEEP_VIEW = ("type", "year", "start_year", "end_year", "level", "sido", "top", "ascending")


# 통계표를 찾는 말 ("다문화 관련 어떤 통계가 있어?"). 수치 조건을 이어받지 않고 통계표 찾기로 보낸다
# (새 질문 경로로 가면 지표가 없다고 앞 지표·지역을 이어받아 "부산 총인구"로 답했다)
TABLE_ASK = re.compile(rf"{WHICH_DATA}|통계표|(통계|자료|조사)(가|는|도)?\s*있")


def table_question(question: str) -> bool:
    """지원 지표를 말하지 않고 통계표·자료를 찾는 말인가 (직전 답의 출처를 묻는 말은 아니다)"""
    return (
        bool(TABLE_ASK.search(question))
        and not indicators.find_in_text(question)
        and not source_request(question)
    )


def summary_text(last: dict, question: str) -> str:
    """ "요약해줘"의 답: 직전 답의 요약 (answer_summary.py). "한 줄로"면 핵심 문장만.
    요약이 없는 답(답변 불가, 통계표 안내)은 답변의 첫 문단을 다시 준다"""
    if last.get("summary"):
        return answer_summary.text(last["summary"], answer_summary.one_line(question))
    return lead(last["answer"])


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
        .replace("{INDICATORS}", ", ".join(indicator_names(state["conditions"])))
    )
    raw = await _chat(question, system)
    trace["follow_raw"] = raw
    return parse_delta(raw)


def indicator_names(conditions: dict) -> list[str]:
    """변경 추출에 보여 줄 지표 목록. 지금 지표가 표에서 바로 계산한 지표면 그것도 넣는다
    (목록에 없으면 HCX가 "부산은?"을 이어 묻는 말이 아니라 새 질문으로 읽는다)"""
    names = list(indicators.specs())
    current = conditions.get("indicator")
    if conditions.get("dynamic_spec") and current and current not in names:
        names.append(current)
    return names


@llm_calls.staged("follow_up")
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
    """인사·감사에 대한 답. 조건은 그대로 둔다 (다음 "서울도 같이"가 이어진다)"""
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

    if refused := await table_guide.off_domain_early(question):
        # 집값 · 실업률 · 미세먼지처럼 인구 통계에 없는 주제: 질문 이해(HCX)도 부르지 않고 거절한다
        flow.add("범위 검사", refused.get("status"), "인구 통계에 없는 주제 (질문 이해 전에 거절)")
        refused["request_type"], refused["question"] = NEW, question
        return refused, {}, None
    res = await qa.ask(question, adjust=adjust)
    sources = kept.get("sources") or sources_of(question, res.get("parsed") or {})
    notice = inherited_notice(res.get("parsed") or {}, sources)
    if res.get("status") in {"out_of_range", "region_not_in_year", "missing_data"}:
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
    """같은 되묻기를 상한만큼 했는지 (통계표 찾기처럼 끝없이 다시 묻지 않는다)"""
    return (pending.get("count") or 1) >= question_clarify.MAX_ASKS


async def _after_asking(original: str, kind: str, user_id: int | None):
    """되묻기 상한에 닿았을 때: 이사 범위는 기본값(밖으로 나간 이사만)으로 찾고 알린다.
    지역은 짐작할 수 없으므로 답하지 않고 다시 질문하는 방법을 알린다"""
    if kind == "move_scope":
        told = f"범위를 알아듣지 못해 '{question_clarify.MOVE_OPTIONS[1]}'으로 답했습니다."
        return await _movement(original, "exclude", user_id, told)
    if kind in ("table_cell", "alternative"):
        # 표의 칸을 끝내 고르지 못함 · 대안을 고르지 않음: 원래 질문의 통계표를 안내한다
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


# 범위가 정해진 이사 질문의 지표: 밖으로 나간 이사만 = 전출자 수, 지역 안 이사도 = 이동자 수(총전출)
MOVE_INDICATOR = {"exclude": "전출자 수", "include": "이동자 수", "nation": "이동자 수"}
MOVE_TYPES = ("value", "trend", "compare", "rank")


async def _movement(question: str, scope: str, user_id: int | None, told: str | None = None):
    """범위가 정해진 이사 건수 질문. 순이동(전입-전출)으로 바꾸지 않고 범위에 맞는 지표로 답한다.
    지역·연도 해석은 HCX에 맡기고 지표만 정해 준다. 계산하지 못하면 그 범위의 통계표를 안내한다"""
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
    """통계표를 찾는 말: 수치 질문으로 해석하지 않고 통계표 찾기 결과로 답한다"""
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    found = await table_guide.find_tables(question, sid, None, user_id)
    res = table_guide.tables_answer(found)
    res.update(question=question, request_type=NEW, search_session=sid)
    return res, {}, None


async def _movement_tables(question: str, user_id: int | None, told: str | None = None):
    """이사 건수를 계산하지 못했을 때(지역·연도 자료 없음 등) 그 범위의 통계표를 안내한다"""
    sid = table_guide.new_search_session()
    found = await table_guide.find_tables(question, sid, None, user_id)
    res = table_guide.tables_answer(found)
    res.update(question=question, request_type=REPLY, search_session=sid)
    return res, {}, " ".join(filter(None, [told, "수치로 답하지 못해 통계표를 찾았습니다."]))


async def _turn(
    state: dict | None, question: str, selections: dict | None = None, user_id: int | None = None
) -> tuple[dict, dict, str | None]:
    """이번 말을 처리한다 → (응답, 조건의 출처, 알림)"""
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
            if name:  # "수원"·"강남"처럼 시·군·구를 뺀 이름
                found = await asyncio.to_thread(indicators.resolve_region, name)
                if found["status"] == "ok":
                    region = found["display"]
                elif found["status"] == "ambiguous_region" and not _gave_up_asking(pending):
                    # 같은 이름이 여러 곳(고성 등): 후보를 선택지로 보여 주며 다시 묻는다
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
            # 표의 칸 되묻기 (dynamic_indicator.py): 고른 칸으로 같은 표를 다시 계산한다
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
            # 조건을 모두 만족하는 값이 없어 제안한 대안
            # (alternative.py): 고른 대안의 답은 미리 계산해 두었다
            answers = (pending["clarification"].get("context") or {}).get("answers") or []
            code = dynamic_indicator.pick_option(choice, pending["clarification"])
            if code is None:
                # "귀화자랑 자녀까지 포함으로요"처럼 대안의 내용을 말로 고른 답 (번호 · 이름 아님):
                # 새 질문으로 보내면 원래 질문의 지역을 잃었다
                code = alternative_by_words(choice, answers)
            if code is not None:
                if 0 <= int(code) < len(answers):
                    res = copy.deepcopy(answers[int(code)])
                    res["chart"] = charts.build(res.get("result"))
                    res.update(request_type=REPLY, question=original, trace={})
                    return res, {}, None
        elif scope := question_clarify.scope_reply(choice):
            return await _movement(original, scope, user_id)
        # 미지원 지표라도 새 질문을 선택지 대기에 가두지 않는다.
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
        # 범위를 이미 말한 이사 건수 질문: 되묻지 않고, 순이동으로 바꿔 답하지도 않는다
        return await _movement(question, scope, user_id)
    if question_clarify.VAGUE_REGION.search(question) or question_clarify.MOVE_COUNT.search(
        question
    ):
        regions = await asyncio.to_thread(indicators.find_regions, question)
        if clarification := question_clarify.detect(question, regions):
            return _clarification_answer(question, clarification), {}, None
    if (state or {}).get("search_session") and (
        selections
        or (
            (pending or {}).get("missing") == "table"
            and is_choice_reply(question, pending.get("options") or [])
        )
    ):
        # 통계표 선택지(대상·성별·판, 비슷한 표 여럿)에 대한 답: 통계표 찾기가 이어서 처리한다.
        # 새 질문은 이 분기를 건너뛰어 수치 질문 경로로 보낸다
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
    usable = bool(state and (state.get("conditions") or {}).get("type") not in NO_CONDITIONS)
    if not usable and summary_request(question):
        # 이어 갈 조건이 없을 때(첫 말, 통계표만 안내한 뒤)의 "요약해줘": 질문으로 해석하지 않는다
        # (HCX가 통계 질문으로 읽어 "통계표를 찾지 못했습니다"로 답하던 문제)
        if ((state or {}).get("last") or {}).get("answer"):
            last = state["last"]
            return _reuse(state, summary_text(last, question), None, SUMMARY), {}, None
        return _nothing_to_summarize(), {}, None
    if not usable:
        return await _new(question, None)

    if await asyncio.to_thread(complete_question, question):
        return await _new(question, state)

    has_last = bool((state.get("last") or {}).get("answer"))
    view = chart_request(question) if has_last else None
    delta = None
    if has_last and view is None and await asyncio.to_thread(detail_request, question):
        # "요약하지 말고 더 자세히": 요약이 아니라 직전 답 전체(문장 + 수치 목록)를 다시 준다
        last = state["last"]
        return _reuse(state, last["answer"], last.get("chart"), DETAIL), state["sources"], None
    if has_last and view is None and await asyncio.to_thread(source_request, question):
        if text := source_text(state["last"]):
            return _reuse(state, text, None, SOURCE), state["sources"], None
    if view is None and await asyncio.to_thread(table_question, question):
        return await _find_tables(question, state, user_id)
    asked_why = has_last and view is None and await asyncio.to_thread(why_request, question)
    if asked_why and state["last"].get("summary"):
        # "왜 줄어든 거야?": 원인은 판단하지 않는다고 알리고 직전 답의 요약을 다시 준다
        text = f"{NO_CAUSE}\n{answer_summary.text(state['last']['summary'])}"
        return _reuse(state, text, state["last"].get("chart"), SUMMARY), state["sources"], None
    if view is None and not (has_last and summary_request(question)):
        trace: dict = {}
        split = await asyncio.to_thread(region_split_changes, question, state["conditions"])
        if composition_request(question):  # "구성비로 보여줘": 보는 방식만 바꾼다
            delta = parse_delta('{"request_type": "change", "changes": {"type": "composition"}}')
        elif split:  # "구별로 보여줘": 보던 시도 안의 시군구 순위로
            delta = {"request_type": CHANGE, "changes": split, "chart": None, "unclear": None}
        else:
            delta = await extract(state, question, trace)
        year = follow_year(question, state["conditions"])
        if year and not names_indicator(question, {}) and delta["request_type"] != NEW:
            # 상대 시점은 HCX가 아니라 코드가 푼다 ("작년은?"을 2020년의 다음 해로 읽던 문제).
            # HCX가 새 질문(new)으로 본 말은 바꾸지 않는다: 사전에 없는 말로 지표를 말한
            # "작년에 수도권에서 태어난 애기 다 합치면?"을 앞 지표(혼인) · 지역(부산)의 2025년으로
            # 바꿔 답했다 (11/20). 새 질문 경로도 "작년"을 오늘 기준으로 푼다
            changes = {
                k: v
                for k, v in (delta.get("changes") or {}).items()
                if k not in ("year", "start_year", "end_year")
            }
            delta.update(request_type=CHANGE, unclear=None, changes={**changes, "year": year})
        if delta["unclear"]:
            return _unclear(state, delta["unclear"]), state["sources"], None
        if delta["request_type"] == SUMMARY and delta["changes"]:
            delta["request_type"] = CHANGE  # "2022년만 요약해줘": 조건을 바꾼 답이 먼저다
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
            _reuse(state, summary_text(last, question), last.get("chart"), SUMMARY),
            state["sources"],
            None,
        )

    return await _new(question, state)


def next_state(state: dict | None, question: str, res: dict, sources: dict) -> dict:
    """이번 답 뒤의 세션 상태"""
    prev = state or {}
    recent = [*(prev.get("recent") or []), question][-RECENT:]
    turn_no = (prev.get("turn_no") or 0) + 1
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
                "count": (before.get("count") or 1) + 1 if again else 1,  # 같은 되묻기 횟수
            },
            "recent": recent,
            "turn_no": turn_no,
        }
    if res.get("request_type") in (CHART, SUMMARY, DETAIL, SOURCE):
        last = {**prev["last"], "chart": res.get("chart") or prev["last"].get("chart")}
        return {**prev, "last": last, "recent": recent, "turn_no": turn_no}
    if res.get("request_type") in (UNCLEAR, CHAT):
        return {**prev, "recent": recent, "turn_no": turn_no}
    if res.get("status") in (TABLES, *table_guide.REFUSED):
        # 수치 없이 표만 안내했다 (또는 범위 밖이라 표도 없다): 수치 조건은 이어 가지 않는다
        # (다음 "서울도 같이"는 새 질문).
        # 표 선택지를 물었으면 다음 말을 그 답으로 읽는다
        found = res.get("tables")
        return {
            "conditions": {},
            "sources": {},
            "last": {
                "status": res.get("status"),
                "answer": res.get("answer") or "",
                "sources": [],
                "chart": None,
            },
            "pending": {
                "missing": "table",
                "options": table_choice_options(found),
                # 표를 고른 뒤 그 표로 바로 계산해 보려고 원래 질문과 해석을 둔다
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
    return {
        "conditions": effective(res.get("parsed") or {}, res.get("result") or {}),
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
    """답에 통계표를 붙인다 (table_guide.py). 표 찾기가 실패해도 수치 답은 그대로 준다"""
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
    if res.get("status") == qa.CROSS_DYNAMIC:
        if await _cross_dynamic(res, state, asked, user_id):
            return
        res["status"] = "unknown_indicator"  # 한쪽 값을 못 구했다: 지금처럼 통계표 찾기로 (아래)
    if res.get("status") in table_guide.SEARCH_INSTEAD:
        sid = (state or {}).get("search_session") or table_guide.new_search_session()
        kind, trace, parsed = res.get("request_type"), res.get("trace") or {}, res.get("parsed")
        # 전에 바로 계산한 지표면 통계표 찾기 없이 그 표로 계산한다 (계산 방법 캐시)
        computed = await dynamic_indicator.cached_answer(asked, parsed or {}, trace)
        if computed is not None:
            res.clear()
            res.update(computed)
            if not res.get("clarification"):
                res["chart"] = charts.build(res.get("result"))
            res["request_type"], res["question"], res["search_session"] = kind, asked, sid
            await _attach_cards(res, asked)
            return
        try:
            found = await table_guide.find_tables(asked, sid, None, user_id)
        except Exception:
            logger.exception("통계표 찾기 실패")
            return
        try:  # 사전에 없는 지표: 찾은 표에서 바로 계산해 본다 (dynamic_indicator.py)
            # 넘기기 꾸러미(found["handoff"])의 후보 5개를
            # 동시에 계산해 고른다. 꾸러미가 없으면 예전 방식
            computed = await dynamic_indicator.compute_from_handoff(
                asked, parsed or {}, found, trace
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
            pair = cross_indicator.detect_partial(asked, both=True)
        except Exception:
            logger.exception("두 값 나누기 감지 실패")
            pair = None
        if pair is not None:
            if pair.dynamic == "both":
                # 두 값 모두 사전에 없는 나누기: 한 표 안에서 못 찾았으니 양쪽을 따로 찾아 나눈다
                two = {"parsed": {**(parsed or {}), "cross": pair.as_dict()}, "trace": trace}
                two["request_type"] = kind
                if await _cross_dynamic(two, state, asked, user_id) and two.get("status") == "ok":
                    res.update(two)
                    return
        res.update(table_guide.tables_answer(found, {"parsed": parsed or {}}))
        # 수치 답을 하려다 표 찾기로 넘어온 경위(질문 해석)는 요청 기록에 남긴다
        res["request_type"], res["question"], res["trace"] = kind, asked, trace or {}
        res["search_session"] = sid
        return
    if ungrounded(res) and await _dynamic_instead(res, state, asked, user_id):
        return
    if searchable_after(res) and await _search_after(res, state, asked, user_id):
        return
    await _attach_cards(res, asked)


def searchable_after(res: dict) -> bool:
    """사전 지표가 자료가 없어 답하지 못한 새 질문 (table_guide.SEARCH_AFTER).
    추계값 제안(suggestion)이 이미 있으면 그대로 둔다 (수록 기간 밖 미래 연도 → 장래추계)"""
    result = res.get("result") or {}
    return (
        res.get("status") in table_guide.SEARCH_AFTER
        # 되묻기에 답한 턴(REPLY)도: 원래 질문과 받은 지역을 합친 질문이다 ("우리 시 결혼 건수" →
        # "창원시" → 사전은 시도까지만이라 끝났다. 시군구 표가 따로 있다)
        and res.get("request_type") in (NEW, REPLY)
        and not (result.get("suggestion") or {}).get("status") == "ok"
    )


def _year_found_elsewhere(res: dict, found: dict) -> str | None:
    """사전 답이 '수록 기간 밖'(out_of_range)인데 통계표 찾기에서 검토가 고른 표(exact · partial)가
    물은 연도를 담고 있으면(checks의 year ✔) 표 안내 앞에 붙일 한 줄. 아니면 None.
    검토가 고르지 않은 후보는 보지 않는다 (같은 해의 다른 주제 표도 year ✔일 수 있다).
    그해에 없던 지역(region_not_in_year)은 바꾸지 않는다: 표의 지역 단위 ✔는 그해에 그 지역이
    있었다는 근거가 아니다"""
    result = res.get("result") or {}
    if res.get("status") != "out_of_range" or found.get("status") not in ("ok", "partial_match"):
        return None
    targets = (found.get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None  # 지표가 여럿인 질문은 지금처럼
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


async def _search_after(res: dict, state: dict | None, asked: str, user_id: int | None) -> bool:
    """사전이 자료가 없어 답하지 못한 지표를 다른 통계표에서 찾아 계산한다. res를 바꿨으면 True

      바로 계산됨 · 대안 제안 · 칸 되묻기   그 답
      수록 기간 밖인데 고른 표에 그 연도   표 안내 (status tables, _year_found_elsewhere)
      그 밖                               사전의 안내를 그대로 두고, 찾은 표를 표 카드로 붙인다
    바꾼 원래 답은 trace["replaced"]에 남긴다 (사전 답이 어떻게 바뀌었는지 나중에 센다)"""
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    trace = res.setdefault("trace", {})
    parsed = res.get("parsed") or {}
    try:
        found = await table_guide.find_tables(asked, sid, None, user_id)
        computed = await dynamic_indicator.compute_from_handoff(asked, parsed, found, trace)
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
            # 사전은 '수록 기간 밖'인데 검토가 고른 표가 그 연도를 담고 있다 (칸을 못 골라 계산만
            # 못 함). "1990~2025년만 있습니다"는 사실과 달라 표 안내로 바꾼다 (11/20 D-020)
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
        # 다른 표도 수록 기간 밖 등으로 끝났으면 사전의 안내가 더 낫다
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
        # 사전이 왜 못 했는지("혼인 건수 자료는 1990~2025년만 있습니다")는 앞에 붙이지 않는다:
        # 바로 뒤에 그 밖의 연도 값을 줘서 앞뒤가 어긋나 보이고, 다른 표에서 꺼낸 것은 답 아래
        # ※ 안내(OTHER_TABLE_NOTE)와 출처가 이미 알린다 (11/20 세진). 원래 답은 trace["replaced"]
        res["chart"] = charts.build(res.get("result"))
        await _attach_cards(res, asked)
    res.setdefault("trace", trace)
    res["request_type"], res["question"], res["search_session"] = kind, asked, sid
    return True


CROSS_KINDS = ("value", "trend", "compare", "rank")


def _side_question(parsed: dict, phrase: str, kind: str) -> str:
    """사전에 없는 쪽만 따로 물은 질문 ("2020년 서울특별시 1인 가구 수",
    "2015년부터 2020년까지 서울특별시 1인 가구 수 추이", "2020년 시도별 1인 가구 수")"""
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
    """사전에 없는 말 하나를 동적 지표로 계산한 결과(result). 건수 · 인원이 아니거나 못 하면 None.
    계산 방법 캐시를 먼저 보고, 없으면 통계표 찾기 → 넘기기 계산 (처음 한 번만 HCX를 부른다.
    성공하면 캐시에 남아 다음 연도 · 지역은 통계표 찾기 없이 계산된다)"""
    sub_q = _side_question(parsed, phrase, kind)
    sub_parsed = {
        **parsed,
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


# 같은 표 계산에서 칸과 맞출 필요가 없는 꾸밈말 ("베트남 사람" · "베트남 국적자" · "베트남 출신").
# 대상을 좁히는 말(결혼이민자 · 유학생 · 근로자)이나 애매한 말은 넣지 않는다
# (넣으면 좁혀 물은 값 대신 넓은 값을 답한다)
SAME_TABLE_FILLER = {"사람", "수", "명", "출신", "국적", "국적자", "온"}
SAME_TABLE_OPTIONS_MAX = 50  # 실패 기록에 남기는 분류당 칸 이름 수
# 같은 표 계산 기록(trace["cross_sides"])의 status. 에이전트가 이 값으로 다음 행동을 고른다
SAME_TABLE_STATUSES = ("ok", "uncovered", "no_cell", "not_count")


def _leftover_words(phrase: str) -> list[str]:
    """같은 표의 칸으로 모두 설명돼야 하는 분자 말의 낱말 (꾸밈말 · 1글자 낱말 뺌).
    칸 고르기 공통의 바꾸기(table_words: 이동 말이 있을 때 '학교 가는' → 통학, 출근 → 통근)만
    거치고, 이 경로만의 바꿔 말하기는 하지 않는다"""
    if dynamic_indicator.MOVE_CONTEXT.search(phrase):  # table_words와 같은 조건 · 같은 바꾸기
        phrase = dynamic_indicator.SCHOOL_GOING.sub("통학", phrase)
    words = [w for w in phrase.split() if len(w) >= 2 and w not in SAME_TABLE_FILLER]
    return [dynamic_indicator.table_words(w) for w in words]


def _cell_options(tbl_id: str) -> dict[str, list[str]]:
    """표가 가진 칸 이름 {분류 이름: [칸 이름, …]} (지역 분류 빼고, 분류당 최대 50개)"""
    dims, region_dim, _ = dynamic_indicator.schema(tbl_id)
    return {
        d.name: list(d.codes.values())[:SAME_TABLE_OPTIONS_MAX] for d in dims if d.no != region_dim
    }


async def _same_table_side(
    phrase: str, known: str, parsed: dict, kind: str, trace: dict, table: str | None = None
) -> dict | None:
    """사전에 없는 쪽을 먼저 다른 쪽 사전 지표의 표 안에서 찾는다 ("등록 외국인 중에 베트남 사람":
    등록외국인 표의 국적 칸). 같은 표라 기준이 같다. 통계표 찾기를 부르지 않는다. 못 하면 None

    분자 말의 낱말이 모두 고른 칸으로 설명될 때만 계산한다 ("베트남 출신 결혼이민자"에서 결혼이민자
    칸이 없으면 베트남 전체를 답하지 않는다). 판단은 phrase 하나로만 한다 (에이전트가 말을 다시
    써서 부를 수 있게). table: 이 표만 본다
    성공 · 실패 모두 trace["cross_sides"]에 같은 모양으로 남긴다:
      phrase · same_table · status · leftover · cells · why · unit · options
    status (SAME_TABLE_STATUSES):
      ok         계산했고 건수 · 인원이라 나누기에 쓴다
      uncovered  칸은 골랐지만 칸으로 설명되지 않은 낱말이 남음 (leftover)
                 → 말을 표의 낱말로 다시 쓰기
      no_cell    질문의 말과 맞는 칸을 고르지 못함 → 말을 바꾸거나 다른 표
      not_count  칸은 맞게 골라 계산했지만 값이 건수 · 인원이 아님 (%, 세, 명당 …).
                 비율을 다시 나누면 엉뚱한 값이 된다 → 건수가 있는 다른 표, 또는 비율을 물었으면
                 그 값을 그대로 답하기 (말을 다시 써도 같은 칸이라 같은 이유로 실패한다)"""
    if table:
        tables = [table]
    else:
        spec = indicators.spec(known)
        if not spec or spec.get("method") != "lookup":
            return None
        tables = [src["table"] for src in spec.get("sources") or []]
    sub_q = _side_question(parsed, phrase, kind)
    sub_parsed = {**parsed, "type": kind, "indicator": phrase, "term": phrase, "dynamic_spec": None}
    if kind == "rank":
        sub_parsed["top"] = None
    items = _leftover_words(phrase)
    for tbl_id in tables:
        step: dict = {"dynamic": []}
        report: dict = {}
        # check_target · cells_by_llm은 넣지 않는다: 말 전체를 칸 이름과 맞추면 맞는 질문도 막고,
        # cells_by_llm이 있으면 낱말 검사를 건너뛴다
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
        if computed:  # 칸은 맞았고 계산도 됐지만 나누기에 쓸 수 없는 단위
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
    """두 값을 나누는데(빼는데) 한쪽 또는 양쪽이 사전에 없다. res를 바꿨으면 True

    사전에 없는 쪽은 그 말만 따로 물은 것처럼 동적 지표로 구한다 (_dynamic_side). 사전 쪽은
    qa.compute로 같은 조건을 계산한다. 값 · 추이 · 비교 · 순위 모두 cross_indicator.combine_parts가
    지역 · 연도 · 성별 · 주기를 확인한 뒤 나눈다"""
    parsed = res.get("parsed") or {}
    trace = res.setdefault("trace", {})
    c = cross_indicator.Cross(**parsed["cross"])
    kind = parsed.get("type") if parsed.get("type") in CROSS_KINDS else "value"
    if kind == "compare" and len(parsed.get("regions") or []) <= 1:
        kind = "value"
    q = {**parsed, "type": kind, "cross": None, "indicators": None}
    if kind in ("value", "trend") and not q.get("regions"):
        # 지역을 말하지 않은 비율은 전국 ("등록 외국인 중에 베트남 사람 비율")
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
            q["year"] = got[side]["year"]  # 동적 쪽이 답한 해로 나머지를 맞춘다
    for side in ("num", "den"):
        if side in got:
            continue
        known = {**q, "indicator": sides[side], "term": sides[side]}
        if kind == "rank":
            known["top"] = None  # 나눈 값으로 다시 순위를 매기므로 모든 지역을 받는다
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
    """답에 쓴 표를 표 카드로 붙인다. 카드를 못 만들어도 답은 그대로 준다"""
    try:
        cards = await table_guide.cards_for(asked, res.get("sources") or [])
    except Exception:
        logger.exception("통계표 카드 만들기 실패")
        cards = []
    if cards:
        res["tables"] = {"status": "ok", "recommendations": cards}


# 사전 지표로 답했거나 그 지표의 자료 사정을 안내한 상태. 근거 없는 지표면 둘 다 질문과 맞지 않는다
# (예: "2020년 서울 1인 가구 수"를 미혼 인구로 읽고 "미혼 인구 자료는 2022~2025년만 있습니다")
DICTIONARY_ANSWERED = ("ok", "out_of_range", "missing_data", "region_not_in_year", "missing_region")
# missing_region: 근거 없는 지표로 지역을 되묻기보다 통계표 찾기가 낫다
# ("1인 가구가 가장 많은 시도의 통근 인구"를 총인구로 읽고 "어느 지역인지"를 물었다)


def ungrounded(res: dict) -> bool:
    """사전 지표로 답했지만 질문에 그 지표의 이름·별칭이 없는 새 질문 (qa.correct의 grounded)"""
    parsed = res.get("parsed") or {}
    return (
        res.get("status") in DICTIONARY_ANSWERED
        and res.get("request_type") == NEW
        and parsed.get("grounded") is False
        and not parsed.get("dynamic_spec")
    )


async def _dynamic_instead(res: dict, state: dict | None, asked: str, user_id: int | None) -> bool:
    """사전 지표 대신 통계표에서 바로 계산한다. res를 바꿨으면 True

    질문에 그 지표의 이름·별칭이 없으면 HCX가 비슷한 다른 지표를 고른 것일 수 있다
    (예: "1인 가구 중 여성 비율" → 서울 인구 성별 구성). 그래서 원래 답은 돌려주지 않는다.
      바로 계산됨       그 답
      칸을 되물어야 함   되묻기
      그 밖             통계표 안내 (찾은 표가 없으면 원래 답 그대로: 통계표 찾기 실패 등)
    """
    sid = (state or {}).get("search_session") or table_guide.new_search_session()
    trace = res.get("trace") or {}
    parsed = res.get("parsed") or {}
    found = None
    if parsed.get("rank_axis") and (axis := await _axis_rank(asked, parsed, trace)):
        # 지역이 아닌 분류의 순위: 사전 지표의 표에서 그 분류로 순위를 매겼다
        kind = res.get("request_type")
        res.clear()
        res.update(axis)
        res["chart"] = charts.build(res.get("result"))
        await _attach_cards(res, asked)
        res["request_type"], res["question"], res["search_session"] = kind, asked, sid
        return True
    try:
        computed = await dynamic_indicator.cached_answer(asked, parsed, trace)
        if computed is None:
            found = await table_guide.find_tables(asked, sid, None, user_id)
            computed = await dynamic_indicator.compute_from_handoff(asked, parsed, found, trace)
    except Exception:
        logger.exception("근거 없는 지표의 바로 계산 실패")
        return False
    if computed is None and found is None:
        return False
    if res.get("status") == "ok" and (same := _same_dictionary_table(parsed, computed, found)):
        # 바로 계산이 사전 지표가 쓰는 표를 골랐다 (예: 출생아 수 표 DT_1B8000H).
        # 표 안내나 칸 되묻기, 같은 표의 다른 칸 대신 검증된 사전 계산을 쓴다
        # (변형셋 1차: 사전 표를 고르고도 안내로 끝남)
        trace["dynamic_kept"] = {"indicator": parsed.get("indicator"), "tbl_id": same}
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


async def _after_table_choice(pending: dict | None, found: dict) -> dict | None:
    """표 선택지에 답한 뒤 고른 표로 원래 질문을 바로 계산한 답. 못 하면 None (지금처럼 표 안내)"""
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
    """사전 지표의 표로 지역이 아닌 분류의 순위를 계산한 답 (예: 등록외국인 표의 국적별 순위)"""
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


def _same_dictionary_table(parsed: dict, computed: dict | None, found: dict | None) -> str | None:
    """바로 계산(또는 표 안내)이 고른 표가 사전 지표의 표이면 그 표 ID. 아니면 None"""
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
    """답의 요약. 요약을 만들다 실패해도 답은 그대로 준다"""
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
    """/ask 한 턴. qa.ask의 응답에 더하는 것:
    session_id, question, chart, request_type, applied(적용한 조건), notice(알림)"""
    sid = session_id or new_id()
    steps = flow.start()  # 이 턴의 처리 흐름 (요청 기록 · 평가 CSV에 남긴다)
    calls = llm_calls.start()  # 이 턴의 LLM 호출 (단계 · 토큰 · 시간 · 다시 시도)
    understand_merged.start()  # UNDERSTAND_MERGED=gemma: 질문 이해가 함께 뽑은 조건 추출 보관함
    async with _lock(sid):
        state, expired = await _load(sid)
        moved = state is not None and not same_owner(state, user_id)
        if state is None and session_id:
            # 만료·삭제된 대화 ID는 재사용하지 않는다. 남아 있는 턴 기록과 새 계정 기록이
            # 같은 대화로 묶이는 것을 막는다.
            sid = new_id()
        if moved:  # 다른 계정이 시작한 대화: 그 조건·답을 이어받지 않고 새 대화로 답한다
            sid, state, expired = new_id(), None, False
        res, sources, notice = await _turn(state, question, selections, user_id)
        await _attach_tables(res, state, question, user_id)
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
        after["user_id"] = user_id  # 이 대화의 주인 (same_owner). 손님이면 None
        res.setdefault("question", question)
        res["session_id"] = sid
        res["applied"] = applied(after.get("conditions") or {}, after.get("sources") or {})
        res["notice"] = notice
        first_line = (res.get("answer") or "").strip().split("\n")[0][:80]
        flow.add("최종 답", res.get("status"), f"{res.get('request_type') or ''} · {first_line}")
        res.setdefault("trace", {})["flow"] = list(steps)  # 턴 기록 · 요청 기록 · 평가 CSV
        res["trace"]["llm_calls"] = list(calls)
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
        if user_id is not None:  # 로그인 유저의 대화는 목록에 남겨 나중에 다시 연다
            await conversations.touch(sid, user_id, question)
    except (SQLAlchemyError, OSError):
        logger.exception("대화 상태 저장 실패")


def keeps_for(state: dict) -> timedelta:
    """이 대화를 이어 가는 기간: 로그인 유저의 대화는 기록 보관 기간 내내, 손님은 1시간"""
    return TURNS_FOR if state.get("user_id") is not None else SESSION_FOR


async def load(session_id: str, at: datetime) -> tuple[dict | None, bool]:
    """(세션 상태, 만료됐는가). 서버 메모리에 있으면 DB를 읽지 않는다"""
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
    """세션 상태를 남긴다. 만료된 지 하루가 지난 손님 대화, 보관 기간이 지난 로그인 유저 대화는
    이때 지운다 (로그인 유저의 대화는 나중에 다시 열어 이어 간다)"""
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
    """턴 기록 (app.chat_turns): 무엇을 요청했고 조건이 어떻게 바뀌었는지.
    디버깅과 멀티턴 채점(유지할 조건·바뀔 조건·다시 계산했는지)에 쓴다.
    TURNS_FOR이 지난 기록은 지운다"""
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
                # 처리 흐름 (관리자 화면의 대화 기록에서 턴마다 본다). 질문 · 답 글처럼 가린다
                "flow": json.dumps(
                    redact((res.get("trace") or {}).get("flow")), ensure_ascii=False, default=str
                ),
            },
        )
        await conn.execute(
            text("delete from app.chat_turns where created_at < :cutoff"),
            {"cutoff": now() - TURNS_FOR},
        )
