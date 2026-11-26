import logging
import time
from uuid import uuid4

from fastapi import APIRouter

from app.api.deps import OptionalUser
from app.schemas.search import SearchRequest, SearchResponse
from app.services import dialog, refusal, search, search_answer, search_log

router = APIRouter(tags=["통계표 검색"])
logger = logging.getLogger(__name__)


@router.post("/search", summary="통계표 안내")
async def search_tables(body: SearchRequest, user: OptionalUser) -> SearchResponse:
    start = time.perf_counter()
    session_id = body.session_id or f"S_{uuid4().hex[:12]}"
    trace: dict = {}
    result, response, error = None, None, None
    try:
        if refusal.instruction_request(body.question):
            response = {
                "session_id": session_id,
                "status": "not_statistics",
                "answer": refusal.INSTRUCTION_REFUSAL,
                "parsed": {},
                "clarify": None,
                "recommendations": [],
                "notice": None,
                "reason": "인구 분야 통계 질문이 아닙니다.",
            }
        else:
            result = await dialog.handle(
                body.question, session_id, body.selections, trace, user_id=user.id if user else None
            )
            session_id = trace.get("search_session_id", session_id)
            tables = await search.load_tables()
            response = search_answer.respond(
                result, tables, session_id, body.top_k, trace, debug=body.debug
            )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        session_id = trace.get("search_session_id", session_id)
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        try:
            await search_log.write(
                body.question,
                session_id,
                result,
                response,
                trace,
                elapsed_ms,
                error,
                user_id=user.id if user else None,
            )
        except Exception:
            logger.exception("검색 요청 기록 실패")
    return SearchResponse(**response)
