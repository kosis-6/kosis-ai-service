"""질문 이해 캐시 (qa.UNDERSTAND_CACHE, 11/18): 같은 질문이면 HCX를 다시 부르지 않는다"""

import asyncio

import pytest

from app.core.config import get_settings
from app.services import indicators, qa


@pytest.fixture
def hcx(monkeypatch):
    calls = []

    async def chat(prompt, system, temperature=0.1, trace=None):
        calls.append(prompt)
        return '{"type": "value", "indicator": "총인구", "regions": ["서울"], "year": null}'

    monkeypatch.setattr(qa, "_chat", chat)
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa, "correct", lambda question, parsed: dict(parsed))
    monkeypatch.setattr(get_settings(), "question_cache", True)
    qa.UNDERSTAND_CACHE.clear()
    yield calls
    qa.UNDERSTAND_CACHE.clear()


def test_same_question_is_understood_once(hcx) -> None:
    first = asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    trace: dict = {}
    again = asyncio.run(qa.understand("서울  인구 몇 명이야? ", trace))  # 띄어쓰기만 다름
    assert hcx == ["서울 인구 몇 명이야?"] and again == first
    assert trace["understand_cached"] is True and trace["hcx_raw"]


def test_cached_result_is_a_copy(hcx) -> None:
    first = asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    first["regions"].append("부산")  # 대화 이어받기가 결과를 고쳐도 캐시는 그대로
    assert asyncio.run(qa.understand("서울 인구 몇 명이야?"))["regions"] == ["서울"]


def test_new_data_year_misses_the_cache(hcx, monkeypatch) -> None:
    asyncio.run(qa.understand("작년 서울 인구"))
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2026))
    asyncio.run(qa.understand("작년 서울 인구"))
    assert len(hcx) == 2  # 자료의 마지막 연도가 바뀌면 '작년'이 다르게 풀릴 수 있다


def test_switch_off(hcx, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "question_cache", False)
    asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    assert len(hcx) == 2


def test_parse_error_is_not_cached(hcx, monkeypatch) -> None:
    async def broken(prompt, system, temperature=0.1, trace=None):
        hcx.append(prompt)
        return "JSON 아님"

    monkeypatch.setattr(qa, "_chat", broken)
    asyncio.run(qa.understand("이상한 말"))
    asyncio.run(qa.understand("이상한 말"))
    assert len(hcx) == 2
