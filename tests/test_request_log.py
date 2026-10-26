import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import qa
from app.services.hcx_client import HcxError
from scripts import request_log as rl

client = TestClient(app)


def answered() -> dict:
    return {
        "status": "ok",
        "answer": "답변",
        "parsed": {"type": "value", "corrections": ["sex: None → '여' (질문의 성별 표현)"]},
        "result": {"status": "ok", "value": 20.43},
        "sources": [],
        "trace": {"hcx_raw": '{"type": "value"}', "understand_ms": 900, "compute_ms": 5},
    }


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_logs_every_request_with_stages(
    request_logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ask(question: str) -> dict:
        return answered()

    monkeypatch.setattr(qa, "ask", fake_ask)
    response = client.post("/ask", json={"question": "2025년 부산 여성 고령인구 비율"})
    assert response.status_code == 200
    assert "trace" not in response.json()  # 로그용 정보는 API 응답에 넣지 않음
    [record] = read(request_logs / "requests.jsonl")
    assert record["status"] == "ok"
    assert record["hcx_raw"] == '{"type": "value"}'
    assert record["parsed"]["corrections"]
    assert record["understand_ms"] == 900
    assert record["elapsed_ms"] is not None
    # 답한 질문은 데이터 부족 기록에 남지 않음
    assert not (request_logs / "missing.jsonl").exists()


def test_logs_failed_request(request_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(question: str) -> dict:
        raise HcxError("40100", "Unauthorized", 401)

    monkeypatch.setattr(qa, "ask", fail)
    assert client.post("/ask", json={"question": "서울 인구"}).status_code == 502
    [record] = read(request_logs / "requests.jsonl")
    assert record["status"] is None
    assert record["error"].startswith("HcxError")


def test_summary_and_show() -> None:
    records = [
        {**qa_record("ok"), "elapsed_ms": 1000},
        {**qa_record("ok"), "elapsed_ms": 3000},
        {**qa_record(None), "error": "HcxError: 401", "elapsed_ms": 500},
    ]
    text = rl.summarize(records)
    assert "| ok | 2 | 2000 |" in text
    assert "| 실패 | 1 | 500 |" in text
    shown = rl.show(records[0], detail=True)
    assert "보정: sex" in shown
    assert "HCX 원문" in shown


def qa_record(status: str | None) -> dict:
    res = answered() if status else None
    if res:
        res["status"] = status
    return {
        "at": "2026-10-26T12:00:00",
        "question": "질문",
        "status": status,
        "error": None,
        "hcx_raw": (res or {}).get("trace", {}).get("hcx_raw"),
        "parsed": (res or {}).get("parsed"),
        "result": (res or {}).get("result"),
    }
