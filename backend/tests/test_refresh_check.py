"""매일 0시 KOSIS 갱신 확인 (pipeline/refresh_check.py): DB·KOSIS 없이 바꿔 끼운다"""

import asyncio
import json
from datetime import datetime

import pytest

from pipeline import refresh_check as rc

KST = rc.KST
POP = [
    {"PRD_SE": "월", "STRT_PRD_DE": "2008.01", "END_PRD_DE": "2026.10"},
    {"PRD_SE": "년", "STRT_PRD_DE": "2008", "END_PRD_DE": "2025"},
]
# DT_1B26009: 월·분기·년이 있는데 DB에는 월·년만 있음
BIRTHS = [
    {"PRD_SE": "월", "END_PRD_DE": "2026.09"},
    {"PRD_SE": "분기", "END_PRD_DE": "2026 3/4"},
    {"PRD_SE": "년", "END_PRD_DE": "2025"},
]


@pytest.mark.parametrize(
    ("code", "end", "value"),
    [
        ("A", "2025", 2025),
        ("M", "2026.09", 202609),
        ("Q", "2026 3/4", 202603),
        ("H", "2026 1/2", 202601),
        ("D", "2026.10.31", 20261031),
        ("F", "2025", 2025),
    ],
)
def test_db_period(code, end, value) -> None:
    assert rc.db_period(code, end) == value


@pytest.mark.parametrize(
    ("periods", "db", "new"),
    [
        (POP, {"A": 2025}, {"M": (None, 202610)}),  # 연간만 적재: 빠진 월별도 가져온다
        (POP, {"A": 2024, "M": 202610}, {"A": (2024, 2025)}),
        (POP, {"M": 202609, "A": 2025}, {"M": (202609, 202610)}),
        (POP, {"M": 202610, "A": 2025}, {}),
        # 분기 "2026 3/4"(숫자만 202634)를 월 202609와 견주던 오탐 없이, 빠진 분기만 가져온다
        (BIRTHS, {"M": 202609, "A": 2025}, {"Q": (None, 202603)}),
        (BIRTHS, {"M": 202609, "Q": 202602, "A": 2025}, {"Q": (202602, 202603)}),
        ([{"PRD_SE": "5년", "END_PRD_DE": "2020"}], {"F": 2015}, {"F": (2015, 2020)}),
        # 다년 표도 자료는 연(A)으로 와서 DB에 A로 있다 → 같은 연도면 최신
        ([{"PRD_SE": "2년", "END_PRD_DE": "2025"}], {"A": 2025}, {}),
        ([{"PRD_SE": "5년", "END_PRD_DE": "2025"}], {"A": 2020}, {"F": (2020, 2025)}),
        ([], {"A": 2025}, {}),
        (POP, {}, {}),  # DB에 자료가 없는 표는 보지 않는다
    ],
)
def test_needs_update(periods, db, new) -> None:
    assert rc.needs_update(periods, db) == new


def test_next_run_is_midnight_kst() -> None:
    assert rc.seconds_until_next_run(datetime(2026, 11, 12, 23, 0, tzinfo=KST)) == 3600
    assert rc.seconds_until_next_run(datetime(2026, 11, 13, 0, 0, tzinfo=KST)) == 24 * 3600
    # 컨테이너 시계가 UTC여도 한국 시간 0시 (UTC 15시 = KST 0시)
    utc = datetime(2026, 11, 12, 14, 0, tzinfo=rc.ZoneInfo("UTC"))
    assert rc.seconds_until_next_run(utc) == 3600


