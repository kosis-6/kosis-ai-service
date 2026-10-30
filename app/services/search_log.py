"""통계표 검색 요청 로그 (실전 2 F-09) → PostgreSQL search_logs

질문, 슬롯, 후보 상위 10개와 점수, 최종 응답, 단계별 시간, 오류를 남긴다.
실패 사례 분석(F-12)에서 원인(슬롯 / 검색 / 재순위 / 패밀리·판 선택 / 판단)을 가를 때 쓴다.
테이블은 처음 기록할 때 만든다.
"""

import json
from typing import Any

from sqlalchemy import text

DDL = """
CREATE TABLE IF NOT EXISTS search_logs (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    session_id   text,
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
INSERT = """
INSERT INTO search_logs (session_id, question, status, tbl_id, slots, candidates, response,
                         hcx_raw, timings, elapsed_ms, error)
VALUES (:session_id, :question, :status, :tbl_id, CAST(:slots AS jsonb),
        CAST(:candidates AS jsonb), CAST(:response AS jsonb), CAST(:hcx_raw AS jsonb),
        CAST(:timings AS jsonb), :elapsed_ms, :error)
"""

_ready = False


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


async def write(
    question: str,
    session_id: str | None,
    result: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
) -> None:
    global _ready
    from app.core.db import engine

    trace = trace or {}
    recs = (response or {}).get("recommendations") or []
    async with engine.begin() as conn:
        if not _ready:
            await conn.execute(text(DDL))
            _ready = True
        await conn.execute(
            text(INSERT),
            {
                "session_id": session_id,
                "question": question,
                "status": (response or {}).get("status"),
                "tbl_id": recs[0]["tbl_id"] if recs else None,
                "slots": _json((result or {}).get("slots")),
                "candidates": _json((result or {}).get("candidates")),
                "response": _json(response),
                "hcx_raw": _json(trace.get("hcx_raw")),
                "timings": _json(trace.get("timings")),
                "elapsed_ms": elapsed_ms,
                "error": error,
            },
        )
