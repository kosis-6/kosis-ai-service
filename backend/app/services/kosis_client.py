import asyncio
import re
from typing import Any

import httpx

from app.core.config import get_settings

_API_KEY = re.compile(r"(apiKey=)[^&\s\"']+")
# API와 적재 워커가 같은 키를 쓴다. 각 프로세스에서 메타데이터와 수치 요청을 함께 센다.
_REQUEST_INTERVAL = 60 / 90
_request_lock = asyncio.Lock()
_last_request_at = 0.0


def mask_key(text: str) -> str:
    return _API_KEY.sub(r"\1***", text)


async def _pace_requests() -> None:
    global _last_request_at
    loop = asyncio.get_running_loop()
    async with _request_lock:
        wait = _last_request_at + _REQUEST_INTERVAL - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = loop.time()


class KosisClient:
    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, timeout: float = 30.0
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.kosis_api_key
        self.base_url = base_url or settings.kosis_base_url
        self.timeout = timeout

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = {"apiKey": self.api_key, "format": "json", "jsonVD": "Y", **(params or {})}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            await _pace_requests()
            response = await client.get(path, params=query)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise httpx.HTTPStatusError(
                    mask_key(str(e)), request=e.request, response=e.response
                ) from None
            return response.json()
