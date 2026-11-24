import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import main
from app.core import db
from app.services import log_redaction as redaction
from app.services import log_retention, search_log, user_logs
from app.services.log_redaction import mask, redact


@pytest.mark.parametrize(
    "value",
    [
        "apiKey=FAKE_SECRET&format=json",
        "X-NCP-CLOVASTUDIO-API-KEY: FAKE_SECRET",
        '{"api_key": "FAKE_SECRET"}',
        "{'password': 'FAKE_SECRET'}",
        "access_token = FAKE_SECRET",
        "refresh-token: FAKE_SECRET",
        "Authorization: Bearer FAKE_SECRET",
        "Authorization: Basic FAKE_SECRET",
        "postgresql+psycopg://user:FAKE_SECRET@localhost/db",
        "https://user:FAKE_SECRET@example.test/path",
        "Cookie: session=FAKE_SECRET; other=FAKE_SECRET",
        '{"api_key": "prefix\\"FAKE_SECRET"}',
    ],
)
def test_secret_formats_are_masked(value):
    assert "FAKE_SECRET" not in redaction.mask(value)


def test_nested_logs_are_masked_without_changing_the_source():
    at = datetime.now(UTC)
    source = {
        "error": None,
        "response": {"apiKey": "FAKE_SECRET", "errors": ["password=FAKE_SECRET"]},
        "hcx_raw": ['{"access_token": "FAKE_SECRET"}'],
        "slots": {"conditions": {"password": "FAKE_SECRET", "year": 2025}},
        "created_at": at,
    }
    original = copy.deepcopy(source)
    shown = user_logs._masked(source)
    assert "FAKE_SECRET" not in str(shown)
    assert source == original
    assert shown["slots"]["conditions"]["year"] == 2025 and shown["created_at"] == at


class FakeEngine:
    def __init__(self, tables=()):
        self.tables = tables
        self.calls = []

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, query, params=None):
        self.calls.append((str(query), params))
        return SimpleNamespace(scalar=lambda: (params or {}).get("name") in self.tables or None)


def test_search_secrets_are_masked_before_database_insert(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(search_log, "_ready", True)
    asyncio.run(
        search_log.write(
            "apiKey=FAKE_SECRET",
            "S_test",
            result={"slots": {"password": "FAKE_SECRET"}},
            response={"status": "ok", "error": "Bearer FAKE_SECRET"},
            trace={"hcx_raw": [{"access_token": "FAKE_SECRET"}]},
            error="postgresql://user:FAKE_SECRET@localhost/db",
            user_id=3,
        )
    )
    params = next(p for sql, p in engine.calls if "INSERT INTO" in sql)
    assert "FAKE_SECRET" not in str(params)
    assert params["user_id"] == 3 and params["session_id"] == "S_test"
    assert json.loads(params["slots"])["password"] == "***"


@pytest.mark.parametrize("tables", [(), ("app.chat_sessions", "app.chat_turns", "app.search_logs")])
def test_retention_cleanup_handles_existing_and_missing_tables(monkeypatch, tables):
    engine = FakeEngine(tables)
    monkeypatch.setattr(db, "engine", engine)
    asyncio.run(log_retention.purge())
    deletes = [(sql, p) for sql, p in engine.calls if sql.startswith("delete")]
    assert len(deletes) == len(tables)
    for sql, params in deletes:
        assert "make_interval(days => :days)" in sql and params == {"days": 30}
    if tables:
        assert any("app.chat_sessions where updated_at" in sql for sql, _ in deletes)


def test_retention_job_retries_after_errors_and_can_be_cancelled(monkeypatch):
    calls = []

    async def purge():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")
        raise asyncio.CancelledError

    monkeypatch.setattr(log_retention, "purge", purge)
    monkeypatch.setattr(log_retention, "INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(log_retention.run_forever())
    assert len(calls) == 2


def test_retention_job_starts_and_stops_with_the_server(monkeypatch):
    events = []

    async def warm_up():
        pass

    async def cleanup():
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(main.search, "warm_up", warm_up)
    monkeypatch.setattr(main, "_queue_indicator_tables", warm_up)
    monkeypatch.setattr(log_retention, "run_forever", cleanup)

    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert events == ["started"]

    asyncio.run(run())
    assert events == ["started", "stopped"]


@pytest.mark.parametrize(
    "value",
    [
        "HCX_API_KEY=ZZVALUE failed",
        "JWT_SECRET_KEY=ZZVALUE",
        "POSTGRES_PASSWORD=ZZVALUE",
        "GEMMA_API_KEY: ZZVALUE",
        "hcx_api_key='ZZVALUE'",
        "serviceKey%3DZZVALUE&format=json",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIiwidiI6MX0.ZZVALUEZZVALUEZZVALUE rejected",
        "clova key nv-ZZVALUEZZVALUEZZVALUE0123 is invalid",
    ],
)
def test_env_style_names_and_bare_tokens_are_masked(value):
    assert "ZZVALUE" not in mask(value)


def test_usage_numbers_are_not_masked():
    text = "usage: promptTokens: 120, completionTokens=33, max_tokens=512, 비밀번호는 abc"
    assert mask(text) == text
    kept = redact({"totalTokens": 5, "HCX_API_KEY": "x", "jwt_secret_key": "y", "token": "z"})
    assert kept == {"totalTokens": 5, "HCX_API_KEY": "***", "jwt_secret_key": "***", "token": "***"}
