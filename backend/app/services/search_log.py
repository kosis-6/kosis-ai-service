import json
from typing import Any

from sqlalchemy import text

from app.services.log_redaction import mask, redact
from app.services.log_retention import KEEP_DAYS

DDL = """
CREATE TABLE IF NOT EXISTS app.search_logs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    session_id   text,
    user_id      bigint,
    question     text NOT NULL,
    status       text,
    tbl_id       text,
    slots        jsonb,
    candidates   jsonb,
    response     jsonb,
    hcx_raw      jsonb,
    timings      jsonb,
    elapsed_ms   int,
    error        text
)
"""
ADD_USER_ID = "ALTER TABLE app.search_logs ADD COLUMN IF NOT EXISTS user_id bigint"
INSERT = """
INSERT INTO app.search_logs (session_id, user_id, question, status, tbl_id, slots, candidates,
                             response, hcx_raw, timings, elapsed_ms, error)
VALUES (:session_id, :user_id, :question, :status, :tbl_id, CAST(:slots AS jsonb),
        CAST(:candidates AS jsonb), CAST(:response AS jsonb), CAST(:hcx_raw AS jsonb),
        CAST(:timings AS jsonb), :elapsed_ms, :error)
"""

PURGE = "DELETE FROM app.search_logs WHERE created_at < now() - make_interval(days => :days)"

_ready = False


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(redact(value), ensure_ascii=False, default=str)


async def write(
    question: str,
    session_id: str | None,
    result: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
    user_id: int | None = None,
) -> None:
    global _ready
    from app.core.db import engine

    trace = trace or {}
    recs = (response or {}).get("recommendations") or []
    async with engine.begin() as conn:
        if not _ready:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
            await conn.execute(text(DDL))
            await conn.execute(text(ADD_USER_ID))
            _ready = True
        await conn.execute(
            text(INSERT),
            {
                "session_id": session_id,
                "user_id": user_id,
                "question": mask(question),
                "status": (response or {}).get("status"),
                "tbl_id": recs[0]["tbl_id"] if recs else None,
                "slots": _json((result or {}).get("slots")),
                "candidates": _json((result or {}).get("candidates")),
                "response": _json(response),
                "hcx_raw": _json(trace.get("hcx_raw")),
                "timings": _json(trace.get("timings")),
                "elapsed_ms": elapsed_ms,
                "error": mask(error),
            },
        )
        await conn.execute(text(PURGE), {"days": KEEP_DAYS})
