import logging
import time

from fastapi import APIRouter, HTTPException

from app.api.deps import OptionalUser
from app.core.stats_db import StatsNotLoadedError
from app.schemas.ask import AskRequest, AskResponse
from app.services import qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["ask"])
logger = logging.getLogger(__name__)


@router.post("/ask")
async def ask(body: AskRequest, user: OptionalUser) -> AskResponse:
    """로그인 없이도 쓸 수 있다. 로그인하면 기록에 사용자 id가 남는다"""
    start = time.perf_counter()
    res, error = None, None
    try:
        res = await qa.ask(body.question)
    except StatsNotLoadedError as e:
        error = f"StatsNotLoadedError: {e}"
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HcxError as e:
        error = f"HcxError: {e}"
        if e.is_rate_limit:  # hcx_client가 다시 시도해도 계속 걸린 경우
            raise HTTPException(
                status_code=429, detail="요청이 많아 잠시 후 다시 질문해 주세요."
            ) from e
        raise HTTPException(status_code=502, detail=f"HCX 호출 실패: {e}") from e
    except Exception as e:  # 예상 못 한 오류도 기록은 남긴다
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        # 평가 스크립트(qa.ask 직접 호출)는 기록하지 않고, 실제 사용자 질문만 남긴다
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        try:
            qa.log_request(body.question, res, error, elapsed_ms, user_id=user.id if user else None)
            if res is not None:
                qa.log_missing(body.question, res)
        except OSError:  # 기록 실패가 답변을 막지 않게
            logger.exception("요청 기록 실패")
    return AskResponse(**res)
