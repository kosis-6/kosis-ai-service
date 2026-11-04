from pathlib import Path

import pytest

from app.services import qa


@pytest.fixture(autouse=True)
def request_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """API 테스트가 실제 data/requests/에 요청 기록을 남기지 않게 임시 폴더로 돌린다"""
    monkeypatch.setattr(qa, "REQUEST_LOG", tmp_path / "requests.jsonl")
    monkeypatch.setattr(qa, "MISSING_LOG", tmp_path / "missing.jsonl")
    return tmp_path
