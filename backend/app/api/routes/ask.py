import logging
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import OptionalUser
from app.core.stats_db import StatsNotLoadedError
from app.schemas.ask import AskRequest, AskResponse
from app.services import ask_followup, qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["질문 답변"])
logger = logging.getLogger(__name__)


async def _follow_up(session_id: str | None, question: str) -> str:
    """연도를 되물은 뒤의 짧은 답("2000년이랑")이면 앞 질문과 합친 질문, 아니면 그대로"""
    if not session_id:
        return question
    try:
        pending = await ask_followup.load_pending(session_id)
    except SQLAlchemyError:
        logger.exception("대화 상태 읽기 실패")
        return question
    return (pending and ask_followup.merge(pending, question)) or question


async def _remember(session_id: str | None, res: dict) -> str | None:
    """되물었으면 질문을 남겨 다음 답과 합치고, 그 밖의 답이면 기다리던 질문을 끝낸다"""
    if res.get("status") != "missing_year" and not session_id:
        return None
    session_id = session_id or f"A_{uuid4().hex[:12]}"
    try:
        await ask_followup.save(session_id, res.get("status") or "", res["question"])
    except SQLAlchemyError:
        logger.exception("대화 상태 저장 실패")
    return session_id


@router.post("/ask", summary="질문에 답하기")
async def ask(body: AskRequest, user: OptionalUser) -> AskResponse:
    """로그인 없이도 쓸 수 있다. 로그인하면 기록에 사용자 id가 남는다

    연도 없이 과거만 말하면("과거와 최근 인구수 비교") status = missing_year로 연도를 되묻고
    session_id를 돌려준다. 그 session_id로 "2000년"처럼 답하면 앞 질문과 합쳐 답한다."""
    start = time.perf_counter()
    res, error = None, None
    question = await _follow_up(body.session_id, body.question)
    try:
        res = await qa.ask(question)
        res["question"] = question
        res["session_id"] = await _remember(body.session_id, res)
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
            qa.log_request(question, res, error, elapsed_ms, user_id=user.id if user else None)
            if res is not None:
                qa.log_missing(question, res)
        except OSError:  # 기록 실패가 답변을 막지 않게
            logger.exception("요청 기록 실패")
    return AskResponse(**res)
