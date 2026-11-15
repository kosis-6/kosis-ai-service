import asyncio
import json

import httpx
import pytest

from app.prompts import llm_pick as pick_prompt
from app.prompts import plan as plan_prompt
from app.prompts import search as slot_prompt
from app.prompts import swap
from app.services import search_llm
from app.services.hcx_client import ChatResult, HcxClient, HcxError
from app.services.search_llm import GemmaClient, to_openai_body

MESSAGES = [{"role": "user", "content": "x"}]
OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": '{"a": 1}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def make_client(
    handler, retry_waits: tuple[float, ...] = (0, 0), api_key: str = "k"
) -> GemmaClient:
    return GemmaClient(
        api_key=api_key,
        base_url="http://gemma.test/v1/",
        model="gemma-4-31b",
        transport=httpx.MockTransport(handler),
        retry_waits=retry_waits,
    )


def test_hcx_options_become_openai_fields() -> None:
    body = to_openai_body(
        "gemma-4-31b",
        [
            {"role": "system", "content": "규칙"},
            {"role": "user", "content": [{"type": "text", "text": "질문"}]},
        ],
        thinking={"effort": "low"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=300,
        temperature=0,
        repetitionPenalty=1.1,
        responseFormat={"type": "json", "schema": {"type": "object"}},
    )
    assert body["messages"][1] == {"role": "user", "content": "질문"}  # 목록 content 는 문자열로
    assert body["max_tokens"] == 300 and body["top_p"] == 0.8
    assert body["temperature"] == 0  # 0 을 기본값(0.1)으로 바꾸지 않는다
    assert body["chat_template_kwargs"] == {"enable_thinking": False}  # 생각은 항상 끈다
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "out", "schema": {"type": "object"}},
    }
    assert not {"thinking", "topK", "topP", "repetitionPenalty", "responseFormat"} & set(body)


def test_defaults_and_plain_json_format() -> None:
    body = to_openai_body("m", [{"role": "user", "content": "x"}], responseFormat={"type": "json"})
    assert (body["max_tokens"], body["temperature"], body["top_p"]) == (1024, 0.1, 0.8)
    assert body["response_format"] == {"type": "json_object"}
    assert "response_format" not in to_openai_body("m", [{"role": "user", "content": "x"}])


def test_gemma_chat_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer k"
        assert json.loads(request.content)["model"] == "gemma-4-31b"  # HCX 모델 이름은 안 보낸다
        return httpx.Response(200, json=OK_BODY)

    result = asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert result.content == '{"a": 1}' and result.finish_reason == "stop"
    assert result.usage == {"promptTokens": 5, "completionTokens": 3, "totalTokens": 8}


def test_gemma_without_key_sends_no_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=OK_BODY)

    asyncio.run(
        make_client(handler, api_key="").chat("HCX-007", [{"role": "user", "content": "x"}])
    )


