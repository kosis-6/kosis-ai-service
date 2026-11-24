import asyncio

import httpx
import pytest

from app.services import llm_calls, search_llm
from app.services.hcx_client import ChatResult, HcxClient, HcxError
from app.services.search_llm import GemmaClient
from scripts import eval_devset

MESSAGES = [{"role": "user", "content": "x"}]
HCX_OK = {
    "status": {"code": "20000", "message": "OK"},
    "result": {
        "message": {"role": "assistant", "content": "{}"},
        "finishReason": "stop",
        "usage": {"promptTokens": 120, "completionTokens": 30, "totalTokens": 150},
    },
}
HCX_LIMIT = {"status": {"code": "42901", "message": "Too many requests"}}
GEMMA_OK = {
    "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57},
}


def hcx(handler) -> HcxClient:
    return HcxClient(
        api_key="k",
        base_url="https://hcx.test",
        transport=httpx.MockTransport(handler),
        retry_waits=(0, 0),
    )


def gemma(handler) -> GemmaClient:
    return GemmaClient(
        api_key="k",
        base_url="http://gemma.test/v1",
        model="gemma-4-31b",
        transport=httpx.MockTransport(handler),
        retry_waits=(0, 0),
    )


def test_record_without_start_does_nothing() -> None:
    llm_calls._calls.set(None)
    asyncio.run(hcx(lambda r: httpx.Response(200, json=HCX_OK)).chat("HCX-007", MESSAGES))
    assert llm_calls.current() is None


def test_hcx_call_is_recorded_with_stage_tokens_and_reserved_length() -> None:
    async def go():
        calls = llm_calls.start()
        with llm_calls.stage("understand"):
            await hcx(lambda r: httpx.Response(200, json=HCX_OK)).chat(
                "HCX-007", MESSAGES, maxCompletionTokens=1024
            )
        return calls

    (row,) = asyncio.run(go())
    assert row["stage"] == "understand" and row["llm"] == "hcx" and row["model"] == "HCX-007"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (120, 30)
    assert row["max_completion_tokens"] == 1024
    assert (row["attempts"], row["outcome"]) == (1, "ok") and "error" not in row


def test_retries_are_one_row_with_attempts() -> None:
    sent = []

    def handler(request):
        sent.append(1)
        return (
            httpx.Response(429, json=HCX_LIMIT)
            if len(sent) < 3
            else httpx.Response(200, json=HCX_OK)
        )

    async def go():
        calls = llm_calls.start()
        await hcx(handler).chat("HCX-007", MESSAGES)
        return calls

    (row,) = asyncio.run(go())
    assert row["attempts"] == 3 and row["outcome"] == "ok"


def test_failed_call_is_recorded_before_the_error_goes_up() -> None:
    async def go():
        calls = llm_calls.start()
        with pytest.raises(HcxError):
            await hcx(lambda r: httpx.Response(429, json=HCX_LIMIT)).chat("HCX-007", MESSAGES)
        return calls

    (row,) = asyncio.run(go())
    assert (row["outcome"], row["error"], row["attempts"]) == ("rate_limit", "42901", 3)
    assert row["prompt_tokens"] is None


def test_gemma_call_is_recorded_with_its_own_model_name() -> None:
    async def go():
        calls = llm_calls.start()
        with llm_calls.stage("cell_pick"):
            await gemma(lambda r: httpx.Response(200, json=GEMMA_OK)).chat(
                "HCX-007", MESSAGES, maxCompletionTokens=512
            )
        return calls

    (row,) = asyncio.run(go())
    assert (row["stage"], row["llm"], row["model"]) == ("cell_pick", "gemma", "gemma-4-31b")
    assert (row["prompt_tokens"], row["completion_tokens"], row["max_completion_tokens"]) == (
        50,
        7,
        512,
    )


def test_gemma_unreachable_is_recorded(monkeypatch) -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    async def go():
        calls = llm_calls.start()
        with pytest.raises(HcxError):
            await gemma(handler).chat("HCX-007", MESSAGES)
        return calls

    (row,) = asyncio.run(go())
    assert (row["error"], row["attempts"]) == ("gemma_unreachable", 3)


