import asyncio

from app.services import lookup
from app.services import search as se


def table(tbl_id, family, start, end):
    return {
        "tbl_id": tbl_id,
        "family_id": family,
        "start_year": start,
        "end_year": end,
        "edition": f"{start}~{end}",
    }


def test_family_last_year_is_latest_end_in_family():
    ts = [table("A", "F", 2010, 2020), table("B", "F", 2015, 2024), table("C", None, 2000, 2005)]
    se._set_family_last_year(ts)
    assert [t["family_last_year"] for t in ts] == [2024, 2024, 2005]


def test_reload_refreshes_periods_in_place(monkeypatch):
    tables = [table("A", "F", 2023, 2024)]
    se._set_family_last_year(tables)
    vectors = object()
    calls = []

    async def no_check():
        return None

    async def fake_refresh(ts):
        calls.append(ts)
        ts[0]["end_year"], ts[0]["edition"] = 2025, "2023~2025"
        se._set_family_last_year(ts)

    monkeypatch.setattr(se, "_tables", tables)
    monkeypatch.setattr(se, "_vectors", vectors)
    monkeypatch.setattr(se, "_periods_stale", False)
    monkeypatch.setattr(se, "_check_reloaded", no_check)
    monkeypatch.setattr(se, "_refresh_periods", fake_refresh)

    assert asyncio.run(se.load_tables()) is tables
    assert calls == []

    lookup.clear_table_caches()
    assert asyncio.run(se.load_tables()) is tables
    assert se._vectors is vectors
    assert (tables[0]["end_year"], tables[0]["edition"]) == (2025, "2023~2025")
    assert tables[0]["family_last_year"] == 2025

    asyncio.run(se.load_tables())
    assert len(calls) == 1
