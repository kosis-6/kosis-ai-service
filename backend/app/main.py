from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.security import AuthNotConfigured
from app.services import search

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 통계표 검색 데이터(표 2천여 개 + 벡터)를 서버가 뜰 때 미리 읽어 첫 질문이 느려지지 않게 한다
    await search.warm_up()
    yield


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.include_router(api_router)


@app.exception_handler(AuthNotConfigured)
async def auth_not_configured(request: Request, exc: AuthNotConfigured) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# 통계표 안내 채팅 화면 (frontend/, /chat, 목업은 /chat/?mock=1)
# API와 같은 주소로 서빙해 CORS가 필요 없다
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
app.mount("/chat", StaticFiles(directory=FRONTEND_DIR, html=True), name="chat")
