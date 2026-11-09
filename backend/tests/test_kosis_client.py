"""KOSIS 오류 메시지에 API 키가 남지 않는다 (요청 주소에 apiKey가 들어 있음)"""

import asyncio

import httpx
import pytest

from app.services import kosis_client
from app.services.kosis_client import KosisClient, mask_key


def test_mask_key() -> None:
    assert mask_key("a.do?apiKey=abc%3D&b=1") == "a.do?apiKey=***&b=1"
    assert mask_key("끝 apiKey=abc") == "끝 apiKey=***"
    assert mask_key("키 없음") == "키 없음"


def test_http_error_message_hides_key(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(429))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        kosis_client.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )
    client = KosisClient(api_key="SECRET123", base_url="https://kosis.kr/openapi")
    with pytest.raises(httpx.HTTPStatusError) as e:
        asyncio.run(client.get("/x.do"))
    assert "SECRET123" not in str(e.value)
    assert "apiKey=***" in str(e.value)
    assert e.value.response.status_code == 429