def test_gemma_retries_server_error_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return (
            httpx.Response(503, text="loading")
            if len(calls) == 1
            else httpx.Response(200, json=OK_BODY)
        )

    result = asyncio.run(make_client(handler).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert result.content == '{"a": 1}' and len(calls) == 2


def test_gemma_failure_is_raised_as_hcx_error() -> None:
    """부르는 쪽은 HcxError 만 잡는다. 잘못된 요청(400)은 다시 시도하지 않는다"""
    calls = []

    def bad_request(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad schema")

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(bad_request).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.code == "gemma_400" and len(calls) == 1

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(unreachable).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.code == "gemma_unreachable"

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>tunnel error</html>")

    with pytest.raises(HcxError) as exc:
        asyncio.run(make_client(not_json).chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.code == "gemma_bad_response"


def test_gemma_without_base_url_fails_before_calling() -> None:
    client = GemmaClient(base_url="", api_key="")
    client.base_url = ""  # .env 에 주소가 있어도 이 테스트는 빈 주소로
    with pytest.raises(HcxError) as exc:
        asyncio.run(client.chat("HCX-007", [{"role": "user", "content": "x"}]))
    assert exc.value.code == "gemma_not_configured"


@pytest.mark.parametrize(("setting", "expected"), [("hcx", "hcx"), ("gemma", "gemma")])
def test_chat_goes_to_the_configured_llm(monkeypatch, setting: str, expected: str) -> None:
    async def hcx(self, model, messages, **options):
        return ChatResult(content="hcx", finish_reason="stop")

    async def gemma(self, model, messages, **options):
        return ChatResult(content="gemma", finish_reason="stop")

    monkeypatch.setattr(HcxClient, "chat", hcx)
    monkeypatch.setattr(GemmaClient, "chat", gemma)
    monkeypatch.setattr(search_llm.get_settings(), "search_llm", setting, raising=False)
    result, answered = asyncio.run(search_llm.ask("HCX-007", lambda llm: (MESSAGES, {})))
    assert (result.content, answered) == (expected, expected)


def test_gemma_prompts_add_to_the_hcx_prompts_without_changing_them() -> None:
    """HCX 프롬프트는 그대로 두고 Gemma 용만 문장을 더한다"""
    assert "1번 후보가 질문에 맞으면 1번을 고르고" in pick_prompt.SYSTEM_GEMMA
    assert "1번 후보가 질문에 맞으면" not in pick_prompt.SYSTEM
    assert "'우리 동네'는 지역 이름이 아니다" in slot_prompt.SLOT_SYSTEM_GEMMA
    assert "지역 이름이 아니다" not in slot_prompt.SLOT_SYSTEM
    for added in ("1-1. 단, KOSIS가", "대상 이름을 지우고", "age 칸에는 나이만", "예시 2-2)"):
        assert added in plan_prompt.SYSTEM_GEMMA and added not in plan_prompt.SYSTEM
    # 더하기만 했으므로 Gemma 용에서 더한 문장을 빼면 원문과 길이가 같다
    assert len(slot_prompt.SLOT_SYSTEM_GEMMA) > len(slot_prompt.SLOT_SYSTEM)
    assert plan_prompt.SYSTEM_GEMMA.index("예시 2-2)") < plan_prompt.SYSTEM_GEMMA.index("예시 3)")


def test_swap_fails_loudly_when_the_sentence_is_gone() -> None:
    assert swap("가 나 다", "나", "라") == "가 라 다"
    with pytest.raises(ValueError):
        swap("가 나 다", "마", "라")


@pytest.mark.parametrize(
    ("setting", "base_url", "expected"),
    [
        ("auto", "http://gemma.test/v1", "gemma"),  # 주소가 있으면 Gemma
        ("auto", "", "hcx"),  # 없으면 HCX
        ("auto", "   ", "hcx"),
        ("hcx", "http://gemma.test/v1", "hcx"),  # 직접 정하면 주소와 관계없이 그 모델
        ("gemma", "", "gemma"),
    ],
)
def test_provider_follows_the_gemma_address(
    monkeypatch, setting: str, base_url: str, expected: str
) -> None:
    settings = search_llm.get_settings()
    monkeypatch.setattr(settings, "search_llm", setting, raising=False)
    monkeypatch.setattr(settings, "gemma_base_url", base_url, raising=False)
    assert search_llm.provider() == expected
    assert search_llm.is_gemma() is (expected == "gemma")


def test_default_setting_is_auto() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["search_llm"].default == "auto"


@pytest.fixture
def gemma_by_address(monkeypatch):
    """기본 설정(auto) + Gemma 서버 주소가 있는 상태. Gemma · HCX 호출은 calls 에 적힌다"""
    settings = search_llm.get_settings()
    monkeypatch.setattr(settings, "search_llm", "auto", raising=False)
    monkeypatch.setattr(settings, "gemma_base_url", "http://gemma.test/v1", raising=False)
    monkeypatch.setattr(search_llm, "_down_until", 0.0)
    calls: list[str] = []
    state = {"gemma_up": True}

    async def gemma(self, model, messages, **options):
        calls.append(f"gemma:{messages[0]['content']}")
        if not state["gemma_up"]:
            raise HcxError("gemma_unreachable", "ConnectError: refused")
        return ChatResult(content="gemma", finish_reason="stop")

    async def hcx(self, model, messages, **options):
        calls.append(f"hcx:{messages[0]['content']}")
        return ChatResult(content="hcx", finish_reason="stop")

    monkeypatch.setattr(GemmaClient, "chat", gemma)
    monkeypatch.setattr(HcxClient, "chat", hcx)
    return calls, state


def _request(llm: str):
    return [{"role": "user", "content": f"{llm} 프롬프트"}], {}


def test_gemma_down_falls_back_to_hcx_with_the_hcx_request(gemma_by_address) -> None:
    calls, state = gemma_by_address
    result, answered = asyncio.run(search_llm.ask("HCX-007", _request))
    assert (result.content, answered) == ("gemma", "gemma") and not search_llm.falling_back()

    state["gemma_up"] = False
    result, answered = asyncio.run(search_llm.ask("HCX-007", _request))
    assert (result.content, answered) == ("hcx", "hcx")  # 그 호출을 HCX 로 다시 보낸다
    assert calls[-2:] == ["gemma:gemma 프롬프트", "hcx:hcx 프롬프트"]  # HCX 에는 HCX 용 요청을
    assert search_llm.falling_back()


def test_gemma_is_skipped_while_down_and_tried_again_later(gemma_by_address, monkeypatch) -> None:
    calls, state = gemma_by_address
    state["gemma_up"] = False
    asyncio.run(search_llm.ask("HCX-007", _request))
    calls.clear()
    asyncio.run(search_llm.ask("HCX-007", _request))
    assert calls == ["hcx:hcx 프롬프트"]  # 꺼진 서버를 질문마다 다시 부르지 않는다

    state["gemma_up"] = True
    monkeypatch.setattr(search_llm, "_down_until", 0.0)  # 건너뛰는 시간이 지났다
    calls.clear()
    _, answered = asyncio.run(search_llm.ask("HCX-007", _request))
    assert answered == "gemma" and calls == ["gemma:gemma 프롬프트"]  # 서버가 돌아오면 다시 Gemma
    assert not search_llm.falling_back()


def test_fixed_gemma_does_not_fall_back(gemma_by_address, monkeypatch) -> None:
    """SEARCH_LLM=gemma 로 고정하면 실패를 그대로 올린다 (측정에 HCX 답이 섞이지 않게)"""
    calls, state = gemma_by_address
    monkeypatch.setattr(search_llm.get_settings(), "search_llm", "gemma", raising=False)
    state["gemma_up"] = False
    with pytest.raises(HcxError):
        asyncio.run(search_llm.ask("HCX-007", _request))
    assert calls == ["gemma:gemma 프롬프트"] and not search_llm.falling_back()


def test_fallback_does_not_wait_for_retries(monkeypatch) -> None:
    """넘어갈 곳이 있으면 Gemma 를 기다렸다 다시 부르지 않는다"""
    settings = search_llm.get_settings()
    monkeypatch.setattr(settings, "search_llm", "auto", raising=False)
    monkeypatch.setattr(settings, "gemma_base_url", "http://gemma.test/v1", raising=False)
    monkeypatch.setattr(search_llm, "_down_until", 0.0)
    tried = []

    async def once(self, body):
        tried.append(1)
        raise HcxError("gemma_503", "loading", 503)

    async def hcx(self, model, messages, **options):
        return ChatResult(content="hcx", finish_reason="stop")

    monkeypatch.setattr(GemmaClient, "_chat_once", once)
    monkeypatch.setattr(HcxClient, "chat", hcx)
    _, answered = asyncio.run(search_llm.ask("HCX-007", _request))
    assert answered == "hcx" and len(tried) == 1