def test_missed_today(tmp_path) -> None:
    state = tmp_path / "last_run.json"
    now = datetime(2026, 11, 12, 9, 0, tzinfo=KST)
    assert rc.missed_today(now, state)  # 기록 없음
    state.write_text(json.dumps({"date": "2026-11-11"}))
    assert rc.missed_today(now, state)
    state.write_text(json.dumps({"date": "2026-11-12"}))
    assert not rc.missed_today(now, state)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict:
    state = {
        "tables": [("T_NEW", "101", 1), ("T_SAME", "101", 2), ("T_ERR", "101", 3)],
        "queued": [],
    }

    def with_pg(fn, *args):
        if fn is rc.loaded_tables:
            only = args[0] if args else None
            return [t for t in state["tables"] if not only or t[0] in only]
        if fn is rc.enqueue_updates:
            state["queued"] += args[0]
        if fn is rc.db_lasts:
            return {"A": 2024 if args[0] == 1 else 2025}
        if fn is rc.last_update_done:
            return state.get("done_at")
        return None

    async def periods(client, org_id, tbl_id):
        if tbl_id == "T_ERR":
            raise RuntimeError("40: 호출 초과 (https://x?apiKey=SECRET)")
        return [{"PRD_SE": "년", "END_PRD_DE": "2025"}]

    class NoWait:
        def __init__(self, per_minute):
            pass

        async def wait(self):
            return None

    monkeypatch.setattr(rc, "_with_pg", with_pg)
    monkeypatch.setattr(rc, "_kosis_periods", periods)
    monkeypatch.setattr("scripts.collect_all.RateLimiter", NoWait)
    return state


def test_check_all_queues_only_newer_tables(fake, tmp_path) -> None:
    state_path = tmp_path / "last_run.json"
    summary = asyncio.run(rc.check_all(state_path=state_path, tried_path=tmp_path / "t.json"))
    assert fake["queued"] == [("T_NEW", "101")]
    assert [u["tbl_id"] for u in summary["updated"]] == ["T_NEW"]
    assert summary["checked"] == 2 and summary["failed"][0]["tbl_id"] == "T_ERR"
    assert "SECRET" not in summary["failed"][0]["error"]  # 오류에 API 키를 남기지 않는다
    assert json.loads(state_path.read_text())["date"] == summary["date"]  # 그날 확인 기록


def test_dry_run_and_partial_run_do_not_count(fake, tmp_path) -> None:
    state_path = tmp_path / "last_run.json"
    tried = tmp_path / "t.json"
    asyncio.run(rc.check_all(dry_run=True, state_path=state_path, tried_path=tried))
    assert fake["queued"] == [] and not state_path.exists()
    asyncio.run(rc.check_all(only=["T_NEW"], state_path=state_path, tried_path=tried))
    assert fake["queued"] == [("T_NEW", "101")] and not state_path.exists()


def test_period_only_in_kosis_metadata_is_not_reloaded_every_night(fake, tmp_path) -> None:
    """KOSIS 수록 기간에는 2025년이 있지만 다시 받아도 자료가 없으면
    다음 날부터는 다시 넣지 않는다"""
    paths = {"state_path": tmp_path / "s.json", "tried_path": tmp_path / "t.json"}
    asyncio.run(rc.check_all(**paths))
    assert fake["queued"] == [("T_NEW", "101")]
    fake["done_at"] = datetime.now(KST)  # 적재 프로세스가 다시 받았지만 DB는 여전히 2024년
    summary = asyncio.run(rc.check_all(**paths))
    assert fake["queued"] == [("T_NEW", "101")] and summary["waiting"] == ["T_NEW"]


def test_already_tried() -> None:
    queued = datetime(2026, 11, 12, 9, 0, tzinfo=KST)
    tried = {"kosis": {"A": 2018}, "queued_at": queued.isoformat()}
    assert rc.already_tried(tried, {"A": 2018}, queued + rc.timedelta(hours=1))
    assert not rc.already_tried(tried, {"A": 2018}, None)  # 아직 다시 받지 않음
    assert not rc.already_tried(tried, {"A": 2018}, queued - rc.timedelta(days=1))  # 예전 작업
    assert not rc.already_tried(
        tried, {"A": 2019}, queued + rc.timedelta(hours=1)
    )  # 수록 기간이 또 바뀜
    assert not rc.already_tried(None, {"A": 2018}, queued)


