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
NETWORK_CODE = "network"  # HCX에 연결하지 못함 (연결 거부, 응답 시간 초과 등)
# 호출 제한에 걸렸는데 응답 헤더에 풀리는 시각이 없을 때 기다리는 간격 (초, 최대 20% 더 쉼).
# 몇 번까지 다시 시도할지도 이 길이로 정한다. 헤더가 있으면 그 시각만큼 기다린다
RETRY_WAITS = (2.0, 4.0, 8.0, 16.0)
JITTER = 0.2  # 동시에 걸린 호출이 같은 순간에 다시 몰리지 않게
# 연결 실패·HCX 서버 오류(5xx)는 잠깐의 끊김일 수 있어 짧게 한 번만 다시 시도한다
TRANSIENT_WAITS = (1.0,)

# 이벤트 루프마다 하나 (asyncio.Semaphore는 처음 쓴 루프에 묶인다. 테스트는 루프를 새로 만든다)
_gates: dict[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = {}
# 호출 제한에 걸리면 "이 시각(time.monotonic)까지는 보내지 말 것"을 루프마다 적어 두고,
# 다른 호출도 보내기 전에 그때까지 기다린다 (각자 1초 뒤 한꺼번에 다시 보내 또 걸리지 않게)
_cool_until: dict[asyncio.AbstractEventLoop, float] = {}


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


def _cooldown_left() -> float:
    """다른 호출이 걸린 호출 제한이 풀릴 때까지 남은 초 (없으면 0)"""
    loop = asyncio.get_running_loop()
    return max(_cool_until.get(loop, 0.0) - time.monotonic(), 0.0)


def _cool_down(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    for old in [k for k in _cool_until if k.is_closed()]:
        del _cool_until[old]
    _cool_until[loop] = max(_cool_until.get(loop, 0.0), time.monotonic() + seconds)


def _seconds(value: str | None) -> float | None:
    """'24s' · '1.5s' · '24' → 24.0. 읽지 못하면 None"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", value or "")
    return float(match.group(1)) if match else None


def reset_after(headers: httpx.Headers) -> float | None:
    """호출 제한 응답의 헤더 → 다시 보내도 될 때까지의 초.
    HCX는 x-ratelimit-{remaining,reset}-{requests,tokens}를 준다
    (11/19 확인: 분당 60회 · 6만 토큰). 요청 수가 바닥났으면 요청 쪽, 아니면 토큰 쪽 풀리는 시각.
    Retry-After가 있으면 그것을 먼저 본다"""
    if (after := _seconds(headers.get("retry-after"))) is not None:
        return after
    kind = "requests" if headers.get("x-ratelimit-remaining-requests") == "0" else "tokens"
    found = _seconds(headers.get(f"x-ratelimit-reset-{kind}"))
    return found if found is not None else _seconds(headers.get("x-ratelimit-reset-requests"))


class HcxError(RuntimeError):
    """CLOVA Studio가 오류 상태 코드를 돌려주거나, 연결하지 못한 경우 (code "network").
    retry_after: 호출 제한이면 다시 보내도 될 때까지의 초
    (응답 헤더에서 읽었거나, 기다릴 수 있는 시간을 넘겨 포기한 경우)"""

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

        호출 제한(42901, HTTP 429)에 걸리면 이 호출만 다시 보낸다 (질문 전체를 다시 하지 않는다).
          - 응답 헤더의 풀리는 시각만큼 기다린다. 헤더가 없으면 retry_waits 간격(+ 최대 20%)
          - 그동안 다른 호출도 보내지 않고 같이 기다린다 (_cool_until)
          - 한 호출이 기다리는 시간의 합은 설정 hcx_retry_budget_seconds까지. 넘을 것 같으면 바로
            HcxError(retry_after = 풀리기까지 남은 초)를 낸다 (API는 429 + Retry-After로 알린다)
        연결 실패·서버 오류는 TRANSIENT_WAITS만큼 다시 시도한다. 다 실패하면 HcxError
        동시에 보내는 요청은 hcx_max_concurrency개까지 (기다리는 동안은 자리를 비운다)
        끝나면(성공 · 실패) 호출 한 줄을 llm_calls에 남긴다
        (다시 시도는 attempts, 쉰 시간은 waited_ms)
        """
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
            if (left := _cooldown_left()) > 0:  # 다른 호출이 걸린 호출 제한이 아직 안 풀림
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
                    continue  # 위에서 풀리는 시각까지 기다린다
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