def test_fallback_leaves_a_gemma_row_and_an_hcx_row(monkeypatch) -> None:
    settings = search_llm.get_settings()
    monkeypatch.setattr(settings, "search_llm", "auto", raising=False)
    monkeypatch.setattr(settings, "gemma_base_url", "http://gemma.test/v1", raising=False)
    monkeypatch.setattr(search_llm, "_down_until", 0.0)

    async def gemma_down(self, body):
        raise HcxError("gemma_530", "tunnel", 530)

    async def hcx_ok(self, model, messages, **options):
        return ChatResult(content="{}", finish_reason="stop", usage={"promptTokens": 9})

    monkeypatch.setattr(GemmaClient, "_chat_once", gemma_down)
    monkeypatch.setattr(HcxClient, "_chat_once", hcx_ok)

    @llm_calls.staged("llm_pick")
    async def review():
        return await search_llm.ask("HCX-007", lambda llm: (MESSAGES, {}))

    async def go():
        calls = llm_calls.start()
        await review()
        return calls

    calls = asyncio.run(go())
    assert [(c["stage"], c["llm"], c["outcome"]) for c in calls] == [
        ("llm_pick", "gemma", "transient"),
        ("llm_pick", "hcx", "ok"),
    ]


def test_requests_do_not_mix_and_parallel_calls_share_one_list(monkeypatch) -> None:
    async def ok(self, model, messages, **options):
        await asyncio.sleep(0)
        return ChatResult(content="{}", finish_reason="stop")

    monkeypatch.setattr(HcxClient, "_chat_once", ok)

    @llm_calls.staged("cell_pick")
    async def pick():
        await HcxClient().chat("HCX-007", MESSAGES)

    async def request():
        calls = llm_calls.start()
        with llm_calls.stage("understand"):
            await HcxClient().chat("HCX-007", MESSAGES)
        await asyncio.gather(pick(), pick())
        return calls

    async def both():
        return await asyncio.gather(request(), request())

    one, two = asyncio.run(both())
    for calls in (one, two):
        assert sorted(c["stage"] for c in calls) == ["cell_pick", "cell_pick", "understand"]


def test_totals_add_up_by_llm_and_stage() -> None:
    calls = [
        {"stage": "understand", "llm": "hcx", "ms": 1000, "prompt_tokens": 100,
         "completion_tokens": 20, "max_completion_tokens": 1024, "attempts": 2,
         "waited_ms": 1000, "outcome": "ok"},
        {"stage": "cell_pick", "llm": "gemma", "ms": 3000, "prompt_tokens": 50,
         "completion_tokens": 5, "max_completion_tokens": 512, "attempts": 1,
         "waited_ms": 0, "outcome": "ok"},
        {"stage": "cell_pick", "llm": "gemma", "ms": 500, "prompt_tokens": None,
         "completion_tokens": None, "max_completion_tokens": 512, "attempts": 3,
         "waited_ms": 4000, "outcome": "transient"},
    ]  # fmt: skip
    t = llm_calls.totals(calls)
    assert (t["calls"], t["hcx"], t["gemma"]) == (3, 1, 2)
    assert (t["prompt_tokens"], t["completion_tokens"], t["reserved_tokens"]) == (150, 25, 2048)
    assert (t["retries"], t["waited_ms"], t["failed"]) == (3, 5000, 1)
    assert t["by_stage"]["cell_pick"] == {"calls": 2, "ms": 3500, "tokens": 55}


def _turn(*stages: str) -> dict:
    return {
        "trace": {
            "llm_calls": [
                {"stage": s, "llm": "hcx", "ms": 2000, "prompt_tokens": 10,
                 "completion_tokens": 2, "max_completion_tokens": 100, "attempts": 1,
                 "waited_ms": 0, "outcome": "ok"}
                for s in stages
            ]
        }
    }  # fmt: skip


def test_eval_columns_join_both_turns_and_stay_blank_for_old_runs() -> None:
    cols = eval_devset.llm_columns(_turn("understand"), _turn("follow_up", "cell_pick"))
    assert cols["LLM 호출"] == 3 and cols["HCX 호출"] == 3 and cols["입력 토큰"] == 30
    assert set(cols) == set(eval_devset.LLM_COLUMNS)
    old = eval_devset.llm_columns({"trace": {"flow": []}}, None)
    assert all(v == "" for v in old.values())


def test_eval_summary_has_a_row_per_stage() -> None:
    recs = [
        {"문항": "D-001", **eval_devset.llm_columns(_turn("understand", "cell_pick"), None)},
        {"문항": "D-002", **eval_devset.llm_columns(_turn("understand"), None)},
        {"문항": "D-003", **eval_devset.llm_columns({"trace": {}}, None)},
    ]
    text = "\n".join(eval_devset.llm_summary(recs))
    assert "호출: 3회 (문항당 평균 1.5회, HCX 3 · Gemma 0)" in text
    assert "| 질문 이해 | 2 | 2 | 1.00 | 2.0초 | 24 |" in text
    assert "| 칸 고르기 | 1 | 1 | 0.50 | 2.0초 | 12 |" in text
    assert text.endswith("D-001 2회, D-002 1회")
    assert eval_devset.llm_summary([recs[2]]) == []
