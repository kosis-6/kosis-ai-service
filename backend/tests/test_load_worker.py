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
    assert calls[0][:2] == ("collect", "DT_X")


def test_file_only_source_does_not_call_kosis(tmp_path: Path, calls: list) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(w.process(_job(), "file", tmp_path))
    assert calls == []


def test_update_fetches_only_delta_into_separate_folder(tmp_path: Path, calls: list) -> None:
    """갱신은 받아 둔 원본 대신 KOSIS에서 가져올 부분만 받는다. 원본 파일은 건드리지 않고,
    받은 일부 자료는 _updates/에 두었다가 적재 뒤 지운다"""
    (tmp_path / "DT_X.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "DT_X.meta.json").write_text("{}", encoding="utf-8")
    asyncio.run(w.process(_job(reason="update"), "auto", tmp_path))
    assert calls == [("collect", "DT_X", w.UPDATE_DIR, True), ("load", "DT_X.csv", True)]
    assert (tmp_path / "DT_X.csv").read_text(encoding="utf-8") == CSV  # 원본 그대로
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


def test_error_message_hides_kosis_key() -> None:
    pg = FakePg()
    url = "https://kosis.kr/openapi/x.do?apiKey=SECRET123%3D&format=json"
    w.fail(pg, _job(attempts=3), httpx.ConnectError(f"실패 {url}"))
    saved = pg.sql[-1][1][0]
    assert "SECRET123" not in saved
    assert "apiKey=***&format=json" in saved
