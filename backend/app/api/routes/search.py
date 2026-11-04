import logging
import time
from uuid import uuid4

from fastapi import APIRouter

from app.schemas.search import SearchRequest, SearchResponse
from app.services import dialog, search, search_answer, search_log

router = APIRouter(tags=["search"])
logger = logging.getLogger(__name__)


@router.post("/search")
async def search_tables(body: SearchRequest) -> SearchResponse:
    """질문 → 통계표 안내 (실전 2). 실전 1의 POST /ask와는 따로 동작한다"""
    start = time.perf_counter()
    session_id = body.session_id or f"S_{uuid4().hex[:12]}"
    trace: dict = {}
    result, response, error = None, None, None
    try:
        result = await dialog.handle(body.question, session_id, body.selections, trace)
        tables = await search.load_tables()
        response = search_answer.respond(
            result, tables, session_id, body.top_k, trace, debug=body.debug
        )
    except Exception as e:  # 예상 못 한 오류도 기록은 남긴다
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        try:
            await search_log.write(
                body.question, session_id, result, response, trace, elapsed_ms, error
            )
        except Exception:  # 기록 실패가 답변을 막지 않게
            logger.exception("검색 요청 기록 실패")
    return SearchResponse(**response)
