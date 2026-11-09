"""적재 프로세스 기본 틀: 원본 찾기(받아 둔 파일 → 없으면 KOSIS), 재시도 규칙 (DB·KOSIS 없이)"""

import asyncio
import gzip
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from pipeline import load_worker as w

CSV = "tbl_id,org_id,period,prd_se,item_id,item_nm,value,value_flag,unit,last_changed\n"


def _job(reason="manual", attempts=1):
    return w.Job(id=1, tbl_id="DT_X", org_id="101", reason=reason, attempts=attempts)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    seen = []

    async def collect(job, table_dir):
        seen.append(("collect", job.tbl_id))
        (table_dir / f"{job.tbl_id}.csv").write_text(CSV, encoding="utf-8")
        return table_dir / f"{job.tbl_id}.csv"

    def load(path, tbl_id):
        seen.append(("load", path.name, path.exists()))

    monkeypatch.setattr(w, "collect", collect)
    monkeypatch.setattr(w, "_load", load)
    return seen


def test_uses_downloaded_csv_without_kosis(tmp_path: Path, calls: list) -> None:
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls == [("load", "DT_X.csv", True)]
    assert (tmp_path / "DT_X.csv").exists()  # 받아 둔 원본은 지우지 않는다


def test_unpacks_gz_and_removes_only_unpacked_csv(tmp_path: Path, calls: list) -> None:
    with gzip.open(tmp_path / "DT_X.csv.gz", "wt", encoding="utf-8") as f:
        f.write(CSV)
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls == [("load", "DT_X.csv", True)]
    assert not (tmp_path / "DT_X.csv").exists()
    assert (tmp_path / "DT_X.csv.gz").exists()


def test_missing_file_falls_back_to_kosis(tmp_path: Path, calls: list) -> None:
    # CSV만 있고 표 설명(.meta.json)이 없으면 다 받은 표로 보지 않는다
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    asyncio.run(w.process(_job(), "auto", tmp_path))
    assert calls[0] == ("collect", "DT_X")


def test_file_only_source_does_not_call_kosis(tmp_path: Path, calls: list) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(w.process(_job(), "file", tmp_path))
    assert calls == []


def test_update_always_downloads_fresh(tmp_path: Path, calls: list) -> None:
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(reason="update"), "auto", tmp_path))
    assert calls[0] == ("collect", "DT_X")


@pytest.mark.parametrize(
    ("error", "attempts", "expected"),
    [
        (httpx.ConnectTimeout("느림"), 1, timedelta(minutes=10)),
        (httpx.ConnectTimeout("느림"), 2, timedelta(hours=1)),
        (httpx.ConnectTimeout("느림"), 3, None),  # 3번 실패하면 그만
        (ValueError("행 수가 다릅니다"), 1, None),  # 검증 실패는 다시 해도 같다
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
