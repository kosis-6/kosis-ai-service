import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ask_session, qa
from scripts import missing_requests as mr

client = TestClient(app)


def refused(status: str, gap: str | None = None) -> dict:
    parsed = {"type": "value", "indicator": "20대 혼인율", "regions": ["서울특별시"], "year": 2025}
    result = {"gap": gap} if gap else {}
    return {"status": status, "answer": "안내", "parsed": parsed, "result": result, "sources": []}


def test_logs_only_data_gaps(tmp_path: Path) -> None:
    log = tmp_path / "missing.jsonl"
    assert qa.log_missing("질문", refused("unknown_indicator"), log) is True
    assert qa.log_missing("질문", refused("missing_region"), log) is False
    assert qa.log_missing("질문", refused("ok"), log) is False
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["parsed"]["indicator"] == "20대 혼인율"


def test_ask_route_logs_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "missing.jsonl"
    monkeypatch.setattr(qa, "MISSING_LOG", log)

    async def fake_ask(question: str, adjust=None) -> dict:
        return refused("out_of_range")

    async def no_search(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(qa, "ask", fake_ask)
    monkeypatch.setattr(ask_session, "_search_after", no_search)
    assert client.post("/ask", json={"question": "2030년 서울 인구"}).status_code == 200
    assert json.loads(log.read_text(encoding="utf-8"))["question"] == "2030년 서울 인구"


def test_summary_groups_same_request(tmp_path: Path) -> None:
    log = tmp_path / "missing.jsonl"
    for q in ["서울 20대 혼인율", "2025 서울 20대 혼인율은?"]:
        qa.log_missing(q, refused("unknown_indicator", "not_collected"), log)
    qa.log_missing("2030 서울", refused("out_of_range"), log)
    text = mr.summarize(mr.load(log))
    assert "요청 3건, 묶음 2개" in text
    row = (
        "| 2 | 지원하지 않는 지표 | 수집 안 함 | 20대 혼인율 | 서울특별시 | 2025 "
        "| 서울 20대 혼인율 |"
    )
    assert row in text
    assert "수집 안 함 2건" in text
