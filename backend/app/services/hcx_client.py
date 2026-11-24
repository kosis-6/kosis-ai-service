import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings
from app.services import llm_calls

SUCCESS_CODE = "20000"
RATE_LIMIT_CODE = "42901"
NETWORK_CODE = "network"
RETRY_WAITS = (2.0, 4.0, 8.0, 16.0)
JITTER = 0.2
TRANSIENT_WAITS = (1.0,)

_gates: dict[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = {}
_cool_until: dict[asyncio.AbstractEventLoop, float] = {}


def _gate() -> asyncio.Semaphore | None:
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


def _cooldown_left() -> float:
    loop = asyncio.get_running_loop()
    return max(_cool_until.get(loop, 0.0) - time.monotonic(), 0.0)


def _cool_down(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    for old in [k for k in _cool_until if k.is_closed()]:
        del _cool_until[old]
    _cool_until[loop] = max(_cool_until.get(loop, 0.0), time.monotonic() + seconds)


def _seconds(value: str | None) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", value or "")
    return float(match.group(1)) if match else None


def reset_after(headers: httpx.Headers) -> float | None:
    if (after := _seconds(headers.get("retry-after"))) is not None:
        return after
    kind = "requests" if headers.get("x-ratelimit-remaining-requests") == "0" else "tokens"
    found = _seconds(headers.get(f"x-ratelimit-reset-{kind}"))
    return found if found is not None else _seconds(headers.get("x-ratelimit-reset-requests"))


class HcxError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after

    @property
    def is_rate_limit(self) -> bool:
        return self.code == RATE_LIMIT_CODE or self.http_status == 429

    @property
    def is_transient(self) -> bool:
        return self.code == NETWORK_CODE or (self.http_status or 0) >= 500


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)


class HcxClient:
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
        self.transport = transport
        self.retry_waits = retry_waits

    async def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> ChatResult:
        limit_waits, transient_waits = list(self.retry_waits), list(TRANSIENT_WAITS)
        budget = get_settings().hcx_retry_budget_seconds
        gate = _gate()
        start, attempts, waited = time.perf_counter(), 0, 0.0

        def log(usage: dict | None = None, error: Exception | None = None) -> None:
            llm_calls.record(
                llm="hcx",
                model=model,
                ms=(time.perf_counter() - start) * 1000,
                options=options,
                attempts=attempts,
                waited_ms=waited * 1000,
                usage=usage,
                error=error,
            )

        def give_up(left: float) -> HcxError:
            return HcxError(
                RATE_LIMIT_CODE, "호출 제한이 풀리기를 기다리다 포기함", 429, retry_after=left
            )

        while True:
            if (left := _cooldown_left()) > 0:
                if waited + left > budget:
                    error = give_up(left)
                    log(error=error)
                    raise error
                waited += left
                await asyncio.sleep(left)
            attempts += 1
            try:
                if gate is None:
                    result = await self._chat_once(model, messages, **options)
                else:
                    async with gate:
                        result = await self._chat_once(model, messages, **options)
            except HcxError as e:
                if e.is_rate_limit and limit_waits:
                    backoff = limit_waits.pop(0)
                    wait = e.retry_after
                    if wait is None:
                        wait = backoff * (1 + random.uniform(0, JITTER))
                    if waited + wait > budget:
                        e.retry_after = wait
                        log(error=e)
                        raise
                    _cool_down(wait)
                    continue
                if e.is_transient and transient_waits:
                    wait = transient_waits.pop(0)
                    waited += wait
                    await asyncio.sleep(wait)
                    continue
                log(error=e)
                raise
            else:
                log(usage=result.usage)
                return result

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
        except httpx.TransportError as e:
            raise HcxError(NETWORK_CODE, f"HCX에 연결하지 못했습니다 ({type(e).__name__})") from e

        try:
            data = response.json()
        except ValueError:
            raise HcxError(
                str(response.status_code), response.reason_phrase, response.status_code
            ) from None
        status = data.get("status") or {}
        if response.is_error or status.get("code") != SUCCESS_CODE:
            error = HcxError(
                str(status.get("code", response.status_code)),
                status.get("message", response.text),
                response.status_code,
            )
            if error.is_rate_limit:
                error.retry_after = reset_after(response.headers)
            raise error

        result = data["result"]
        return ChatResult(
            content=result["message"]["content"],
            finish_reason=result.get("finishReason"),
            usage=result.get("usage") or {},
        )
