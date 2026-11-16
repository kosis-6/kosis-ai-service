"""관리자용 사용자 기록 조회: 거르는 조건 만들기, /admin/logs API
(DB 대신 services.user_logs 의 조회 함수를 바꿔 끼운다)
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.db import get_session
from app.main import app
from app.models.app import AdminAudit
from app.services import admin_audit, user_logs

AT = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
TURN = {
    "id": 7,
    "created_at": AT,
    "session_id": "A_1",
    "turn_no": 2,
    "user_id": 3,
    "user_email": "user@b.co",
    "user_nickname": "통계왕",
    "message": "서울도 같이",
    "request_type": "change",
    "status": "ok",
    "tbl_id": "DT_1B040A3",
    "recomputed": True,
    "answer": "2024년 총인구는 …",
}
SEARCH = {
    "id": 11,
    "created_at": AT,
    "session_id": "S_1",
    "user_id": None,
    "user_email": None,
    "user_nickname": None,
    "question": "다문화 관련 어떤 통계가 있어?",
    "status": "ok",
    "tbl_id": "DT_1",
    "elapsed_ms": 4200,
    "error": None,
}


@pytest.fixture
def client() -> TestClient:
    async def no_db():
        yield None

    app.dependency_overrides[get_session] = no_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, is_admin=True)
    return client


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """조회 함수 대신 받은 인자를 적어 두고 정해 둔 줄을 돌려준다"""
    seen: dict = {}

    async def turns(session, limit, offset, **filters):
        seen["turns"] = {"limit": limit, "offset": offset, **filters}
        return [TURN], 1

    async def sessions(session, limit, offset, user_id, guest, q, since, until):
        seen["sessions"] = {"user_id": user_id, "guest": guest, "q": q, "since": since}
        row = {
            "session_id": "A_1",
            "user_id": 3,
            "user_email": "user@b.co",
            "user_nickname": "통계왕",
            "started_at": AT,
            "last_at": AT,
            "turns": 2,
            "first_message": "부산 인구",
            "last_status": "ok",
        }
        return [row], 1

    async def session_turns(session, session_id):
        if session_id != "A_1":
            return []
        guest_turn = {**TURN, "id": 6, "turn_no": 1, "user_id": None, "user_email": None}
        extra = {"conditions_before": None, "conditions_after": {"regions": ["서울"]}}
        return [{**guest_turn, **extra, "sources": None}, {**TURN, **extra, "sources": {}}]

    async def searches(session, limit, offset, **filters):
        seen["searches"] = filters
        return [SEARCH], 1

    async def search_detail(session, log_id):
        if log_id != 11:
            return None
        return {**SEARCH, "slots": {"topic": "다문화"}, "candidates": [], "response": None,
                "hcx_raw": None, "timings": {"slots_ms": 1500}}  # fmt: skip

    async def summary(session, since):
        seen["summary"] = since
        return {
            "turns": 5,
            "sessions": 2,
            "users": 1,
            "guest_turns": 3,
            "searches": 4,
            "search_errors": 0,
            "by_day": [{"day": AT.date(), "turns": 5, "sessions": 2}],
            "by_status": [{"name": "ok", "count": 4}, {"name": "tables", "count": 1}],
            "by_request_type": [{"name": "new", "count": 5}],
        }

    for name, fn in {
        "turns": turns,
        "sessions": sessions,
        "session_turns": session_turns,
        "searches": searches,
        "search_detail": search_detail,
        "summary": summary,
    }.items():
        monkeypatch.setattr(user_logs, name, fn)
    return seen


# ---- 거르는 조건 -------------------------------------------------------------------------


def test_no_filter_is_no_where() -> None:
    assert user_logs.conditions(user_logs.TURN_COLUMNS) == ("", {})


def test_filters_are_bound_not_pasted() -> None:
    where, params = user_logs.conditions(
        user_logs.TURN_COLUMNS,
        user_id=3,
        status="ok",
        request_type="change",
        q="100% 인구_수",
        since=AT,
    )
    assert where == (
        " where t.user_id = :user_id and t.status = :status and t.request_type = :request_type"
        " and t.message ilike :q escape '\\' and t.created_at >= :since"
    )
    # 사용자가 넣은 % 와 _ 는 글자 그대로 찾는다
    assert params["q"] == "%100\\% 인구\\_수%"
    assert "100%" not in where and params["user_id"] == 3


@pytest.mark.parametrize(("guest", "sql"), [(True, "is null"), (False, "is not null")])
def test_guest_filter(guest: bool, sql: str) -> None:
    where, params = user_logs.conditions(user_logs.SEARCH_COLUMNS, guest=guest)
    assert where == f" where s.user_id {sql}" and params == {}


def test_search_logs_have_no_request_type_but_have_error() -> None:
    where, params = user_logs.conditions(
        user_logs.SEARCH_COLUMNS, request_type="new", has_error=True
    )
    assert where == " where s.error is not null" and params == {}
    # 턴 기록에는 오류 칸이 없다
    assert user_logs.conditions(user_logs.TURN_COLUMNS, has_error=True) == ("", {})


def test_blank_word_is_ignored() -> None:
    assert user_logs.conditions(user_logs.TURN_COLUMNS, q="   ") == ("", {})


# ---- API ---------------------------------------------------------------------------------

PATHS = [
    "/admin/logs/summary",
    "/admin/logs/sessions",
    "/admin/logs/sessions/A_1",
    "/admin/logs/turns",
    "/admin/logs/searches",
    "/admin/logs/searches/11",
]


@pytest.mark.parametrize("path", PATHS)
def test_logs_need_login(client: TestClient, calls: dict, path: str) -> None:
    assert client.get(path).status_code == 401
    assert calls == {}


@pytest.mark.parametrize("path", PATHS)
def test_logs_are_admin_only(client: TestClient, calls: dict, path: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, is_admin=False)
    assert client.get(path).status_code == 403
    assert calls == {}


def test_turns_pass_filters(admin: TestClient, calls: dict) -> None:
    params = {"user_id": 3, "status": "ok", "q": "서울", "since": "2026-11-15T00:00:00"}
    r = admin.get("/admin/logs/turns", params={**params, "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["turns"][0]["message"] == "서울도 같이"
    assert body["turns"][0]["user_email"] == "user@b.co"
    seen = calls["turns"]
    assert (seen["limit"], seen["offset"], seen["user_id"], seen["status"]) == (10, 0, 3, "ok")
    # 시간대 없이 온 시각은 UTC 로 본다
    assert seen["since"] == datetime(2026, 11, 15, tzinfo=UTC)
    assert seen["guest"] is None and seen["until"] is None


def test_sessions_list(admin: TestClient, calls: dict) -> None:
    r = admin.get("/admin/logs/sessions", params={"guest": "false", "q": "부산"})
    assert r.status_code == 200
    row = r.json()["sessions"][0]
    assert (row["session_id"], row["turns"], row["first_message"]) == ("A_1", 2, "부산 인구")
    assert calls["sessions"]["guest"] is False and calls["sessions"]["q"] == "부산"


def test_session_detail_in_order_with_user(admin: TestClient, calls: dict) -> None:
    r = admin.get("/admin/logs/sessions/A_1")
    assert r.status_code == 200
    body = r.json()
    assert [t["turn_no"] for t in body["turns"]] == [1, 2]
    assert body["turns"][0]["conditions_after"] == {"regions": ["서울"]}
    # 중간에 로그인한 대화는 로그인한 턴의 사용자를 대화의 사용자로 본다
    assert body["user_email"] == "user@b.co"
    assert admin.get("/admin/logs/sessions/A_none").status_code == 404


def test_searches_list_and_detail(admin: TestClient, calls: dict) -> None:
    r = admin.get("/admin/logs/searches", params={"has_error": "false", "status": "ok"})
    assert r.status_code == 200
    assert r.json()["searches"][0]["elapsed_ms"] == 4200
    assert calls["searches"]["has_error"] is False
    assert "slots" not in r.json()["searches"][0]  # 목록에는 무거운 칸을 싣지 않는다

    one = admin.get("/admin/logs/searches/11")
    assert one.status_code == 200 and one.json()["timings"] == {"slots_ms": 1500}
    assert admin.get("/admin/logs/searches/12").status_code == 404


def test_summary(admin: TestClient, calls: dict) -> None:
    r = admin.get("/admin/logs/summary", params={"days": 3})
    assert r.status_code == 200
    body = r.json()
    assert (body["days"], body["turns"], body["guest_turns"]) == (3, 5, 3)
    assert body["by_day"] == [{"day": "2026-11-16", "turns": 5, "sessions": 2}]
    assert calls["summary"].tzinfo is not None
    assert admin.get("/admin/logs/summary", params={"days": 31}).status_code == 422


def test_limit_is_capped(admin: TestClient, calls: dict) -> None:
    assert admin.get("/admin/logs/turns", params={"limit": 201}).status_code == 422
    assert admin.get("/admin/logs/turns", params={"q": "가" * 101}).status_code == 422


# ---- 보안 --------------------------------------------------------------------------------


def test_error_text_hides_keys() -> None:
    raw = (
        "ConnectError: https://kosis.kr/openapi/x.do?method=getList&apiKey=AbC123=&format=json"
        " / Authorization: Bearer eyJhbGciOi.payload.sig / token=zzz"
    )
    shown = user_logs.mask(raw)
    assert "AbC123" not in shown and "eyJhbGciOi" not in shown and "zzz" not in shown
    assert "apiKey=***&format=json" in shown and "Bearer ***" in shown
    assert user_logs.mask(None) is None
    assert user_logs.mask("HcxError: 429 too many requests") == "HcxError: 429 too many requests"


def test_admin_answers_are_not_cached(admin: TestClient, calls: dict) -> None:
    for path in PATHS:
        assert admin.get(path).headers["cache-control"] == "no-store"


def test_long_filter_values_are_refused(admin: TestClient, calls: dict) -> None:
    assert admin.get("/admin/logs/turns", params={"session_id": "A" * 65}).status_code == 422
    assert admin.get("/admin/logs/searches", params={"status": "s" * 41}).status_code == 422


def test_admin_calls_are_recorded(admin: TestClient, calls: dict, admin_calls: list) -> None:
    admin.get("/admin/logs/turns", params={"user_id": 3, "q": "서울"})
    admin.get("/admin/logs/sessions/A_1")
    assert admin_calls == [
        {"admin_id": 1, "method": "GET", "path": "/admin/logs/turns",
         "query": {"user_id": "3", "q": "서울"}},
        {"admin_id": 1, "method": "GET", "path": "/admin/logs/sessions/A_1", "query": {}},
    ]  # fmt: skip


def test_refused_calls_are_not_recorded(client: TestClient, calls: dict, admin_calls: list) -> None:
    assert client.get("/admin/logs/turns").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, is_admin=False)
    assert client.get("/admin/logs/turns").status_code == 403
    assert admin_calls == []


def test_recording_failure_does_not_block(
    admin: TestClient, calls: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(admin_audit, "write", broken)
    assert admin.get("/admin/logs/turns").status_code == 200


def test_audit_list(admin: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def recent(session, limit, offset):
        row = AdminAudit(id=5, created_at=AT, admin_id=1, admin_email="admin@b.co")
        row.method, row.path, row.query = "GET", "/admin/logs/turns", {"user_id": "3"}
        return [row], 1

    monkeypatch.setattr(admin_audit, "recent", recent)
    r = admin.get("/admin/audit")
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["records"][0] == {
        "id": 5, "created_at": "2026-11-16T03:00:00Z", "admin_id": 1, "admin_email": "admin@b.co",
        "method": "GET", "path": "/admin/logs/turns", "query": {"user_id": "3"},
    }  # fmt: skip


def test_audit_keeps_filter_values_short() -> None:
    kept = admin_audit.trimmed({"q": "가" * 500, "user_id": "3"})
    assert len(kept["q"]) == admin_audit.MAX_VALUE and kept["user_id"] == "3"
    assert admin_audit.trimmed({}) is None
