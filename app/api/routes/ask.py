import logging

from fastapi import APIRouter, HTTPException

from app.core.duckdb import DuckDBNotFoundError
from app.schemas.ask import AskRequest, AskResponse
from app.services import qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["ask"])
logger = logging.getLogger(__name__)


@router.post("/ask")
async def ask(body: AskRequest) -> AskResponse:
    try:
        res = await qa.ask(body.question)
    except DuckDBNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HcxError as e:
        if e.is_rate_limit:  # hcx_client가 다시 시도해도 계속 걸린 경우
            raise HTTPException(
                status_code=429, detail="요청이 많아 잠시 후 다시 질문해 주세요."
            ) from e
        raise HTTPException(status_code=502, detail=f"HCX 호출 실패: {e}") from e
    try:
        # 평가 스크립트(qa.ask 직접 호출)는 기록하지 않고, 실제 사용자 질문만 남긴다
        qa.log_missing(body.question, res)
    except OSError:  # 기록 실패가 답변을 막지 않게
        logger.exception("요청 기록 실패")
    return AskResponse(**res)
