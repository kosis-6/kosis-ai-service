from contextvars import copy_context
from types import SimpleNamespace

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
    async def fail(question: str, adjust=None) -> dict:
        raise HcxError("network", "HCX에 연결하지 못했습니다 (ConnectError)")

    monkeypatch.setattr(qa, "ask", fail)
    detail = client.post("/ask", json={"question": "서울 인구"}).json()["detail"]
    assert "잠시 후 다시 질문해 주세요" in detail and "ConnectError" not in detail


def test_ask_progress_steps_while_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    """답을 만드는 동안 처리 단계를 모으고 (같은 단계로 돌아오면 맨 뒤로), 끝나면 지운다"""
    from app.services import ask_session, progress

    seen = []

    async def fake_handle(session_id, question, user_id=None, selections=None, **kw) -> dict:
        for step in (progress.UNDERSTAND, progress.COMPUTE, progress.COMPUTE, progress.CHECK):
            progress.now(step)
        progress.now(progress.COMPUTE)  # 판정 뒤 다시 계산
        seen.append(client.get("/ask/progress/pid-12345678").json()["steps"])
        return {"status": "ok", "answer": "답", "parsed": {}, "result": {}, "sources": []}

    monkeypatch.setattr(ask_session, "handle", fake_handle)
    r = client.post("/ask", json={"question": "서울 인구", "progress_id": "pid-12345678"})
    assert r.status_code == 200
    assert seen == [[progress.UNDERSTAND, progress.CHECK, progress.COMPUTE]]
    assert client.get("/ask/progress/pid-12345678").json() == {"steps": []}


def test_ask_progress_rejects_odd_id() -> None:
    assert client.get("/ask/progress/a%2Fb").status_code in (404, 422)
    assert client.post("/ask", json={"question": "서울", "progress_id": "x"}).status_code == 422


def test_ask_cannot_claim_progress_id_from_an_active_request() -> None:
    from app.services import progress

    progress_id = "pid-active-1234"
    token = progress.begin(progress_id)
    try:
        progress.now(progress.FIND_TABLE)
        result = client.post("/ask", json={"question": "서울 인구", "progress_id": progress_id})
        assert result.status_code == 409
        assert client.get(f"/ask/progress/{progress_id}").json() == {"steps": [progress.FIND_TABLE]}
    finally:
        progress.end(token)


def test_ask_progress_expires_on_read_and_old_request_cannot_change_new_one(monkeypatch):
    from app.services import progress

    at = 1000.0
    monkeypatch.setattr(progress, "time", SimpleNamespace(monotonic=lambda: at))
    progress_id = "pid-expired-1234"
    before, after = copy_context(), copy_context()
    old = before.run(progress.begin, progress_id)
    before.run(progress.now, progress.REPORT)
    at += progress.TTL
    assert client.get(f"/ask/progress/{progress_id}").json() == {"steps": []}
    new = after.run(progress.begin, progress_id)
    try:
        before.run(progress.now, progress.WRAP_UP)
        before.run(progress.end, old)
        after.run(progress.now, progress.COMPUTE)
        assert client.get(f"/ask/progress/{progress_id}").json() == {"steps": [progress.COMPUTE]}
    finally:
        after.run(progress.end, new)
