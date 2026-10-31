"""되묻기·대화 상태 (실전 2 F-14, 검색 규칙 2.3 가안)

- 직전 응답이 clarify/ambiguous면 이번 질문을 먼저 선택지와 문자열로 맞춘다
  ("혼인 귀화자, 여성, 2010년", "2번", "아무거나").
  맞으면 검색을 다시 하지 않고 저장된 패밀리로 멤버·판만 고른다 (search.resolve).
  안 맞으면 새 질문으로 보고 처음부터 검색한다.
- 화면 버튼으로 고르면 selections로 받는다 (문자열 매칭 생략)
  {"family_id": ..., "target": "혼인 귀화자", "sex": "여", "edition": "TX_11025_A022"}
- 되묻기는 최대 2턴. 그래도 대상이 안 정해지면 기본값(전체)으로 답한다.
- 대화 상태는 PostgreSQL chat_sessions에 저장한다 (처음 저장할 때 테이블을 만든다).
"""

import copy
import json
import re
from typing import Any

from sqlalchemy import text

from app.services import search
from app.services.search import norm

MAX_CLARIFY_TURNS = 2
ANYTHING = re.compile(r"아무거나|아무\s*거나|상관\s*없|기본(?:값|으로)?|다\s*괜찮|알아서")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
EDITION = re.compile(r"((?:19|20)\d{2})\s*~\s*((?:19|20)\d{2})")
NUMBER = re.compile(r"^\s*(?:([1-4])(?!\d)\s*번?|([①②③④]))")
ORDINAL = {"첫": 1, "두": 2, "세": 3, "네": 4}
MALE = re.compile(r"남자|남성|남(?![가-힣])")
FEMALE = re.compile(r"여자|여성|여(?![가-힣])")
TOTAL = re.compile(r"전체|합계|(?<![가-힣])계(?![가-힣])")

DDL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id     text PRIMARY KEY,
    status         text,
    family_id      text,
    slots          jsonb,
    asked          jsonb,
    clarify_turns  int NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
)
"""
UPSERT = """
INSERT INTO chat_sessions (session_id, status, family_id, slots, asked, clarify_turns)
VALUES (:session_id, :status, :family_id, CAST(:slots AS jsonb), CAST(:asked AS jsonb), :turns)
ON CONFLICT (session_id) DO UPDATE SET
    status = EXCLUDED.status, family_id = EXCLUDED.family_id, slots = EXCLUDED.slots,
    asked = EXCLUDED.asked, clarify_turns = EXCLUDED.clarify_turns, updated_at = now()
