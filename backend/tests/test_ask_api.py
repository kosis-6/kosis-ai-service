import pytest
from fastapi.testclient import TestClient

from app.core.stats_db import StatsNotLoadedError
from app.main import app
from app.services import qa
from app.services.hcx_client import HcxError

client = TestClient(app)


def test_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ask(question: str, adjust=None) -> dict:
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
        (StatsNotLoadedError("DB 없음"), 503),
        (HcxError("42901", "Too many requests", 429), 429),
        (HcxError("40100", "Unauthorized", 401), 502),
        (HcxError("network", "HCX에 연결하지 못했습니다 (ConnectError)"), 502),
    ],
)
def test_ask_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, status_code: int) -> None:
    async def fail(question: str, adjust=None) -> dict:
        raise error

    monkeypatch.setattr(qa, "ask", fail)
    r = client.post("/ask", json={"question": "서울 인구"})
    assert r.status_code == status_code
    # 오류 원문(DB 주소 · HCX 메시지)은 화면에 보내지 않는다
    assert "DB 없음" not in r.text and "Unauthorized" not in r.text


def test_question_clarification_is_exposed_without_ai(monkeypatch):
    from app.services import indicators

    monkeypatch.setattr(indicators, "find_regions", lambda _: [])

    async def unexpected(*args, **kwargs):
        raise AssertionError("되묻기 전에 AI를 호출하면 안 된다")

    monkeypatch.setattr(qa, "ask", unexpected)
    response = client.post("/ask", json={"question": "우리 동네 인구는?"})
    assert response.status_code == 200
    assert response.json()["clarification"]["kind"] == "local_region"
    response = client.post("/ask", json={"question": "수원에서 이사한 건수는?"})
    assert response.status_code == 200
    assert len(response.json()["clarification"]["options"]) == 2


def test_ask_hcx_error_detail_hides_raw_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # 화면에는 HCX 원문 오류 대신 다시 질문하라는 안내만
    async def fail(question: str, adjust=None) -> dict:
        raise HcxError("network", "HCX에 연결하지 못했습니다 (ConnectError)")

    monkeypatch.setattr(qa, "ask", fail)
    detail = client.post("/ask", json={"question": "서울 인구"}).json()["detail"]
    assert "잠시 후 다시 질문해 주세요" in detail and "ConnectError" not in detail
