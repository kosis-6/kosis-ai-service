import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.security import AuthNotConfigured
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.spa import SpaFiles
from app.services import indicators, load_queue, log_retention, search
from app.services.mailer import MailNotConfigured

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await search.warm_up()
    await _queue_indicator_tables()
    cleanup = asyncio.create_task(log_retention.run_forever())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup


async def _queue_indicator_tables() -> None:
    try:
        queued = await load_queue.enqueue_missing(indicators.spec_tables(), "indicator")
    except Exception:  # noqa: BLE001
        logger.exception("지표 사전 표를 적재 대기열에 넣지 못했습니다")
        return
    if queued:
        logger.info("적재 대기열에 넣은 지표 표 %d개: %s", len(queued), ", ".join(queued))


DESCRIPTION = """KOSIS 인구 통계를 자연어로 묻고 답하는 서비스의 API입니다.

로그인이 필요한 API는 오른쪽 위 **Authorize**를 눌러
이메일(username 칸)과 비밀번호를 넣으면 쓸 수 있습니다.
"""
TAGS = [
    {"name": "질문 답변", "description": "질문에 숫자와 출처로 답한다. 로그인은 선택"},
    {
        "name": "통계표 검색",
        "description": "질문에 맞는 KOSIS 통계표를 안내한다. 로그인은 선택",
    },
    {
        "name": "로그인·회원가입",
        "description": "가입 순서: ① 인증번호 받기 → ② 인증번호 확인 → ③ 회원가입. 아이디는 이메일",
    },
    {"name": "관리자", "description": "관리자 계정만 쓸 수 있다"},
    {"name": "서버 상태", "description": "서버·DB·검색 준비 상태 확인"},
]
RESPONSE_TEXT = {"Successful Response": "성공", "Validation Error": "입력값이 규칙에 맞지 않음"}

app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    openapi_tags=TAGS,
    debug=settings.debug,
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs else None,
    redoc_url="/redoc" if settings.api_docs else None,
    openapi_url="/openapi.json" if settings.api_docs else None,
)


def _korean_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    for methods in schema["paths"].values():
        for op in methods.values():
            for res in op.get("responses", {}).values():
                res["description"] = RESPONSE_TEXT.get(res["description"], res["description"])
    app.openapi_schema = schema
    return schema


app.openapi = _korean_openapi  # type: ignore[method-assign]
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(api_router)


@app.exception_handler(AuthNotConfigured)
@app.exception_handler(MailNotConfigured)
async def not_configured(request: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
app.mount("/chat", SpaFiles(directory=str(FRONTEND_DIR)), name="chat")


@app.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/chat/", status_code=302)
