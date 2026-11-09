import re
from typing import Any

import httpx

from app.core.config import get_settings

_API_KEY = re.compile(r"(apiKey=)[^&\s\"']+")


def mask_key(text: str) -> str:
    """요청 주소의 apiKey 값을 가린다. 오류 메시지·로그에 키가 남지 않게 한다"""
    return _API_KEY.sub(r"\1***", text)


class KosisClient:
    """KOSIS OpenAPI 호출 클라이언트."""

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
            response = await client.get(path, params=query)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # httpx 오류 메시지에는 요청 주소 전체(apiKey 포함)가 들어간다
                raise httpx.HTTPStatusError(
                    mask_key(str(e)), request=e.request, response=e.response
                ) from None
            return response.json()
