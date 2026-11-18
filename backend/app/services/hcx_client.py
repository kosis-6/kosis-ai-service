import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings

SUCCESS_CODE = "20000"
RATE_LIMIT_CODE = "42901"
NETWORK_CODE = "network"  # HCX에 연결하지 못함 (연결 거부, 응답 시간 초과 등)
# 호출 제한에 걸리면 기다렸다가 다시 시도하는 간격 (초). 다 실패하면 HcxError
RETRY_WAITS = (1.0, 3.0, 6.0)
# 연결 실패·HCX 서버 오류(5xx)는 잠깐의 끊김일 수 있어 짧게 한 번만 다시 시도한다
TRANSIENT_WAITS = (1.0,)

# 이벤트 루프마다 하나 (asyncio.Semaphore는 처음 쓴 루프에 묶인다. 테스트는 루프를 새로 만든다)
_gates: dict[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = {}


def _gate() -> asyncio.Semaphore | None:
    """HCX 동시 요청 수 제한 (설정 hcx_max_concurrency, 0이면 None = 제한 없음)"""
    limit = get_settings().hcx_max_concurrency
    if limit <= 0:
        return None
    loop = asyncio.get_running_loop()
    found = _gates.get(loop)
    if found is None or found[0] != limit:
        for old in [k for k in _gates if k.is_closed()]:
            del _gates[old]
        found = _gates[loop] = (limit, asyncio.Semaphore(limit))
    return found[1]


class HcxError(RuntimeError):
    """CLOVA Studio가 오류 상태 코드를 돌려주거나, 연결하지 못한 경우 (code "network")."""

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status

    @property
    def is_rate_limit(self) -> bool:
        return self.code == RATE_LIMIT_CODE or self.http_status == 429

    @property
    def is_transient(self) -> bool:
        """연결 실패·HCX 서버 오류: 잠시 뒤 다시 하면 될 수 있다"""
        return self.code == NETWORK_CODE or (self.http_status or 0) >= 500


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)


class HcxClient:
    """CLOVA Studio Chat Completions v3 호출 클라이언트."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_waits: tuple[float, ...] = RETRY_WAITS,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.hcx_api_key
        self.base_url = base_url or settings.hcx_base_url
        self.timeout = timeout
        # 테스트에서 실제 API 대신 가짜 응답을 넣을 때 사용
        self.transport = transport
        self.retry_waits = retry_waits

    async def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> ChatResult:
        """메시지를 보내고 답변을 받는다. options는 temperature, maxTokens 등 요청 본문 필드.

        호출 제한(42901, HTTP 429)에 걸리면 retry_waits 간격으로 다시 시도한다.
        연결 실패·서버 오류는 TRANSIENT_WAITS만큼 다시 시도한다. 다 실패하면 HcxError
        동시에 보내는 요청은 hcx_max_concurrency개까지 (기다리는 동안은 자리를 비운다)
        """
        limit_waits, transient_waits = list(self.retry_waits), list(TRANSIENT_WAITS)
        gate = _gate()
        while True:
            try:
                if gate is None:
                    return await self._chat_once(model, messages, **options)
                async with gate:
                    return await self._chat_once(model, messages, **options)
            except HcxError as e:
                waits = (
                    limit_waits if e.is_rate_limit else transient_waits if e.is_transient else []
                )
                if not waits:
                    raise
                await asyncio.sleep(waits.pop(0))

    async def _chat_once(
        self, model: str, messages: list[dict[str, Any]], **options: Any
    ) -> ChatResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid4().hex,
        }
        body = {"messages": messages, **options}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.post(
                    f"/v3/chat-completions/{model}", headers=headers, json=body
                )
        except httpx.TransportError as e:  # 연결 거부, 응답 시간 초과, 연결 끊김
            raise HcxError(NETWORK_CODE, f"HCX에 연결하지 못했습니다 ({type(e).__name__})") from e

        # 오류도 {"status": {"code": "...", "message": "..."}} 형태로 옴
        try:
            data = response.json()
        except ValueError:
            # 게이트웨이 오류처럼 JSON이 아닌 응답
            raise HcxError(
                str(response.status_code), response.reason_phrase, response.status_code
            ) from None
        status = data.get("status") or {}
        if response.is_error or status.get("code") != SUCCESS_CODE:
            raise HcxError(
                str(status.get("code", response.status_code)),
                status.get("message", response.text),
                response.status_code,
            )

        result = data["result"]
        return ChatResult(
            content=result["message"]["content"],
            finish_reason=result.get("finishReason"),
            usage=result.get("usage") or {},
        )
