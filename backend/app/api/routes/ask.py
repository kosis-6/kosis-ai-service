import logging
import time

from fastapi import APIRouter, HTTPException

from app.api.deps import OptionalUser
from app.core.stats_db import StatsNotLoadedError
from app.schemas.ask import AskRequest, AskResponse
from app.services import ask_session, qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["질문 답변"])
logger = logging.getLogger(__name__)


@router.post("/ask", summary="질문에 답하기")
async def ask(body: AskRequest, user: OptionalUser) -> AskResponse:
    """로그인 없이도 쓸 수 있다. 로그인하면 기록에 사용자 id가 남는다

    응답의 session_id를 다음 요청에 함께 보내면 대화를 이어 간다 (app/services/ask_session.py).
    - "서울도 같이", "2022년만", "남자만", "낮은 순으로": 바뀐 조건만 고쳐 다시 계산한다
    - "막대그래프로", "표로 보여줘", "요약해줘": 다시 계산하지 않고 직전 답으로 답한다
    - 새 질문은 말하지 않은 지역·기간을 앞 질문에서 이어받고, 이어받은 조건을 notice로 알린다
    - 수치로 답하면 답에 쓴 통계표를, 수치로 답할 수 없는 질문이면 통계표 찾기 결과를 tables로 준다
      (통계표 선택지에 답할 때는 selections를 함께 보낸다)
    - 연도·지역을 되물었을 때("과거와 최근 비교", "광주")는
      "2000년", "광주광역시"처럼 답하면 된다"""
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