"""

_ready = False


# ---- 답 맞추기 (DB 없이 동작) -------------------------------------------------------


def pick_family(answer: str, options: list[dict[str, Any]]) -> str | None:
    """ambiguous 선택지에서 고른 패밀리: '2번', '②', '두 번째', 표 이름 일부"""
    m = NUMBER.match(answer)
    n = None
    if m:
        n = int(m.group(1)) if m.group(1) else "①②③④".index(m.group(2)) + 1
    for word, k in ORDINAL.items():
        if re.match(rf"\s*{word}\s*번째", answer):
            n = k
    if n is not None and n <= len(options):
        return options[n - 1]["family_id"]
    said = norm(answer)
    hits = [
        o["family_id"]
        for o in options
        if said and (said in norm(o["table_name"]) or norm(o["family_name"]) in said)
    ]
    return hits[0] if len(hits) == 1 else None


def match_member(answer: str, asked: list[dict[str, Any]]) -> dict[str, Any] | None:
    """clarify 선택지에서 고른 값 → {target, sex, edition, year}. 아무것도 못 맞추면 None"""
    options = {a["axis"]: a["options"] for a in asked}
    said = norm(answer)
    picked: dict[str, Any] = {}

    targets = [o for o in options.get("target", []) if o != "전체" and norm(o) in said]
    if targets:
        picked["target"] = max(targets, key=len)
    if "sex" in options:
        male, female = bool(MALE.search(answer)), bool(FEMALE.search(answer))
        if male != female:
            picked["sex"] = "남" if male else "여"
    edition = EDITION.search(answer)
    if edition and f"{edition.group(1)}~{edition.group(2)}" in options.get("edition", []):
        picked["edition"] = f"{edition.group(1)}~{edition.group(2)}"
    elif years := YEAR.findall(answer):
        if len(years) == 1 and years[0] in options.get("edition", []) and "년판" in answer:
            picked["edition"] = years[0]
        else:
            picked["year"] = int(years[0])
    # '전체'는 아직 안 정해진 대상·성별 모두에 쓴다 ('전체로 보여줘')
    if TOTAL.search(answer):
        if "target" in options and "target" not in picked:
            picked["target"] = "전체"
        if "sex" in options and "sex" not in picked:
            picked["sex"] = "전체"
    if ANYTHING.search(answer):
        picked["anything"] = True
    return picked or None


def apply_answer(slots: dict[str, Any], picked: dict[str, Any], last_turn: bool) -> dict[str, Any]:
    """저장된 슬롯에 답을 합친다. '아무거나'거나 마지막 턴이면 빠진 대상은 기본값(전체)"""
    slots = copy.deepcopy(slots)
    c = slots["conditions"]
    if "target" in picked:
        slots["target"] = picked["target"]
    if "sex" in picked:
        c["sex"] = picked["sex"]
    if "year" in picked:
        c["year"], c["year_end"] = picked["year"], None
    if "edition" in picked:
        slots["edition"] = picked["edition"]
    if "tbl_id" in picked:
        slots["tbl_id"] = picked["tbl_id"]
    if (picked.get("anything") or last_turn) and "target" not in picked:
        slots["target"] = "전체"
        slots["target_default"] = True  # 답변에 '대상을 지정하지 않아 전체' 고지
    return slots


def follow_up(
    tables: list[dict[str, Any]],
    state: dict[str, Any],
    answer: str,
    selections: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """직전 되묻기에 대한 답이면 결과, 새 질문이면 None"""
    selections = {k: v for k, v in (selections or {}).items() if v}
    turns = state["clarify_turns"]
    last_turn = turns + 1 >= MAX_CLARIFY_TURNS
    slots = state["slots"]

    if state["status"] == "ambiguous":
        options = state["asked"]
        family_id = selections.get("family_id") or pick_family(answer, options)
        if family_id is None and ANYTHING.search(answer):
            family_id = options[0]["family_id"]  # 1순위
        if family_id is not None:
            return search.resolve(tables, slots, family_id, allow_clarify=not last_turn)
        # '2010년', '여자'처럼 조건만 답하면 앞 질문에 조건을 더해 다시 검색한다
        picked = match_member(answer, [{"axis": "sex", "options": ["전체", "남", "여"]}])
        if picked and set(picked) <= {"sex", "year"}:
            merged = apply_answer(slots, picked, last_turn=False)
            return search.find(tables, merged, allow_clarify=not last_turn)
        return None

    if state["status"] == "clarify":
        picked = match_member(answer, state["asked"]) or {}
        picked.update({k: selections[k] for k in ("target", "sex") if k in selections})
        if "edition" in selections:
            picked["tbl_id"] = selections["edition"]
        if not picked:
            return None
        merged = apply_answer(slots, picked, last_turn)
        return search.resolve(tables, merged, state["family_id"], allow_clarify=not last_turn)
    return None


# ---- 세션 저장 -------------------------------------------------------------------


async def load_state(session_id: str) -> dict[str, Any] | None:
    from app.core.db import engine

    async with engine.begin() as conn:
        await _ensure(conn)
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM chat_sessions WHERE session_id = :id"), {"id": session_id}
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def save_state(session_id: str, result: dict[str, Any], turns: int) -> None:
    from app.core.db import engine

    asked = result.get("clarify_axes") if result["status"] == "clarify" else result.get("options")
    async with engine.begin() as conn:
        await _ensure(conn)
        await conn.execute(
            text(UPSERT),
            {
                "session_id": session_id,
                "status": result["status"],
                "family_id": (result.get("family") or {}).get("family_id"),
                "slots": json.dumps(result["slots"], ensure_ascii=False),
                "asked": json.dumps(asked, ensure_ascii=False) if asked else None,
                "turns": turns,
            },
        )


async def _ensure(conn: Any) -> None:
    global _ready
    if not _ready:
        await conn.execute(text(DDL))
        _ready = True


async def handle(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """한 턴 처리: 되묻기 답이면 이어서, 아니면 새 검색. 결과의 turn에 몇 번째 되묻기인지 남긴다"""
    state = await load_state(session_id)
    result = None
    turns = 0
    if state and state["status"] in ("clarify", "ambiguous"):
        result = follow_up(await search.load_tables(), state, question, selections)
        if result is not None:
            turns = state["clarify_turns"] + 1
            if trace is not None:
                trace["follow_up"] = True
    if result is None:
        result = await search.search(question, trace)
    waiting = result["status"] in ("clarify", "ambiguous")
    await save_state(session_id, result, turns if waiting else 0)
    result["turn"] = turns
    return result
