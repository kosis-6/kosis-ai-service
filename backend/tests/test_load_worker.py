import asyncio
import contextlib
import gzip
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from pipeline import load_worker as w

CSV = "tbl_id,org_id,period,prd_se,item_id,item_nm,value,value_flag,unit,last_changed\n"


def _job(reason="manual", attempts=1):
    return w.Job(id=1, tbl_id="DT_X", org_id="101", reason=reason, attempts=attempts)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    seen = []

    async def collect(job, table_dir, delta=False):
        seen.append(("collect", job.tbl_id, table_dir.name, delta))
        table_dir.mkdir(parents=True, exist_ok=True)
        (table_dir / f"{job.tbl_id}.csv").write_text(CSV, encoding="utf-8")
        (table_dir / f"{job.tbl_id}.meta.json").write_text("{}", encoding="utf-8")
        return table_dir / f"{job.tbl_id}.csv"

    def load(path, tbl_id, merge=False):
        seen.append(("load", path.name, path.exists()))

    monkeypatch.setattr(w, "collect", collect)
    monkeypatch.setattr(w, "_load", load)
    return seen


def test_uses_downloaded_csv_without_kosis(tmp_path: Path, calls: list) -> None:
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls == [("load", "DT_X.csv", True)]
    assert (tmp_path / "DT_X.csv").exists()


def test_unpacks_gz_and_removes_only_unpacked_csv(tmp_path: Path, calls: list) -> None:
    with gzip.open(tmp_path / "DT_X.csv.gz", "wt", encoding="utf-8") as f:
        f.write(CSV)
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls == [("load", "DT_X.csv", True)]
    assert not (tmp_path / "DT_X.csv").exists()
    assert (tmp_path / "DT_X.csv.gz").exists()


def test_missing_file_falls_back_to_kosis(tmp_path: Path, calls: list) -> None:
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls[0][:2] == ("collect", "DT_X")


def test_file_only_source_does_not_call_kosis(tmp_path: Path, calls: list) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(w.process(_job(), "file", tmp_path))
    assert calls == []


def test_update_fetches_only_delta_into_separate_folder(tmp_path: Path, calls: list) -> None:
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(reason="update"), "auto", tmp_path))
    assert calls == [("collect", "DT_X", w.UPDATE_DIR, True), ("load", "DT_X.csv", True)]
    assert (tmp_path / "DT_X.csv").read_text(encoding="utf-8") == CSV
    assert not (tmp_path / w.UPDATE_DIR / "DT_X.csv").exists()
    assert not (tmp_path / w.UPDATE_DIR / "DT_X.meta.json").exists()


def test_update_with_nothing_to_fetch_does_not_load(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(job, table_dir, delta=False):
        raise w.NothingToFetch(job.tbl_id)

    monkeypatch.setattr(w, "collect", nothing)
    asyncio.run(w.process(_job(reason="update"), "auto", tmp_path))
    assert calls == []


def test_loaded_table_is_skipped(calls: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """원본 저장과 적재 후 단계가 한 트랜잭션이라 원본이 있으면 매핑까지 끝난 표다"""
    monkeypatch.setattr(w, "_with_pg", lambda fn, *args: fn(None, *args))
    monkeypatch.setattr(w, "is_loaded", lambda pg, tbl_id: True)
    asyncio.run(w.handle(_job(attempts=2)))
    assert calls == []


class TxPg:
    """psycopg 연결 대신: 트랜잭션 시작 · 확정 · 되돌림을 적는다"""

    def __init__(self, events: list) -> None:
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextlib.contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        self.events.append("commit")


@pytest.mark.parametrize("fails", [False, True])
def test_raw_rows_and_after_load_steps_commit_together(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    """원본을 저장한 뒤 지역 매핑이 실패하면 원본만 확정돼 '적재됨'이 됐다 (11/26 리뷰).
    원본 저장과 적재 후 단계를 한 트랜잭션으로: 매핑이 실패하면 원본도 되돌린다"""
    events: list = []
    monkeypatch.setattr(w.psycopg, "connect", lambda *a, **k: TxPg(events))
    monkeypatch.setattr(w, "load_table", lambda pg, path, merge=False: events.append("raw"))

    def map_table(pg, tbl_id):
        events.append("map")
        if fails:
            raise psycopg.OperationalError("연결 끊김")

    monkeypatch.setattr(w, "AFTER_LOAD", [("지역 매핑", map_table)])
    if fails:
        with pytest.raises(psycopg.OperationalError):
            w._load(Path("DT_X.csv"), "DT_X")
        assert events == ["begin", "raw", "map", "rollback"]
    else:
        w._load(Path("DT_X.csv"), "DT_X")
        assert events == ["begin", "raw", "map", "commit"]


def test_table_not_loaded_yet_is_processed(calls: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "_with_pg", lambda fn, *args: fn(None, *args))
    monkeypatch.setattr(w, "is_loaded", lambda pg, tbl_id: False)
    seen = []

    async def process(job):
        seen.append(job.tbl_id)

    monkeypatch.setattr(w, "process", process)
    asyncio.run(w.handle(_job()))
    assert seen == ["DT_X"] and calls == []


@pytest.mark.parametrize(
    ("error", "attempts", "expected"),
    [
        (httpx.ConnectTimeout("느림"), 1, timedelta(minutes=10)),
        (httpx.ConnectTimeout("느림"), 2, timedelta(hours=1)),
        (httpx.ConnectTimeout("느림"), 3, None),
        (ValueError("행 수가 다릅니다"), 1, None),
        (SystemExit("분류가 6개입니다"), 1, None),
    ],
)
def test_retry_delay(error, attempts, expected) -> None:
    assert w.retry_delay(error, attempts) == expected


class FakePg:
    def __init__(self) -> None:
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))


def test_fail_requeues_transient_and_fails_others() -> None:
    pg = FakePg()
    assert w.fail(pg, _job(attempts=1), httpx.ReadTimeout("느림")) == "queued"
    assert "run_after = now() + %s" in pg.sql[-1][0]
    assert w.fail(pg, _job(attempts=1), ValueError("합계가 다릅니다")) == "failed"
    assert pg.sql[-1][1][0] == "ValueError: 합계가 다릅니다"


def test_error_message_hides_kosis_key() -> None:
    pg = FakePg()
    url = "https://kosis.kr/openapi/x.do?apiKey=SECRET123%3D&format=json"
    w.fail(pg, _job(attempts=3), httpx.ConnectError(f"실패 {url}"))
    saved = pg.sql[-1][1][0]
    assert "SECRET123" not in saved
    assert "apiKey=***&format=json" in saved
