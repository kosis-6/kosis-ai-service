"""질문 답변(/ask)의 연도 되묻기와 대화 이어 받기

과거 표현만 있고 연도가 없는 질문("과거와 최근 인구수 비교해줘", "예전보다 늘었어?")은 어느 해와
비교할지 알 수 없어 계산하지 않고 되묻는다 (status = missing_year). 가능한 기간을 함께 알려 준다.
  - DB에 있는 기간 (지표 사전에 연결한 과거 연도용 표 포함)
  - KOSIS에 더 이른 연도가 있으면 그 시작 연도 (catalog.table_meta)

최근·최신·현재처럼 지금을 가리키거나 시점 표현이 없으면 되묻지 않고 가장 최근 연도로 답한다.

되묻기 뒤 사용자가 "2000년이랑"처럼 연도만 짧게 답하면, 같은 session_id의 앞 질문과 합쳐 다시 묻는다
("과거와 최근 인구수 비교해줘" + "2000년" → "2000년과 최근 인구수 비교해줘").
대화 상태는 검색 되묻기와 같은 app.chat_sessions에 "ask:" 키로 둔다 (dialog.py).
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.services import slots, timeref

VAGUE_PAST = re.compile(r"과거|예전|옛날|옛적")
# "과거부터 지금까지 합계출산율 추이" → 되묻지 않고 수록 기간 전체 추이 (qa.correct의 from_start)
FROM_PAST = re.compile(r"(과거|예전|옛날|옛적)\s*부터")
VAGUE_WITH_PARTICLE = re.compile(r"(과거|예전|옛날|옛적)(와|랑|를|가|는|)")
# 받침 없는 말 뒤 조사 → '년'(받침) 뒤 조사. 없는 조사(보다·에·이랑 등)는 그대로
PARTICLE_AFTER_NYEON = {"와": "과", "랑": "이랑", "를": "을", "가": "이", "는": "은"}
FOLLOW_UP_MAX_CHARS = 25  # 이보다 길면 되묻기 답이 아니라 새 질문으로 본다
# 되묻기 답에서 연도 표현을 지운 뒤 남아도 되는 말 (조사·접속어·문장부호).
# 이것 말고 다른 말("출생아 수", "서울 인구")이 남으면 새 질문으로 본다
REPLY_FILLER = re.compile(
    r"\s+|[,.·~\-?!/()]|이랑|랑|하고|과|와|부터|까지|에서|이고|그리고|및|도|으로|로|"
    r"입니다|이에요|예요|이요|요|기준|비교|대비|보다|년도|년|쯤|정도"
)
PENDING_FOR = timedelta(minutes=30)  # 되묻기 답을 기다리는 시간
CENSUS_EVERY = 5  # 인구총조사 주기 (기간 안내를 줄일 때)


def has_explicit_year(question: str) -> bool:
    """연도나 상대 시점("2010년", "'95년", "작년", "5년 전", "최근 5년")이 있는가"""
    return bool(
        slots.YEAR.search(question)
        or slots.short_years(question)
        or timeref.resolve(question)
        or timeref.RECENT_N_YEARS.search(question)
        or timeref.N_YEARS_AGO.search(question)
    )


def needs_year(question: str) -> bool:
    """과거 표현만 있고 연도가 없어 되물어야 하는 질문인가"""
    return (
        bool(VAGUE_PAST.search(question))
        and not has_explicit_year(question)
        and not FROM_PAST.search(question)
    )


def _span_text(spans: list[tuple[int, int]]) -> str:
    """[(1980, 1980), (2022, 2025)] → '1980년, 2022~2025년'
    이어지는 기간은 합치고 ([(1970, 1989), (1990, 2025)] → '1970~2025년'),
    5년마다 한 해씩인 인구총조사는 줄인다 (1970, 1975, …, 2020 → '1970~2020년(5년마다)')"""
    merged: list[list[int]] = []
    for lo, hi in sorted(set(spans)):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    parts, i = [], 0
    while i < len(merged):
        lo, hi = merged[i]
        j = i
        while (
            lo == hi
            and j + 1 < len(merged)
            and merged[j + 1][0] == merged[j + 1][1] == merged[j][0] + CENSUS_EVERY
        ):
            j += 1
        if j - i >= 2:  # 세 번 이상 이어질 때만 줄인다
            parts.append(f"{lo}~{merged[j][0]}년({CENSUS_EVERY}년마다)")
        else:
            j = i
            parts.append(f"{lo}년" if lo == hi else f"{lo}~{hi}년")
        i = j + 1
    return ", ".join(parts)


def missing_year(parsed: dict[str, Any]) -> dict[str, Any]:
    """되묻기 결과. 지표를 알면 그 지표의 기간을, 모르면 연도만 묻는다 (DB를 읽으므로 스레드에서)"""
    from app.services import indicators, lookup

    name = parsed.get("indicator") or ""
    out: dict[str, Any] = {
        "status": "missing_year",
        "indicator": name or None,
        "reason": "과거 어느 연도와 비교할지 질문에 없습니다.",
    }
    sp = indicators.spec(name) if name else None
    if sp is None:
        return out
    spans, tables = [], []
    if sp["method"] == "lookup":
        for s in sp["sources"]:
            tables.append(s["table"])
            try:
                spans.append(lookup._years(lookup._effective(s, "계") or s))
            except Exception:  # noqa: BLE001 - 적재 안 된 표는 기간 안내에서 뺀다
                continue
    else:
        spans.append(indicators._data_years())
        tables += [s["table"] for s in sp.get("sources") or []]
    if spans:
        out["available"] = _span_text(spans)
        out["available_years"] = [min(a for a, _ in spans), max(b for _, b in spans)]
    marks = ", ".join("?" for _ in tables)
    rows = indicators._catalog_rows(
        f"select min(start_year) as lo from table_meta where tbl_id in ({marks})", tables
    )
    first = rows[0]["lo"] if rows else None
    if first and spans and first < out["available_years"][0]:
        out["kosis_first_year"] = first
    return out


def guide(r: dict[str, Any]) -> str:
    """되묻기 답변 문장 (qa._refusal_text)"""
    name = r.get("indicator")
    if not r.get("available"):
        return "비교할 연도를 넣어 다시 질문해 주세요 (예: 2010년과 2025년)."
    lo, hi = r["available_years"]
    text = f"{name} 자료는 {r['available']}이 있습니다."
    if r.get("kosis_first_year"):
        text += f" KOSIS에는 {r['kosis_first_year']}년부터 있어, 그 연도를 물으시면 받아 옵니다."
    return text + f" 비교할 연도를 알려 주세요 (예: {lo}년과 {hi}년)."


# ---- 대화 이어 받기 -----------------------------------------------------------------------


def merge(pending: str, reply: str) -> str | None:
    """되묻기 답("2000년이랑")을 앞 질문과 합친다. 답이 아니라 새 질문이면 None"""
    reply = reply.strip()
    if len(reply) > FOLLOW_UP_MAX_CHARS or not has_explicit_year(reply):
        return None
    if not _only_years(reply):  # "2020년 출생아 수", "작년 서울 인구"는 새 질문
        return None
    years = sorted({int(y) for y in slots.YEAR.findall(reply)} | slots.short_years(reply))
    if len(years) == 1 and VAGUE_PAST.search(pending):
        # "과거와" → "2000년과" ('년' 뒤라 받침 조사로 바꾼다)
        return VAGUE_WITH_PARTICLE.sub(
            lambda m: f"{years[0]}년" + PARTICLE_AFTER_NYEON.get(m.group(2), m.group(2)),
            pending,
            count=1,
        )
    return f"{pending} ({reply})"


def _only_years(reply: str) -> bool:
    """연도 표현과 조사만 있는 답인가 ("2000년이랑", "2000년, 2010년", "작년", "5년 전")"""
    rest = slots.YEAR.sub(" ", reply)
    rest = slots.SHORT_YEAR.sub(" ", rest).replace("'", " ")
    rest = timeref.RECENT_N_YEARS.sub(" ", rest)
    rest = timeref.N_YEARS_AGO.sub(" ", rest)
    for word, _ in timeref.WORDS:
        rest = rest.replace(word, " ")
    return not REPLY_FILLER.sub("", rest)


def _key(session_id: str) -> str:
    return f"ask:{session_id}"


async def load_pending(session_id: str) -> str | None:
    """되묻기를 기다리는 앞 질문 (PENDING_FOR 안에 저장된 것만)"""
    from app.core.db import engine
    from app.services import dialog

    async with engine.begin() as conn:
        await dialog._ensure(conn)
        row = (
            await conn.execute(
                text(
                    "select slots, updated_at from app.chat_sessions"
                    " where session_id = :k and status = 'missing_year'"
                ),
                {"k": _key(session_id)},
            )
        ).first()
    if row is None or datetime.now(UTC) - row.updated_at > PENDING_FOR:
        return None
    return (row.slots or {}).get("question")


async def save(session_id: str, status: str, question: str) -> None:
    """이번 답의 상태를 남긴다.
    missing_year면 다음 답과 합칠 질문, 그 밖이면 기다리던 질문이 끝남"""
    from app.core.db import engine
    from app.services import dialog

    async with engine.begin() as conn:
        await dialog._ensure(conn)
        await conn.execute(
            text(dialog.UPSERT),
            {
                "session_id": _key(session_id),
                "status": status,
                "family_id": None,
                "slots": json.dumps({"question": question}, ensure_ascii=False),
                "asked": None,
                "turns": 0,
            },
        )
