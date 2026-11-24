import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.db import get_session
from app.main import app
from app.services import ask_session as s
from app.services import conversations as c

AT = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
STATE = {"conditions": {"indicator": "출생아 수", "regions": ["서울"]}, "user_id": 3}
REAL_LOAD = s.load


@pytest.fixture(autouse=True)
def clean_cache():
    s.STATE_CACHE.clear()
    yield
    s.STATE_CACHE.clear()


@pytest.mark.parametrize(
    ("user_id", "after", "expired"),
    [
        (None, timedelta(minutes=59), False),
        (None, timedelta(hours=2), True),
        (3, timedelta(days=5), False),
        (3, timedelta(days=31), True),
    ],
)
def test_logged_in_conversation_keeps_longer(user_id, after, expired) -> None:
    s.STATE_CACHE.set("A_1", ({**STATE, "user_id": user_id}, AT))
    state, was_expired = asyncio.run(REAL_LOAD("A_1", AT + after))
    assert was_expired is expired and (state is None) is expired


def test_state_is_read_from_memory_copy() -> None:
    s.STATE_CACHE.set("A_1", (STATE, AT))
    state, _ = asyncio.run(REAL_LOAD("A_1", AT))
    assert state == STATE
    state["conditions"]["regions"].append("부산")
    assert s.STATE_CACHE.get("A_1")[0]["conditions"]["regions"] == ["서울"]


def test_save_touches_conversation_only_for_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    touched = []

    async def nothing(*args):
        return None

    async def touch(sid, user_id, question):
        touched.append((sid, user_id, question))

    monkeypatch.setattr(s, "save", nothing)
    monkeypatch.setattr(s, "log_turn", nothing)
    monkeypatch.setattr(c, "touch", touch)
    asyncio.run(s._save("A_1", {"turn_no": 1}, {}, None, "서울 출생아 수", None))
    asyncio.run(s._save("A_2", {"turn_no": 1}, {}, None, "부산 출생아 수", 3))
    assert touched == [("A_2", 3, "부산 출생아 수")]


def test_replay_keeps_screen_fields_without_internal_context() -> None:
    res = {
        "status": "clarify_question",
        "answer": "어느 것을 볼까요?",
        "clarification": {"kind": "table_cell", "options": ["가"], "context": {"tbl_id": "T"}},
        "parsed": {"indicator": "x"},
        "trace": {"hcx": 1},
        "notice": None,
    }
    out = c.replay(res)
    assert out == {
        "status": "clarify_question",
        "answer": "어느 것을 볼까요?",
        "clarification": {"kind": "table_cell", "options": ["가"]},
    }


def test_title_is_first_question_shortened() -> None:
    assert c.title_of("  2024년   서울 출생아 수 ") == "2024년 서울 출생아 수"
    long = c.title_of("가" * 100)
    assert len(long) == c.TITLE_MAX and long.endswith("…")


ROW = {"session_id": "A_1", "title": "서울 출생아 수", "created_at": AT, "updated_at": AT}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def no_db():
        yield None

    owner = 3

    async def listing(user_id, limit, offset):
        rows = [{**ROW, "turns": 2}] if user_id == owner else []
        return {"total": len(rows), "conversations": rows}

    async def detail(user_id, session_id):
        if user_id != owner or session_id != "A_1":
            return None
        turn = {"turn_no": 1, "question": "서울 출생아 수", "created_at": AT}
        return {**ROW, "turns": [{**turn, "response": {"status": "ok", "answer": "답"}}]}

    async def rename(user_id, session_id, title):
        return user_id == owner and session_id == "A_1"

    monkeypatch.setattr(c, "listing", listing)
    monkeypatch.setattr(c, "detail", detail)
    monkeypatch.setattr(c, "rename", rename)

    async def delete(user_id, session_id):
        return user_id == owner and session_id == "A_1"

    monkeypatch.setattr(c, "delete", delete)
    app.dependency_overrides[get_session] = no_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(user_id: int) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)


def test_owner_lists_opens_renames_deletes(client: TestClient) -> None:
    login(3)
    listed = client.get("/conversations")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert listed.json()["conversations"][0]["turns"] == 2
    opened = client.get("/conversations/A_1").json()
    assert opened["turns"][0]["response"]["answer"] == "답"
    assert client.patch("/conversations/A_1", json={"title": "새 제목"}).status_code == 204
    assert client.delete("/conversations/A_1").status_code == 204


def test_other_account_cannot_see_conversation(client: TestClient) -> None:
    login(9)
    assert client.get("/conversations").json()["total"] == 0
    assert client.get("/conversations/A_1").status_code == 404
    assert client.patch("/conversations/A_1", json={"title": "남의 것"}).status_code == 404
    assert client.delete("/conversations/A_1").status_code == 404


def test_login_required_and_inputs_checked(client: TestClient) -> None:
    assert client.get("/conversations").status_code == 401
    login(3)
    assert client.get("/conversations/A_1;drop").status_code == 422
    assert client.patch("/conversations/A_1", json={"title": ""}).status_code == 422
    assert client.patch("/conversations/A_1", json={"title": "가" * 61}).status_code == 422
