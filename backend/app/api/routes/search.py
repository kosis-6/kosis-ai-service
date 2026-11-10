import logging
import time
from uuid import uuid4

from fastapi import APIRouter

from app.api.deps import OptionalUser
from app.schemas.search import SearchRequest, SearchResponse
from app.services import dialog, search, search_answer, search_log

router = APIRouter(tags=["통계표 검색"])
logger = logging.getLogger(__name__)


@router.post("/search", summary="통계표 안내")
async def search_tables(body: SearchRequest, user: OptionalUser) -> SearchResponse:
    """질문에 맞는 통계표를 안내한다. 고를 것이 있으면 선택지를 돌려준다.
    로그인 없이도 쓸 수 있고, 로그인하면 기록에 사용자 id가 남는다"""
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
                body.question,
                session_id,
                result,
                response,
                trace,
                elapsed_ms,
                error,
                user_id=user.id if user else None,
            )
        except Exception:  # 기록 실패가 답변을 막지 않게
            logger.exception("검색 요청 기록 실패")
    return SearchResponse(**response)