@pytest.mark.parametrize(
    ("code", "last", "nxt"),
    [
        ("M", 202609, "202610"),
        ("M", 202612, "202701"),
        ("Q", 202504, "202601"),
        ("Q", 202602, "202603"),
        ("H", 202502, "202601"),
        ("A", 2025, "2026"),
        ("D", 20261231, "20270101"),
    ],
)
def test_next_period(code, last, nxt) -> None:
    assert rc.next_period(code, last) == nxt


def test_delta_periods_fetch_only_what_db_lacks() -> None:
    periods = [
        {"PRD_SE": "월", "STRT_PRD_DE": "1970.01", "END_PRD_DE": "2026.10"},
        {"PRD_SE": "분기", "STRT_PRD_DE": "1970 1/4", "END_PRD_DE": "2026 3/4"},
        {"PRD_SE": "년", "STRT_PRD_DE": "1970", "END_PRD_DE": "2025"},
    ]
    delta = rc.delta_periods(periods, {"M": 202609, "A": 2025})
    assert delta == [
        {"PRD_SE": "월", "STRT_PRD_DE": "202610", "END_PRD_DE": "2026.10"},  # 새 달만
        {"PRD_SE": "분기", "STRT_PRD_DE": "1970 1/4", "END_PRD_DE": "2026 3/4"},  # 없는 주기 전체
    ]  # 연은 같아서 뺀다
    assert rc.delta_periods(periods, {"M": 202610, "Q": 202603, "A": 2025}) == []


def test_db_period_reads_compact_dates() -> None:
    # KOSIS가 구분자 없이 "202609"처럼 주면 연도(2026)로만 읽어 새 달을 놓쳤다
    assert rc.db_period("M", "202609") == 202609
    assert rc.db_period("Q", "202603") == 202603
    assert rc.db_period("D", "20261031") == 20261031
    assert rc.db_period("A", "2025") == 2025
    assert rc.db_period("M", "2026.09") == 202609


def _sent(rows):
    return [{"PRD_SE": se, "PRD_DE": de, "SEND_DE": on} for se, de, on in rows]


def test_revised_periods_only_recent_and_after_load() -> None:
    loaded = datetime(2026, 11, 13, 10, 0, tzinfo=KST)
    lasts = {"M": 202609}
    # 표 전체를 다시 보내도(오래된 달까지 같은 날) 최근 3개 시점만 본다
    sent = _sent([("월", f"2026{m:02d}", "2026-11-20") for m in range(1, 10)])
    assert rc.revised_periods(sent, lasts, loaded) == {"M": 202607}
    # 적재한 날 이전에 보낸 자료는 이미 받았다
    assert rc.revised_periods(_sent([("월", "202609", "2026-11-13")]), lasts, loaded) == {}
    # DB에 아직 없는 새 시점은 needs_update가 본다
    assert rc.revised_periods(_sent([("월", "202610", "2026-11-20")]), lasts, loaded) == {}
    # 연 단위: 2년 주기(F)로 와도 DB의 연(A)과 같은 칸
    yearly = _sent([("년", "2024", "2026-11-20"), ("년", "2025", "2026-11-01")])
    assert rc.revised_periods(yearly, {"A": 2025}, loaded) == {"A": 2024}


def test_delta_periods_refetch_from_revised_period() -> None:
    periods = [{"PRD_SE": "월", "STRT_PRD_DE": "197001", "END_PRD_DE": "2026.09"}]
    # 새 시점 없이 고친 시점만 있으면 그 시점부터 끝까지
    out = rc.delta_periods(periods, {"M": 202609}, {"M": 202607})
    assert out[0]["STRT_PRD_DE"] == "202607"
    # 고친 시점이 없고 새 시점도 없으면 받지 않는다
    assert rc.delta_periods(periods, {"M": 202609}) == []
