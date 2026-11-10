import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.security import AuthNotConfigured
from app.services import indicators, load_queue, search
from app.services.mailer import MailNotConfigured

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 통계표 검색 데이터(표 2천여 개 + 벡터)를 서버가 뜰 때 미리 읽어 첫 질문이 느려지지 않게 한다
    await search.warm_up()
    await _queue_indicator_tables()
    yield


async def _queue_indicator_tables() -> None:
    """지표 사전이 쓰는 표 중 적재 안 된 표를 적재 대기열에 넣는다 (지표만 등록하면 표가 적재되게).
    DB 문제로 실패해도 서버는 띄운다. 그 표를 묻는 질문이 오면 그때 다시 넣는다"""
    try:
        queued = await load_queue.enqueue_missing(indicators.spec_tables(), "indicator")
    except Exception:  # noqa: BLE001
        logger.exception("지표 사전 표를 적재 대기열에 넣지 못했습니다")
        return
    if queued:
        logger.info("적재 대기열에 넣은 지표 표 %d개: %s", len(queued), ", ".join(queued))


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.include_router(api_router)


@app.exception_handler(AuthNotConfigured)
@app.exception_handler(MailNotConfigured)
async def not_configured(request: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# 통계표 안내 채팅 화면 (frontend/, /chat, 목업은 /chat/?mock=1)
# API와 같은 주소로 서빙해 CORS가 필요 없다
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
app.mount("/chat", StaticFiles(directory=FRONTEND_DIR, html=True), name="chat")
