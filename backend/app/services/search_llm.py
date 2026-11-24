import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import get_settings
from app.services import llm_calls
from app.services.hcx_client import ChatResult, HcxClient, HcxError

logger = logging.getLogger(__name__)

RETRY_WAITS = (1.0, 3.0, 6.0)
DEFAULT_MAX_TOKENS = 1024
CONNECT_TIMEOUT = 5.0
GEMMA_DOWN_SECONDS = 60.0
_down_until = 0.0

Request = Callable[[str], tuple[list[dict[str, Any]], dict[str, Any]]]


def provider() -> str:
    settings = get_settings()
    if settings.search_llm == "auto":
        return "gemma" if settings.gemma_base_url.strip() else "hcx"
    return settings.search_llm


def is_gemma() -> bool:
    return provider() == "gemma"


def falling_back() -> bool:
    return get_settings().search_llm == "auto" and is_gemma() and time.monotonic() < _down_until


async def ask(model: str, request: Request) -> tuple[ChatResult, str]:
    global _down_until
    if is_gemma():
        auto = get_settings().search_llm == "auto"
        if not (auto and time.monotonic() < _down_until):
            messages, options = request("gemma")
            client = GemmaClient(retry_waits=()) if auto else GemmaClient()
            try:
                return await client.chat(model, messages, **options), "gemma"
            except HcxError as e:
                if not auto:
                    raise
                _down_until = time.monotonic() + GEMMA_DOWN_SECONDS
                logger.warning(
                    "Gemma 응답 없음 → HCX-007 로 넘어감 (%d초 동안 Gemma 를 건너뜀): %s",
                    GEMMA_DOWN_SECONDS, e,
                )  # fmt: skip
    messages, options = request("hcx")
    return await HcxClient().chat(model, messages, **options), "hcx"


def _text(content: Any) -> Any:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def to_openai_body(model: str, messages: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": m["role"], "content": _text(m["content"])} for m in messages],
        "max_tokens": options.get("maxCompletionTokens")
        or options.get("maxTokens")
        or DEFAULT_MAX_TOKENS,
        "temperature": options.get("temperature", 0.1),
        "top_p": options.get("topP") or 0.8,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    response_format = options.get("responseFormat")
    if response_format and response_format.get("schema"):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "out", "schema": response_format["schema"]},
        }
    elif response_format:
        body["response_format"] = {"type": "json_object"}
    return body


class GemmaClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_waits: tuple[float, ...] = RETRY_WAITS,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemma_api_key
        self.base_url = (base_url or settings.gemma_base_url).rstrip("/")
        self.model = model or settings.gemma_model
        self.timeout = timeout or settings.gemma_timeout
        self.transport = transport
        self.retry_waits = retry_waits

    async def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> ChatResult:
        if not self.base_url:
            raise HcxError("gemma_not_configured", "GEMMA_BASE_URL 이 없습니다")
        body = to_openai_body(self.model, messages, **options)
        start, attempts, waited = time.perf_counter(), 0, 0.0

        def log(usage: dict | None = None, error: Exception | None = None) -> None:
            llm_calls.record(
                llm="gemma",
                model=self.model,
                ms=(time.perf_counter() - start) * 1000,
                options=options,
                attempts=attempts,
                waited_ms=waited * 1000,
                usage=usage,
                error=error,
            )

        for wait in (*self.retry_waits, None):
            attempts += 1
            try:
                result = await self._chat_once(body)
            except HcxError as e:
                if not _retryable(e) or wait is None:
                    log(error=e)
                    raise
            except httpx.TransportError as e:
                if wait is None:
                    error = HcxError("gemma_unreachable", f"{type(e).__name__}: {e}")
                    log(error=error)
                    raise error from e
            else:
                log(usage=result.usage)
                return result
            waited += wait
            await asyncio.sleep(wait)

    async def _chat_once(self, body: dict[str, Any]) -> ChatResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        timeout = httpx.Timeout(self.timeout, connect=CONNECT_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=body
            )
        if response.is_error:
            raise HcxError(
                f"gemma_{response.status_code}", response.text[:300], response.status_code
            )
        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise HcxError("gemma_bad_response", response.text[:300], response.status_code) from e
        usage = data.get("usage") or {}
        return ChatResult(
            content=(choice.get("message") or {}).get("content") or "",
            finish_reason=choice.get("finish_reason"),
            usage={
                "promptTokens": usage.get("prompt_tokens"),
                "completionTokens": usage.get("completion_tokens"),
                "totalTokens": usage.get("total_tokens"),
            },
        )


def _retryable(e: HcxError) -> bool:
    return e.http_status == 429 or (e.http_status or 0) >= 500
