"""표가 다시 적재되면 조회 캐시(표 키·코드·수록 기간)를 비운다 (DB 없이 적재 상태 값을 바꿔 끼움)"""

import pytest

from app.core.stats_db import StatsNotLoadedError
from app.services import lookup


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lookup, "_reload", {"checked_at": None, "version": None})
    cleared = []
    fakes = []
    for i in range(2):
        fake = type("Cache", (), {"cache_clear": lambda self, i=i: cleared.append(i)})()
        fakes.append(fake)
    monkeypatch.setattr(lookup, "TABLE_CACHES", tuple(fakes))
    return cleared


def _versions(monkeypatch, values):
    seen = iter(values)
    calls = []

    def version():
        calls.append(1)
        return next(seen)

    monkeypatch.setattr(lookup, "_load_version", version)
    return calls


def test_first_check_only_records_version(monkeypatch, fresh) -> None:
    _versions(monkeypatch, [(20, "t1", None)])
    assert lookup.refresh_if_reloaded(0.0) is False
    assert fresh == []


def test_clears_caches_when_load_state_changes(monkeypatch, fresh) -> None:
    _versions(monkeypatch, [(20, "t1", None), (20, "t1", None), (21, "t2", "j1")])
    lookup.refresh_if_reloaded(0.0)
    assert lookup.refresh_if_reloaded(31.0) is False  # 그대로면 비우지 않는다
    assert lookup.refresh_if_reloaded(62.0) is True
    assert fresh == [0, 1]


def test_checks_db_at_most_every_interval(monkeypatch, fresh) -> None:
    calls = _versions(monkeypatch, [(20, "t1", None), (21, "t2", None)])
    lookup.refresh_if_reloaded(0.0)
    assert lookup.refresh_if_reloaded(10.0) is False  # 30초 안에는 DB를 보지 않는다
    assert len(calls) == 1
    assert lookup.refresh_if_reloaded(31.0) is True


def test_db_unavailable_is_ignored(monkeypatch, fresh) -> None:
    def broken():
        raise StatsNotLoadedError("연결 안 됨")

    monkeypatch.setattr(lookup, "_load_version", broken)
    assert lookup.refresh_if_reloaded(0.0) is False


def test_every_table_cache_is_listed() -> None:
    # 표 정보를 캐시하는 함수를 새로 만들면 TABLE_CACHES에도 넣어야 다시 적재 때 비워진다
    import inspect

    source = inspect.getsource(lookup)
    cached = {
        line.split("def ")[1].split("(")[0]
        for line in source.split("@lru_cache\n")[1:]
        for line in [line.splitlines()[0]]
    }
    listed = source.split("TABLE_CACHES = (")[1].split(")")[0]
    assert cached == {name.strip() for name in listed.split(",") if name.strip()}
