"""CLOVA Studio 임베딩 호출 (실전 2 F-02·F-04)

HCX와 같은 키(HCX_API_KEY)와 주소를 쓴다. 모델 3개 모두 1024차원 (2026-10-30 확인).
  v2                임베딩 v2
  clir-emb-dolphin  검색용
  clir-sts-dolphin  문장 유사도용
호출 제한: 모델마다 분당 60건 (응답 헤더 x-ratelimit-limit-requests, 2026-10-30 확인).
걸리면(42901) 헤더의 x-ratelimit-reset-requests('24s')만큼 기다렸다가 다시 시도한다.
표 2,364개를 임베딩하면 모델 하나에 약 40분 걸린다.
입력 한도는 모델마다 다르다 (넘으면 40003 Context length exceeded).
"""

import asyncio
import re
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings
from app.services.hcx_client import RETRY_WAITS, SUCCESS_CODE, HcxError

MODELS = ("v2", "clir-emb-dolphin", "clir-sts-dolphin")
DEFAULT_MODEL = "v2"
CONCURRENCY = 10
MAX_RETRIES = 5


async def _embed_one(
    client: httpx.AsyncClient, text: str, model: str, max_retries: int = MAX_RETRIES
) -> dict[str, Any]:
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.hcx_api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid4().hex,
    }
    for attempt in range(max_retries + 1):
        response = await client.post(
            f"{settings.hcx_base_url}/v1/api-tools/embedding/{model}",
            headers=headers,
            json={"text": text},
        )
        data = response.json()
        status = data.get("status") or {}
        if status.get("code") == SUCCESS_CODE:
            return data["result"]
        error = HcxError(
            str(status.get("code", response.status_code)),
            status.get("message", response.text),
            response.status_code,
        )
        if not error.is_rate_limit or attempt == max_retries:
            raise error
        await asyncio.sleep(reset_seconds(response.headers, attempt))
    raise AssertionError("unreachable")


def reset_seconds(headers: httpx.Headers, attempt: int) -> float:
    """호출 제한이 풀리는 시각까지 기다릴 초 ('24s' → 25). 헤더가 없으면 hcx_client 간격"""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", headers.get("x-ratelimit-reset-requests", ""))
    if match:
        return float(match.group(1)) + 1
    return RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]


async def rate_limit(model: str = DEFAULT_MODEL) -> dict[str, str]:
    """호출 한 번 하고 응답 헤더의 호출 제한 값을 돌려준다"""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.hcx_base_url}/v1/api-tools/embedding/{model}",
            headers={
                "Authorization": f"Bearer {settings.hcx_api_key}",
                "X-NCP-CLOVASTUDIO-REQUEST-ID": uuid4().hex,
            },
            json={"text": "호출 제한 확인"},
        )
    return {k: v for k, v in response.headers.items() if k.startswith("x-ratelimit")}


async def embed(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    """문장 목록 → 벡터 목록 (입력 순서 유지, 동시에 최대 CONCURRENCY건)"""
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60) as client:

        async def one(text: str) -> list[float]:
            async with gate:
                return (await _embed_one(client, text, model))["embedding"]

        return await asyncio.gather(*(one(t) for t in texts))


async def embed_once(text: str, model: str = DEFAULT_MODEL, timeout: float = 3.0) -> list[float]:
    """문장 하나를 다시 시도 없이 임베딩 (서비스의 질문 임베딩용).

    embed는 색인용이라 호출 제한에 걸리면 25초씩 최대 5번 기다린다. 질문마다 그렇게 기다리면
    /search가 멈추므로, 여기서는 한 번만 부르고 실패하면 바로 HcxError·타임아웃을 낸다
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        result = await asyncio.wait_for(_embed_one(client, text, model, max_retries=0), timeout)
    return result["embedding"]


async def embed_with_usage(text: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """문장 하나 → {"embedding": [...], "inputTokens": n} (토큰 수 확인용)"""
    async with httpx.AsyncClient(timeout=60) as client:
        return await _embed_one(client, text, model)
