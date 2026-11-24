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
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", headers.get("x-ratelimit-reset-requests", ""))
    if match:
        return float(match.group(1)) + 1
    return RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]


async def rate_limit(model: str = DEFAULT_MODEL) -> dict[str, str]:
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
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=60) as client:

        async def one(text: str) -> list[float]:
            async with gate:
                return (await _embed_one(client, text, model))["embedding"]

        return await asyncio.gather(*(one(t) for t in texts))


async def embed_once(text: str, model: str = DEFAULT_MODEL, timeout: float = 3.0) -> list[float]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        result = await asyncio.wait_for(_embed_one(client, text, model, max_retries=0), timeout)
    return result["embedding"]


async def embed_with_usage(text: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        return await _embed_one(client, text, model)
