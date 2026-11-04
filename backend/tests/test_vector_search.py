import asyncio
from types import SimpleNamespace

import numpy as np

from app.services import embedding
from app.services import vector_search as vs
from app.services.hcx_client import HcxError


def _setup(monkeypatch, backend: str = "clova") -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(vs, "get_settings", lambda: SimpleNamespace(embedding_backend=backend))
    monkeypatch.setattr(vs, "_cache", vs.OrderedDict())
    return calls


def test_query_embedding_is_normalized_and_cached(monkeypatch) -> None:
    calls = _setup(monkeypatch)

    async def fake(text, model, timeout):
        calls.append(text)
        return [3.0, 4.0]

    monkeypatch.setattr(embedding, "embed_once", fake)
    first = asyncio.run(vs.embed_query("돌아가신 분 수"))
    assert np.allclose(first, [0.6, 0.8])
    # 같은 질문(앞뒤 공백만 다름)은 다시 부르지 않는다
    asyncio.run(vs.embed_query(" 돌아가신 분 수 "))
    assert calls == ["돌아가신 분 수"]


def test_query_embedding_failure_falls_back_and_is_not_cached(monkeypatch) -> None:
    calls = _setup(monkeypatch)

    async def rate_limited(text, model, timeout):
        calls.append(text)
        raise HcxError("42901", "Too many requests", 429)

    monkeypatch.setattr(embedding, "embed_once", rate_limited)
    assert asyncio.run(vs.embed_query("q")) is None
    assert asyncio.run(vs.embed_query("q")) is None
    assert len(calls) == 2  # 실패는 저장하지 않아 다음 요청에서 다시 시도한다


def test_cache_keeps_recent_questions(monkeypatch) -> None:
    _setup(monkeypatch)
    monkeypatch.setattr(vs, "CACHE_SIZE", 2)

    async def fake(text, model, timeout):
        return [1.0, 0.0]

    monkeypatch.setattr(embedding, "embed_once", fake)
    for q in ("a", "b", "c"):
        asyncio.run(vs.embed_query(q))
    assert list(vs._cache) == [("clova", "b"), ("clova", "c")]


def test_off_backend_skips_embedding(monkeypatch) -> None:
    _setup(monkeypatch, backend="off")
    assert asyncio.run(vs.embed_query("q")) is None
