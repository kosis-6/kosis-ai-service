import sys
from pathlib import Path

import pytest

from pipeline import run_all


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "kosis.duckdb"
    db.write_text("old")
    monkeypatch.setattr(run_all, "DB_PATH", db)
    monkeypatch.setattr(run_all, "NEW_DB_PATH", tmp_path / "kosis.new.duckdb")
    monkeypatch.setattr(run_all, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_all.py", "--source", "collect"])
    return db


def fake_run(fail_on: str | None = None):
    calls = []

    def run(script, env=None):
        calls.append((script, env))
        if script == "preprocess.py":
            Path(env["DUCKDB_PATH"]).write_text("new")
        if script == fail_on:
            raise SystemExit(1)

    return run, calls


def test_swaps_db_after_validation(paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = fake_run()
    monkeypatch.setattr(run_all, "run", run)
    run_all.main()
    assert paths.read_text() == "new"
    # 수집 결과 폴더를 읽고, 서비스 DB가 아니라 임시 DB에 만든다
    assert calls[0][1]["KOSIS_RAW_DIR"] == str(run_all.SOURCES["collect"])
    assert calls[0][1]["DUCKDB_PATH"] == str(run_all.NEW_DB_PATH)


def test_keeps_old_db_when_validation_fails(paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = fake_run(fail_on="validate.py")
    monkeypatch.setattr(run_all, "run", run)
    with pytest.raises(SystemExit):
        run_all.main()
    assert paths.read_text() == "old"
