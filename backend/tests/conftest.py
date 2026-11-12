from pathlib import Path

import pytest

from app.services import ask_session, qa


@pytest.fixture(autouse=True)
def request_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """API 테스트가 실제 data/requests/에 요청 기록을 남기지 않게 임시 폴더로 돌린다"""
    monkeypatch.setattr(qa, "REQUEST_LOG", tmp_path / "requests.jsonl")
    monkeypatch.setattr(qa, "MISSING_LOG", tmp_path / "missing.jsonl")
    return tmp_path


@pytest.fixture(autouse=True)
def ask_sessions(monkeypatch: pytest.MonkeyPatch) -> dict:
    """/ask 테스트가 개발 DB에 수치 대화 세션(app.chat_sessions)·턴 기록(app.chat_turns)을
    남기지 않게 메모리로 돌린다.
    세션 흐름을 따로 보는 테스트(test_ask_session의 Talk)는 다시 바꿔 끼운다"""
    store: dict = {"sessions": {}, "turns": []}

    async def load(session_id, at):
        return store["sessions"].get(session_id), False

    async def save(session_id, state):
        store["sessions"][session_id] = state

    async def log_turn(session_id, user_id, turn_no, question, *rest):
        store["turns"].append((session_id, turn_no, question))

    monkeypatch.setattr(ask_session, "load", load)
    monkeypatch.setattr(ask_session, "save", save)
    monkeypatch.setattr(ask_session, "log_turn", log_turn)
    return store
