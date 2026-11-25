import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import file_logs, log_retention, qa


@pytest.mark.parametrize("missing", [False, True])
def test_request_and_missing_logs_mask_secrets_before_writing(tmp_path, missing):
    path = tmp_path / "log.jsonl"
    res = {
        "status": "unknown_indicator",
        "parsed": {"password": "SYNTHETIC_SECRET"},
        "result": {"related": ["apiKey=SYNTHETIC_SECRET"]},
        "trace": {"hcx_raw": ['{"access_token": "SYNTHETIC_SECRET"}']},
    }
    if missing:
        qa.log_missing("token=SYNTHETIC_SECRET", res, path=path)
    else:
        qa.log_request(
            "token=SYNTHETIC_SECRET", res, error="Bearer SYNTHETIC_SECRET", user_id=7, path=path
        )
    data = path.read_text()
    row = json.loads(data)
    assert "SYNTHETIC_SECRET" not in data and row["parsed"]["password"] == "***"
    assert datetime.fromisoformat(row["at"]).utcoffset() == timedelta(0)
    if not missing:
        assert row["user_id"] == 7 and row["hcx_raw"]
    assert res["parsed"]["password"] == "SYNTHETIC_SECRET"


def test_cleanup_removes_expired_and_unreadable_rows_and_masks_existing_rows(tmp_path):
    path = tmp_path / "log.jsonl"
    cutoff = datetime(2026, 11, 1, tzinfo=UTC)
    records = [
        {"at": "2026-10-31T23:59:59+00:00", "question": "만료"},
        {"at": "2026-11-01T00:00:00+00:00", "question": "token=SYNTHETIC_SECRET"},
        {"at": "2026-11-01T09:00:01+09:00", "question": "유지"},
        {"at": "2026-11-02T00:00:00", "question": "기존 시간대 없는 형식"},
        {"question": "시각 없음"},
        {"at": "잘못된 시각"},
        None,
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n깨진 JSON\n")
    file_logs.purge(path, cutoff)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 3 and "SYNTHETIC_SECRET" not in str(rows)
    assert rows[1]["question"] == "유지"
    assert not [p for p in tmp_path.glob(".log.jsonl.*") if p.suffix != ".lock"]


def test_append_and_cleanup_do_not_lose_or_duplicate_records(tmp_path):
    path = tmp_path / "log.jsonl"
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    file_logs.append(path, {"at": (now - timedelta(days=31)).isoformat(), "id": "expired"})

    def write(i):
        file_logs.append(path, {"at": now.isoformat(), "id": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write, i) for i in range(100)]
        futures += [pool.submit(file_logs.purge, path, cutoff) for _ in range(5)]
        for future in futures:
            future.result()
    file_logs.purge(path, cutoff)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 100 and {row["id"] for row in rows} == set(range(100))


def test_db_failure_does_not_prevent_file_retention(request_logs, monkeypatch):
    old = datetime.now(UTC) - timedelta(days=31)
    for path in (qa.REQUEST_LOG, qa.MISSING_LOG):
        file_logs.append(path, {"at": old.isoformat(), "question": "만료"})
    calls = []

    async def broken_db():
        calls.append(1)
        if len(calls) == 2:
            raise asyncio.CancelledError
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(log_retention, "purge", broken_db)
    monkeypatch.setattr(log_retention, "INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(log_retention.run_forever())
    assert qa.REQUEST_LOG.read_text() == qa.MISSING_LOG.read_text() == ""


def test_failed_rewrite_preserves_the_original_file(tmp_path):
    path = tmp_path / "log.jsonl"
    now = datetime.now(UTC)
    file_logs.append(path, {"at": now.isoformat(), "question": "원본"})
    original = path.read_bytes()
    with patch.object(file_logs.os, "replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            file_logs.purge(path, now - timedelta(days=30))
    assert path.read_bytes() == original
    assert not [p for p in tmp_path.glob(".log.jsonl.*") if p.suffix != ".lock"]
