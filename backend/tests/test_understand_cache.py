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
    again = asyncio.run(qa.understand("서울  인구 몇 명이야? ", trace))
    assert hcx == ["서울 인구 몇 명이야?"] and again == first
    assert trace["understand_cached"] is True and trace["hcx_raw"]


def test_cached_result_is_a_copy(hcx) -> None:
    first = asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    first["regions"].append("부산")
    assert asyncio.run(qa.understand("서울 인구 몇 명이야?"))["regions"] == ["서울"]


def test_new_data_year_misses_the_cache(hcx, monkeypatch) -> None:
    asyncio.run(qa.understand("작년 서울 인구"))
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2026))
    asyncio.run(qa.understand("작년 서울 인구"))
    assert len(hcx) == 2


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


def test_end_punctuation_is_same_question(hcx) -> None:
    asyncio.run(qa.understand("서울 인구 몇 명이야?"))
    asyncio.run(qa.understand("서울 인구 몇 명이야"))
    asyncio.run(qa.understand("서울 인구 몇 명이야 ?!"))
    assert len(hcx) == 1
    asyncio.run(qa.understand("부산 인구 몇 명이야?"))
    assert len(hcx) == 2


def test_cache_use_is_logged(tmp_path) -> None:
    import json

    path = tmp_path / "requests.jsonl"
    trace = {"understand_cached": True, "plan_cache": {"hit": 3, "tbl_id": "T"}}
    qa.log_request("서울 인구", {"status": "ok", "trace": trace}, path=path)
    record = json.loads(path.read_text())
    assert record["understand_cached"] is True and record["plan_cache"]["hit"] == 3
