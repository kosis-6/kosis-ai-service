import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings

SUCCESS_CODE = "20000"
RATE_LIMIT_CODE = "42901"
# 호출 제한에 걸리면 기다렸다가 다시 시도하는 간격 (초). 다 실패하면 HcxError
RETRY_WAITS = (1.0, 3.0, 6.0)


class HcxError(RuntimeError):
    """CLOVA Studio가 오류 상태 코드를 돌려준 경우."""

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status

    @property
    def is_rate_limit(self) -> bool:
        return self.code == RATE_LIMIT_CODE or self.http_status == 429


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
        """
        for wait in (*self.retry_waits, None):
            try:
                return await self._chat_once(model, messages, **options)
            except HcxError as e:
                if not e.is_rate_limit or wait is None:
                    raise
                await asyncio.sleep(wait)

    async def _chat_once(
        self, model: str, messages: list[dict[str, Any]], **options: Any
    ) -> ChatResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid4().hex,
        }
        body = {"messages": messages, **options}
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        ) as client:
            response = await client.post(
                f"/v3/chat-completions/{model}", headers=headers, json=body
            )

        # 오류도 {"status": {"code": "...", "message": "..."}} 형태로 옴
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise
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
