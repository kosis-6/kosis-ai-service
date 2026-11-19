"""통계표 찾기의 LLM 호출 창구: Gemma 서버 주소가 있으면 Gemma 4 31B, 없으면 HCX-007

조건 추출(slots) · 계획(agent/plan) · 최종 검토(llm_pick)가 여기의 ask() 를 부른다.
부르는 쪽은 "어느 LLM 이면 무엇을 보낼지"를 함수로 넘기고(프롬프트가 LLM 마다 다르다),
ask() 가 LLM 을 골라 부른 뒤 응답과 함께 어느 쪽이 답했는지를 돌려준다.
옵션은 HcxClient.chat 과 같은 이름으로 쓰고, Gemma 일 때는 GemmaClient 가 OpenAI 호환 형식으로
바꾼다.

Gemma 로 쓰는 법 (.env). 주소만 넣으면 된다:
  GEMMA_BASE_URL=http://<서버>:8000/v1     vLLM 등 OpenAI 호환 서버
  GEMMA_API_KEY=...                        서버가 키를 받지 않으면 생략
  GEMMA_MODEL=gemma-4-31b                  서버가 띄운 모델 이름
SEARCH_LLM 으로 고정할 수도 있다: auto(기본, 주소가 있으면 Gemma) / hcx / gemma

Gemma 가 응답하지 않을 때 (SEARCH_LLM=auto):
  그 호출은 바로 HCX-007 로 다시 보낸다 (HCX 프롬프트 · HCX 검토 방식으로).
  이어서 GEMMA_DOWN_SECONDS 동안은 Gemma 를 부르지 않고 HCX 로 가고, 그 뒤 다시 Gemma 를 불러 본다.
  SEARCH_LLM=gemma 로 고정하면 넘어가지 않고 실패를 그대로 올린다 (측정할 때 HCX 답이 섞이지 않게).

프롬프트와 검토 방식도 고른 LLM 을 따라간다 (HCX 는 지금 그대로, Gemma 는 11/15 에 잰 조합):
  app/prompts/{search,plan,llm_pick}.py 의 *_GEMMA, llm_pick 의 pick_decided 모드 · temperature 0

수치 답변(qa) · 연속 대화(ask_session)는 여기를 거치지 않고 HCX-007 을 그대로 쓴다.
"""

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

# 서버가 잠깐 못 받을 때(429 · 5xx · 연결 실패) 기다렸다가 다시 시도하는 간격 (초)
RETRY_WAITS = (1.0, 3.0, 6.0)
DEFAULT_MAX_TOKENS = 1024
# 서버에 붙는 데 이보다 오래 걸리면 꺼진 것으로 본다 (초). 응답을 기다리는 시간은 GEMMA_TIMEOUT
CONNECT_TIMEOUT = 5.0
# Gemma 가 실패한 뒤 이 시간(초) 동안은 Gemma 를 건너뛰고 HCX 로 간다.
# 꺼진 서버를 질문마다 다시 불러 기다리지 않게 하고, 서버가 돌아오면 저절로 Gemma 로 돌아간다
GEMMA_DOWN_SECONDS = 60.0
_down_until = 0.0  # time.monotonic() 기준. 이 시각까지는 Gemma 를 부르지 않는다

# 요청 만들기: 어느 LLM("hcx" · "gemma")이면 무엇을 보낼지 → (messages, options)
Request = Callable[[str], tuple[list[dict[str, Any]], dict[str, Any]]]


def provider() -> str:
    """통계표 찾기에 쓰도록 설정된 LLM: "hcx" 또는 "gemma".
    SEARCH_LLM=auto(기본)면 GEMMA_BASE_URL 이 있을 때 Gemma, 없으면 HCX.
    Gemma 가 잠시 응답하지 않아 HCX 로 넘어가 있는지는 falling_back() 으로 본다"""
    settings = get_settings()
    if settings.search_llm == "auto":
        return "gemma" if settings.gemma_base_url.strip() else "hcx"
    return settings.search_llm


def is_gemma() -> bool:
    return provider() == "gemma"


def falling_back() -> bool:
    """Gemma 가 응답하지 않아 지금은 HCX 로 넘어가 있는가 (SEARCH_LLM=auto 에서만)"""
    return (
        get_settings().search_llm == "auto" and is_gemma() and time.monotonic() < _down_until
    )


async def ask(model: str, request: Request) -> tuple[ChatResult, str]:
    """통계표 찾기의 LLM 호출 → (응답, 답한 LLM "hcx" · "gemma").

    request(llm) 은 그 LLM 에 보낼 (messages, options) 를 만든다. model 은 HCX 모델 이름이고
    Gemma 일 때는 GEMMA_MODEL 을 쓴다. SEARCH_LLM=auto 에서 Gemma 가 실패하면 HCX 로 다시 보낸다
    """
    global _down_until
    if is_gemma():
        auto = get_settings().search_llm == "auto"
        if not (auto and time.monotonic() < _down_until):
            messages, options = request("gemma")
            # 넘어갈 곳이 있으면(auto) 기다렸다 다시 부르지 않고 바로 HCX 로 간다
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
    """[{"type": "text", "text": ...}] 꼴의 content 를 문자열 하나로"""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def to_openai_body(model: str, messages: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    """HCX 형식 옵션 → OpenAI 호환 요청 본문. 생각(thinking)은 항상 끈다
    (11/15 실험실: 켜면 응답 41초, 정답 이득 없음). topK · repetitionPenalty 는 보내지 않는다"""
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
    """OpenAI 호환 Chat Completions 서버(vLLM)의 Gemma 호출 클라이언트.

    실패는 HcxError 로 올린다. 부르는 쪽(slots · multi · /ask)이 HcxError 하나만 잡고 있어서,
    Gemma 가 실패해도 HCX 가 실패했을 때와 같은 대체 경로(원문 규칙으로 검색 등)를 탄다.
    """

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
        # 테스트에서 실제 서버 대신 가짜 응답을 넣을 때 사용
        self.transport = transport
        self.retry_waits = retry_waits

    async def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> ChatResult:
        """HcxClient.chat 과 같은 모양. model(HCX 모델 이름)은 쓰지 않고 self.model 로 보낸다"""
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
            except httpx.TransportError as e:  # 연결 실패 · 시간 초과
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
            # 요청 로그가 HCX 와 같은 키로 읽도록 이름을 맞춘다
            usage={
                "promptTokens": usage.get("prompt_tokens"),
                "completionTokens": usage.get("completion_tokens"),
                "totalTokens": usage.get("total_tokens"),
            },
        )


def _retryable(e: HcxError) -> bool:
    return e.http_status == 429 or (e.http_status or 0) >= 500
