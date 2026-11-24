import asyncio
import json

import httpx
import pytest

from app.services.hcx_client import HcxClient, HcxError

OK_BODY = {
    "status": {"code": "20000", "message": "OK"},
    "result": {"message": {"role": "assistant", "content": "안녕하세요"}, "finishReason": "stop"},
}
RATE_LIMIT_BODY = {"status": {"code": "42901", "message": "Too many requests"}}


def make_client(handler, retry_waits: tuple[float, ...] = (0, 0)) -> HcxClient:
    return HcxClient(
        api_key="test-key",
        base_url="https://hcx.test",
        transport=httpx.MockTransport(handler),
        retry_waits=retry_waits,
    )


def test_chat_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/chat-completions/HCX-007"
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
        make_client(handler).chat("HCX-007", [{"role": "user", "content": "안녕"}], temperature=0.1)
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


def test_chat_retries_rate_limit() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, json=RATE_LIMIT_BODY)
        return httpx.Response(200, json=OK_BODY)

    result = asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert result.content == "안녕하세요"
    assert len(calls) == 3


def test_chat_rate_limit_gives_up() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json=RATE_LIMIT_BODY)

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.is_rate_limit
    assert len(calls) == 3


def test_chat_no_retry_on_other_errors() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"status": {"code": "40100", "message": "Unauthorized"}})

    with pytest.raises(HcxError):
        asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert len(calls) == 1


def test_chat_network_error_retries_once_then_hcx_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.hcx_client.TRANSIENT_WAITS", (0,))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.is_transient and not exc.value.is_rate_limit
    assert len(calls) == 2


def test_chat_server_error_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.hcx_client.TRANSIENT_WAITS", (0,))
    responses = [
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.Response(200, json=OK_BODY),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    result = asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert result.content == "안녕하세요"


def test_chat_limits_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "hcx_max_concurrency", 2)
    running, peak = 0, 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return httpx.Response(200, json=OK_BODY)

    async def run() -> None:
        client = make_client(handler)
        messages = [{"role": "user", "content": "안녕"}]
        await asyncio.gather(*(client.chat("HCX-007", messages) for _ in range(6)))

    asyncio.run(run())
    assert peak == 2
