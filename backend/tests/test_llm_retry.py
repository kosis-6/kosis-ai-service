"""HCX 호출 제한 재시도 (app/services/hcx_client.py): 그 호출만, 풀리는 시각만큼, 예산 안에서"""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import hcx_client, llm_calls, qa
from app.services.hcx_client import HcxClient, HcxError, reset_after

MESSAGES = [{"role": "user", "content": "x"}]
OK = {
    "status": {"code": "20000", "message": "OK"},
    "result": {"message": {"role": "assistant", "content": "{}"}, "finishReason": "stop"},
}
LIMIT = {"status": {"code": "42901", "message": "Too many requests"}}


def limited(reset: str | None = None) -> httpx.Response:
    headers = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": reset}
    return httpx.Response(429, json=LIMIT, headers={k: v for k, v in headers.items() if v})


def client(handler, retry_waits=(0.0, 0.0)) -> HcxClient:
    return HcxClient(
        api_key="k",
        base_url="https://hcx.test",
        transport=httpx.MockTransport(handler),
        retry_waits=retry_waits,
    )


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setattr(get_settings(), "hcx_retry_budget_seconds", 5.0)
    hcx_client._cool_until.clear()


def test_reset_after_reads_the_exhausted_limit() -> None:
    both = {"x-ratelimit-reset-requests": "12s", "x-ratelimit-reset-tokens": "40s"}
    assert reset_after(httpx.Headers({**both, "x-ratelimit-remaining-requests": "0"})) == 12
    assert reset_after(httpx.Headers({**both, "x-ratelimit-remaining-requests": "7"})) == 40
    assert reset_after(httpx.Headers({**both, "retry-after": "3"})) == 3
    assert reset_after(httpx.Headers({})) is None


def test_waits_as_long_as_the_header_says_then_succeeds() -> None:
    sent = []

    def handler(request):
        sent.append(time.monotonic())
        return limited("0.2s") if len(sent) == 1 else httpx.Response(200, json=OK)

    async def go():
        calls = llm_calls.start()
        await client(handler).chat("HCX-007", MESSAGES)
        return calls

    (row,) = asyncio.run(go())
    assert sent[1] - sent[0] >= 0.19  # 헤더의 0.2초를 기다렸다
    assert row["attempts"] == 2 and row["outcome"] == "ok" and row["waited_ms"] >= 190


def test_without_header_backs_off_with_retry_waits(monkeypatch) -> None:
    monkeypatch.setattr(hcx_client.random, "uniform", lambda a, b: b)  # 최대 20% 더
    sent = []

    def handler(request):
        sent.append(time.monotonic())
        return limited() if len(sent) == 1 else httpx.Response(200, json=OK)

    asyncio.run(client(handler, retry_waits=(0.1,)).chat("HCX-007", MESSAGES))
    assert sent[1] - sent[0] >= 0.11


def test_gives_up_at_once_when_the_wait_is_over_budget() -> None:
    sent = []

    def handler(request):
        sent.append(1)
        return limited("30s")

    async def go():
        calls = llm_calls.start()
        start = time.monotonic()
        with pytest.raises(HcxError) as e:
            await client(handler).chat("HCX-007", MESSAGES)
        return calls, e.value, time.monotonic() - start

    calls, error, took = asyncio.run(go())
    assert error.is_rate_limit and error.retry_after == 30
    assert len(sent) == 1 and took < 1  # 30초를 기다리지 않고 바로 알린다
    assert calls[0]["outcome"] == "rate_limit" and calls[0]["attempts"] == 1


def test_other_calls_wait_for_the_same_limit() -> None:
    """한 호출이 걸리면 다른 호출도 풀릴 때까지 보내지 않는다 (같이 다시 몰려 또 걸리지 않게)"""
    log: list[tuple[str, float]] = []

    def handler(request):
        who = json.loads(request.content)["messages"][0]["content"]
        log.append((who, time.monotonic()))
        if who == "A" and len(log) == 1:
            return limited("0.3s")
        return httpx.Response(200, json=OK)

    async def one(who: str, delay: float):
        await asyncio.sleep(delay)
        await client(handler).chat("HCX-007", [{"role": "user", "content": who}])

    async def both():
        await asyncio.gather(one("A", 0), one("B", 0.05))

    asyncio.run(both())
    first_limit = log[0][1]
    b_sent = next(t for w, t in log if w == "B")
    assert (
        b_sent - first_limit >= 0.25
    )  # B는 0.05초에 보낼 수 있었지만 A가 걸린 제한이 풀릴 때까지 기다림


def test_gives_up_without_sending_while_another_call_waits_too_long() -> None:
    sent = []

    async def go():
        calls = llm_calls.start()
        hcx_client._cool_down(60)
        with pytest.raises(HcxError) as e:
            await client(lambda r: sent.append(1) or httpx.Response(200, json=OK)).chat(
                "HCX-007", MESSAGES
            )
        return calls, e.value

    calls, error = asyncio.run(go())
    assert sent == [] and error.is_rate_limit and 59 < error.retry_after <= 60
    assert calls[0]["attempts"] == 0


def test_api_tells_how_long_to_wait(monkeypatch) -> None:
    async def fail(question: str, adjust=None) -> dict:
        raise HcxError("42901", "Too many requests", 429, retry_after=23.2)

    monkeypatch.setattr(qa, "ask", fail)
    r = TestClient(app).post("/ask", json={"question": "서울 인구"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "24"
    assert "약 24초 뒤" in r.json()["detail"]
