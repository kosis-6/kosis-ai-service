"""통계표 찾기의 LLM 호출 창구: 설정 SEARCH_LLM 으로 HCX-007(기본) / Gemma 4 31B 를 고른다

조건 추출(slots) · 계획(agent/plan) · 최종 검토(llm_pick)가 여기의 chat() 을 부른다.
부르는 쪽은 HcxClient.chat 과 같은 모양(model, messages, **options)으로 부르고, Gemma 일 때는
GemmaClient 가 옵션 이름을 OpenAI 호환 형식으로 바꿔 보낸다.

Gemma 로 바꾸는 법 (.env):
  SEARCH_LLM=gemma
  GEMMA_BASE_URL=http://<서버>:8000/v1     vLLM 등 OpenAI 호환 서버
  GEMMA_API_KEY=...                        서버가 키를 받지 않으면 생략
  GEMMA_MODEL=gemma-4-31b                  서버가 띄운 모델 이름

프롬프트와 검토 방식도 SEARCH_LLM 을 따라간다 (HCX 는 지금 그대로, Gemma 는 11/15 에 잰 조합):
  app/prompts/{search,plan,llm_pick}.py 의 *_GEMMA, llm_pick 의 pick_decided 모드 · temperature 0

수치 답변(qa) · 연속 대화(ask_session)는 여기를 거치지 않고 HCX-007 을 그대로 쓴다.
"""

import asyncio
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.hcx_client import ChatResult, HcxClient, HcxError

# 서버가 잠깐 못 받을 때(429 · 5xx · 연결 실패) 기다렸다가 다시 시도하는 간격 (초)
RETRY_WAITS = (1.0, 3.0, 6.0)
DEFAULT_MAX_TOKENS = 1024


def provider() -> str:
    return get_settings().search_llm


def is_gemma() -> bool:
    return provider() == "gemma"


async def chat(model: str, messages: list[dict[str, Any]], **options: Any) -> ChatResult:
    """통계표 찾기의 LLM 호출. model 은 HCX 모델 이름이고 Gemma 일 때는 GEMMA_MODEL 을 쓴다"""
    if is_gemma():
        return await GemmaClient().chat(model, messages, **options)
    return await HcxClient().chat(model, messages, **options)


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
            raise HcxError(
                "gemma_not_configured", "SEARCH_LLM=gemma 인데 GEMMA_BASE_URL 이 없습니다"
            )
        body = to_openai_body(self.model, messages, **options)
        for wait in (*self.retry_waits, None):
            try:
                return await self._chat_once(body)
            except HcxError as e:
                if not _retryable(e) or wait is None:
                    raise
            except httpx.TransportError as e:  # 연결 실패 · 시간 초과
                if wait is None:
                    raise HcxError("gemma_unreachable", f"{type(e).__name__}: {e}") from e
            await asyncio.sleep(wait)

    async def _chat_once(self, body: dict[str, Any]) -> ChatResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
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
