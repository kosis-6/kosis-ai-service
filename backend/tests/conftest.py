import os
from pathlib import Path

import pytest

# API 문서(/docs)는 기본이 닫힘이다. 문서 화면의 보안 헤더를 보는 테스트가 있어 테스트에서는 연다
# (app 을 불러오기 전에 정해야 한다)
os.environ.setdefault("API_DOCS", "true")

from app.services import admin_audit, ask_session, population_latest, qa, search_llm


@pytest.fixture(autouse=True)
def admin_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """관리자 호출 기록(app.admin_audit)을 DB 대신 목록에 모은다"""
    seen: list[dict] = []

    async def write(session, admin, method, path, query):
        seen.append({"admin_id": admin.id, "method": method, "path": path, "query": query})

    monkeypatch.setattr(admin_audit, "write", write)
    return seen


@pytest.fixture(autouse=True)
def search_llm_is_hcx(monkeypatch: pytest.MonkeyPatch) -> None:
    """로컬 .env 에 Gemma 서버 주소가 있어도 테스트는 HCX 경로로 돈다
    (Gemma 경로를 보는 테스트는 search_llm 을 직접 바꾼다)"""
    monkeypatch.setattr(search_llm.get_settings(), "search_llm", "hcx", raising=False)


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


@pytest.fixture(autouse=True)
def no_latest_month(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트가 KOSIS에서 최신 달 인구를 받지 않게 한다 (받지 못한 것처럼 None).
    최신 달을 보는 테스트는 population_latest.components_many를 다시 바꿔 끼운다"""
    monkeypatch.setattr(
        population_latest, "components_many", lambda codes, sex="계", period=None: {}
    )
