import pytest
from fastapi.testclient import TestClient

from app.core.duckdb import DuckDBNotFoundError
from app.main import app
from app.services import qa
from app.services.hcx_client import HcxError

client = TestClient(app)


def test_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ask(question: str) -> dict:
        return {
            "status": "ok",
            "answer": f"답변: {question}",
            "parsed": {"type": "value"},
            "result": {"status": "ok"},
            "sources": [{"name": "출처", "basis": "기준", "tbl_id": "DT_TEST", "url": None}],
        }

    monkeypatch.setattr(qa, "ask", fake_ask)
    response = client.post("/ask", json={"question": "서울 인구"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "답변: 서울 인구"
    assert body["status"] == "ok"
    assert body["sources"][0]["tbl_id"] == "DT_TEST"


def test_ask_empty_question() -> None:
    assert client.post("/ask", json={"question": ""}).status_code == 422


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (DuckDBNotFoundError("DB 없음"), 503),
        (HcxError("42901", "Too many requests", 429), 429),
        (HcxError("40100", "Unauthorized", 401), 502),
    ],
)
def test_ask_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, status_code: int) -> None:
    async def fail(question: str) -> dict:
        raise error

    monkeypatch.setattr(qa, "ask", fail)
    assert client.post("/ask", json={"question": "서울 인구"}).status_code == status_code
