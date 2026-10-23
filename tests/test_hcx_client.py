import asyncio
import json

import httpx
import pytest

from app.services.hcx_client import HcxClient, HcxError


def make_client(handler) -> HcxClient:
    return HcxClient(
        api_key="test-key", base_url="https://hcx.test", transport=httpx.MockTransport(handler)
    )


def test_chat_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/chat-completions/HCX-DASH-002"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "user", "content": "안녕"}]
        assert body["temperature"] == 0.1
        return httpx.Response(
            200,
            json={
                "status": {"code": "20000", "message": "OK"},
                "result": {
                    "message": {"role": "assistant", "content": "안녕하세요"},
                    "finishReason": "stop",
                    "usage": {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8},
                },
            },
        )

    result = asyncio.run(
        make_client(handler).chat(
            "HCX-DASH-002", [{"role": "user", "content": "안녕"}], temperature=0.1
        )
    )
    assert result.content == "안녕하세요"
    assert result.finish_reason == "stop"
    assert result.usage["totalTokens"] == 8


def test_chat_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": {"code": "40100", "message": "Unauthorized"}})

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.code == "40100"
    assert exc.value.http_status == 401
