import contextlib
import os
from pathlib import Path

import pytest

os.environ.setdefault("API_DOCS", "true")

from app.services import admin_audit, ask_session, population_latest, qa, search_llm


@pytest.fixture(autouse=True)
def admin_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    async def write(session, admin, method, path, query):
        seen.append({"admin_id": admin.id, "method": method, "path": path, "query": query})

    monkeypatch.setattr(admin_audit, "write", write)
    return seen


@pytest.fixture(autouse=True)
def search_llm_is_hcx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_llm.get_settings(), "search_llm", "hcx", raising=False)


@pytest.fixture(autouse=True)
def no_cell_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import cell_pick

    async def judge(question, understanding, tbl_nm, groups):
        return {"fits": "아니오", "picks": {}, "reason": "테스트"}, "test"

    monkeypatch.setattr(cell_pick, "judge", judge)


@pytest.fixture(autouse=True)
def no_composite_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """복합 판별(Gemma)은 테스트에서 늘 '단순' (네트워크 없이). 복합 경로는 test_composite 몫"""
    from app.services import composite

    async def gate(question):
        return {"composite": False, "why": "테스트"}

    monkeypatch.setattr(composite, "_gate", gate)


@pytest.fixture(autouse=True)
def no_recell_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import recell

    async def judge(text):
        return {"picks": []}, "test"

    monkeypatch.setattr(recell, "judge", judge)


@pytest.fixture(autouse=True)
def request_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(qa, "REQUEST_LOG", tmp_path / "requests.jsonl")
    monkeypatch.setattr(qa, "MISSING_LOG", tmp_path / "missing.jsonl")
    return tmp_path


@pytest.fixture(autouse=True)
def ask_sessions(monkeypatch: pytest.MonkeyPatch) -> dict:
    store: dict = {"sessions": {}, "turns": []}

    async def load(session_id, at):
        return store["sessions"].get(session_id), False

    async def save(session_id, state, conn=None):
        store["sessions"][session_id] = state

    async def log_turn(session_id, user_id, turn_no, question, *rest):
        store["turns"].append((session_id, turn_no, question))

    monkeypatch.setattr(ask_session, "load", load)
    monkeypatch.setattr(ask_session, "save", save)
    monkeypatch.setattr(ask_session, "log_turn", log_turn)
    monkeypatch.setattr(ask_session, "turn_writer", memory_turn_writer(store["sessions"]))
    return store


class NoDb:
    """turn_writer 가 넘기는 연결 대신: 대화 목록 저장(conversations.touch)은 하지 않는다"""

    async def execute(self, *args, **kwargs):
        return None


def memory_turn_writer(sessions: dict):
    """한 턴 저장을 메모리로 (ask_session.turn_writer).
    이어 가는 대화인데 상태가 없으면 지운 대화다"""

    @contextlib.asynccontextmanager
    async def writer(session_id, user_id, continuing):
        yield None if continuing and session_id not in sessions else NoDb()

    return writer


@pytest.fixture(autouse=True)
def no_latest_month(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        population_latest, "components_many", lambda codes, sex="계", period=None: {}
    )


@pytest.fixture(autouse=True)
def no_question_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "question_cache", False)
