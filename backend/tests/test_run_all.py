import sys

import pytest

from pipeline import run_all


@pytest.fixture
def collect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_all.py", "--source", "collect"])


def fake_run(fail_on: str | None = None):
    calls = []

    def run(script: str, env: dict[str, str] | None = None) -> None:
        calls.append((script, env))
        if script == fail_on:
            raise SystemExit(1)

    return run, calls


def scripts(calls: list) -> list[str]:
    return [script for script, _ in calls]


def test_publishes_after_validation(collect: None, monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = fake_run()
    monkeypatch.setattr(run_all, "run", run)
    run_all.main()
    assert scripts(calls) == [
        "preprocess.py",
        "catalog.py",
        "validate.py",
        "publish_stats.py",
        "load_raw.py",
        "region_map.py",
    ]
    assert calls[0][1]["KOSIS_RAW_DIR"] == str(run_all.SOURCES["collect"])


def test_does_not_publish_when_validation_fails(
    collect: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, calls = fake_run(fail_on="validate.py")
    monkeypatch.setattr(run_all, "run", run)
    with pytest.raises(SystemExit):
        run_all.main()
    assert "publish_stats.py" not in scripts(calls)
