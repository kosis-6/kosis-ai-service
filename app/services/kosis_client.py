from typing import Any

import httpx

from app.core.config import get_settings


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
            response.raise_for_status()
            return response.json()
