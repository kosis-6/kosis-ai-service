import copy
import json
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.services import search
from app.services.log_retention import KEEP_DAYS
from app.services.search import norm

MAX_CLARIFY_TURNS = 2
REJECTED = ("out_of_scope", "not_statistics")
ANYTHING = re.compile(r"아무거나|아무\s*거나|상관\s*없|기본(?:값|으로)?|다\s*괜찮|알아서")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
EDITION = re.compile(r"((?:19|20)\d{2})\s*~\s*((?:19|20)\d{2})")
NUMBER = re.compile(r"^\s*(?:([1-4])(?!\d)\s*번?|([①②③④]))")
ORDINAL = {"첫": 1, "두": 2, "세": 3, "네": 4}
MALE = re.compile(r"남자|남성|남(?![가-힣])")
FEMALE = re.compile(r"여자|여성|여(?![가-힣])")
TOTAL = re.compile(r"전체|합계|(?<![가-힣])계(?![가-힣])")

DDL = """
CREATE TABLE IF NOT EXISTS app.chat_sessions (
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
INSERT INTO app.chat_sessions (session_id, status, family_id, slots, asked, clarify_turns)
VALUES (:session_id, :status, :family_id, CAST(:slots AS jsonb), CAST(:asked AS jsonb), :turns)
ON CONFLICT (session_id) DO UPDATE SET
    status = EXCLUDED.status, family_id = EXCLUDED.family_id, slots = EXCLUDED.slots,
    asked = EXCLUDED.asked, clarify_turns = EXCLUDED.clarify_turns, updated_at = now()
"""

_ready = False


def pick_family(answer: str, options: list[dict[str, Any]]) -> str | None:
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
    options = {a["axis"]: a["options"] for a in asked}
    said = norm(answer)
    picked: dict[str, Any] = {}

    targets = search.mentioned_targets(options.get("target", []), said)
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
    if TOTAL.search(answer):
        if "target" in options and "target" not in picked:
            picked["target"] = "전체"
        if "sex" in options and "sex" not in picked:
            picked["sex"] = "전체"
    if ANYTHING.search(answer):
        picked["anything"] = True
    return picked or None


def apply_answer(slots: dict[str, Any], picked: dict[str, Any], last_turn: bool) -> dict[str, Any]:
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
        slots["target_default"] = True
    return slots


def follow_up(
    tables: list[dict[str, Any]],
    state: dict[str, Any],
    answer: str,
    selections: dict[str, Any] | None = None,
    dense: list[float] | None = None,
) -> dict[str, Any] | None:
    selections = {k: v for k, v in (selections or {}).items() if v}
    turns = state["clarify_turns"]
    last_turn = turns + 1 >= MAX_CLARIFY_TURNS
    slots = state["slots"]

    if state["status"] == "ambiguous":
        options = state["asked"]
        family_id = selections.get("family_id") or pick_family(answer, options)
        if family_id is None and ANYTHING.search(answer):
            family_id = options[0]["family_id"]
        if family_id is not None:
            return search.resolve(tables, slots, family_id, allow_clarify=not last_turn)
        picked = match_member(answer, [{"axis": "sex", "options": ["전체", "남", "여"]}])
        if picked and set(picked) <= {"sex", "year"}:
            merged = apply_answer(slots, picked, last_turn=False)
            return search.find(tables, merged, allow_clarify=not last_turn, dense=dense)
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


async def load_state(session_id: str) -> dict[str, Any] | None:
    from app.core.db import engine

    async with engine.begin() as conn:
        await _ensure(conn)
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM app.chat_sessions WHERE session_id = :id"
                        " AND updated_at >= now() - make_interval(days => :days)"
                    ),
                    {"id": session_id, "days": KEEP_DAYS},
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
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
        await conn.execute(text(DDL))
        _ready = True


OWNER_KEY = "_session_user_id"


async def owned_session(session_id: str, user_id: int | None) -> tuple[str, dict | None]:
    state = await load_state(session_id)
    if state is not None:
        slots = state.get("slots") or {}
        if OWNER_KEY not in slots or slots[OWNER_KEY] != user_id:
            return f"S_{uuid4().hex[:12]}", None
    return session_id, state


def owned_result(result: dict, user_id: int | None) -> dict:
    return {**result, "slots": {**result["slots"], OWNER_KEY: user_id}}


async def handle(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    session_id, state = await owned_session(session_id, user_id)
    if trace is not None:
        trace["search_session_id"] = session_id
    result = None
    turns = 0
    if state and state["status"] in ("clarify", "ambiguous"):
        dense = None
        if state["status"] == "ambiguous":
            dense = await search.dense_scores(state["slots"]["question"])
        result = follow_up(await search.load_tables(), state, question, selections, dense)
        if result is not None:
            turns = state["clarify_turns"] + 1
            if trace is not None:
                trace["follow_up"] = True
    if result is None:
        result = await search.search(question, trace)
        if state and state["status"] in ("clarify", "ambiguous") and result["status"] in REJECTED:
            retry = await search.search(f"{state['slots']['question']} {question}", trace)
            if retry["status"] not in REJECTED:
                result = retry
                turns = state["clarify_turns"] + 1
                if trace is not None:
                    trace["follow_up_merged"] = True
    waiting = result["status"] in ("clarify", "ambiguous")
    await save_state(session_id, owned_result(result, user_id), turns if waiting else 0)
    result["turn"] = turns
    return result
