import logging
import math
import time

from fastapi import APIRouter, HTTPException

from app.api.deps import OptionalUser
from app.core.stats_db import StatsNotLoadedError
from app.schemas.ask import AskRequest, AskResponse
from app.services import ask_session, qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["질문 답변"])
logger = logging.getLogger(__name__)
STATS_UNAVAILABLE = "통계 자료를 잠시 불러오지 못했습니다. 잠시 후 다시 질문해 주세요."


@router.post("/ask", summary="질문에 답하기")
async def ask(body: AskRequest, user: OptionalUser) -> AskResponse:
    start = time.perf_counter()
    res, error = None, None
    question = body.question
    try:
        res = await ask_session.handle(
            body.session_id, question, user.id if user else None, body.selections
        )
        question = res.get("question") or question
    except StatsNotLoadedError as e:
        error = f"StatsNotLoadedError: {e}"
        raise HTTPException(status_code=503, detail=STATS_UNAVAILABLE) from e
    except HcxError as e:
        error = f"HcxError: {e}"
        if e.is_rate_limit:
            after = math.ceil(e.retry_after) if e.retry_after else None
            raise HTTPException(
                status_code=429,
                detail="요청이 많아 잠시 후 다시 질문해 주세요."
                + (f" (약 {after}초 뒤)" if after else ""),
                headers={"Retry-After": str(after)} if after else None,
            ) from e
        detail = (
            "질문을 해석하는 AI 서버에 잠시 연결하지 못했습니다. 잠시 후 다시 질문해 주세요."
            if e.is_transient
            else "질문을 해석하는 AI 서버가 응답하지 못했습니다. 잠시 후 다시 질문해 주세요."
        )
        raise HTTPException(status_code=502, detail=detail) from e
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        try:
            qa.log_request(question, res, error, elapsed_ms, user_id=user.id if user else None)
            if res is not None:
                qa.log_missing(question, res)
        except OSError:
            logger.exception("요청 기록 실패")
    return AskResponse(**res)
