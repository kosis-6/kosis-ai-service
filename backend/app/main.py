import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
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


# /docs(Swagger) 화면에 보이는 설명.
# 버튼(Try it out, Execute 등)은 Swagger 자체 글자라 바꾸지 않는다
DESCRIPTION = """KOSIS 인구 통계를 자연어로 묻고 답하는 서비스의 API입니다.

로그인이 필요한 API는 오른쪽 위 **Authorize**를 눌러
이메일(username 칸)과 비밀번호를 넣으면 쓸 수 있습니다.
"""
TAGS = [
    {"name": "질문 답변", "description": "질문에 숫자와 출처로 답한다 (실전 1). 로그인은 선택"},
    {
        "name": "통계표 검색",
        "description": "질문에 맞는 KOSIS 통계표를 안내한다 (실전 2). 로그인은 선택",
    },
    {
        "name": "로그인·회원가입",
        "description": "가입 순서: ① 인증번호 받기 → ② 인증번호 확인 → ③ 회원가입. 아이디는 이메일",
    },
    {"name": "관리자", "description": "관리자 계정만 쓸 수 있다"},
    {"name": "서버 상태", "description": "서버·DB·검색 준비 상태 확인"},
]
# FastAPI가 자동으로 넣는 영문 응답 설명을 한글로 바꾼다
RESPONSE_TEXT = {"Successful Response": "성공", "Validation Error": "입력값이 규칙에 맞지 않음"}

app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    openapi_tags=TAGS,
    debug=settings.debug,
    lifespan=lifespan,
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
app.include_router(api_router)


@app.exception_handler(AuthNotConfigured)
@app.exception_handler(MailNotConfigured)
async def not_configured(request: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# 통계표 안내 채팅 화면 (frontend/, /chat, 목업은 /chat/?mock=1)
# API와 같은 주소로 서빙해 CORS가 필요 없다
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
app.mount("/chat", StaticFiles(directory=FRONTEND_DIR, html=True), name="chat")
