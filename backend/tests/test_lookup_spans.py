"""기간별로 다른 표 이어 쓰기 (지표 사전 sources의 years, lookup._spans·_trend_series)와
적재 안 된 표가 계산 단계에서 삼켜지지 않는지 (qa.compute → _compute_fetching)
(DB 없이 lookup 내부 함수를 바꿔 끼운다)
"""

import pytest

from app.core.stats_db import StatsTableMissing
from app.services import indicators, lookup, qa
from pipeline import catalog

NEW = {"table": "T_NEW", "level": "시군구", "item_id": "A"}  # 2022~2025 (DB에서 읽음)
OLD = {"table": "T_OLD", "level": "전국", "national_only": True, "years": [1980, 1980],
       "item_id": "B", "note": "옛 조사 기준"}  # fmt: skip
SPEC = {"name": "시험 비율", "unit": "%", "kind": "ratio", "sources": [NEW, OLD]}


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> list:
    """T_NEW는 2022~2025, T_OLD는 1980 값만 있는 것처럼. 읽은 (표, 시작, 끝)을 기록한다"""
    reads = []
    values = {"T_NEW": {2022: 10.0, 2023: 11.0, 2024: 12.0, 2025: 13.0}, "T_OLD": {1980: 5.0}}

    def item_years(tbl, items):
        if tbl == "T_OLD":
            raise AssertionError("years가 있는 출처는 DB에서 기간을 읽지 않는다")
        return 2022, 2025

    def fetch(src, codes, y0, y1, age_codes=None):
        reads.append((src["table"], y0, y1, age_codes))
        return {(y, codes[0]): v for y, v in values[src["table"]].items() if y0 <= y <= y1}

    def table(tbl):
        return {"tbl_nm": f"{tbl} 표", "org_nm": "기관", "basis": "", "start_year": None}

    monkeypatch.setattr(lookup, "_item_years", item_years)
    monkeypatch.setattr(lookup, "_fetch", fetch)
    monkeypatch.setattr(lookup, "_table", table)
    monkeypatch.setattr(indicators, "kosis_url", lambda t: f"https://kosis/{t}")
    return reads


def test_years_in_spec_is_used_without_db(db) -> None:
    assert lookup._years(OLD) == (1980, 1980)
    assert lookup._years(NEW) == (2022, 2025)


def test_spans_split_range_by_source(db) -> None:
    spans = lookup._spans(SPEC, "전국", 1980, 2025, "계")
    assert [(s["table"], a, b) for s, a, b in spans] == [
        ("T_NEW", 2022, 2025),
        ("T_OLD", 1980, 1980),
    ]
    # 시도 이하에서는 전국 전용 출처를 쓰지 않는다
    assert [s["table"] for s, _, _ in lookup._spans(SPEC, "시도", 1980, 2025, "계")] == ["T_NEW"]


def test_trend_series_joins_sources(db) -> None:
    reg = {"region_code": "00", "display": "전국", "kind": "전국"}
    src, label, notes, vals, earlier = lookup._trend_series(
        SPEC, reg, 1980, 2025, "계", None, {"status": "out_of_range"}
    )
    assert src["table"] == "T_NEW"  # 마지막 연도의 출처가 대표 출처
    assert sorted(y for y, _ in vals) == [1980, 2022, 2023, 2024, 2025]
    assert [e["tbl_id"] for e in earlier] == ["T_OLD"]
    assert earlier[0]["url"] == "https://kosis/T_OLD"
    assert notes == ["1980년: 옛 조사 기준"]
    assert ("T_NEW", 1980, 2025, None) not in db  # 표마다 자기 기간만 읽는다


def test_trend_series_needs_two_sources(db) -> None:
    reg = {"region_code": "11", "display": "서울", "kind": "시도"}
    err = {"status": "out_of_range", "reason": "2022~2025년만 있습니다."}
    out = lookup._trend_series(SPEC, reg, 1980, 2025, "계", None, err)
    assert out == {**err, "region": "서울"}  # 서울은 옛 표(전국 전용)를 못 쓴다


def test_age_default_used_when_no_age_asked() -> None:
    src = {"age_dim": "c2", "age_total": "000", "age_min": 15, "age_default": ["1004", "120"]}
    assert lookup._ages(src, None) == (["1004", "120"], "", [])
    assert lookup._ages({"age_dim": "c2", "age_total": "000"}, None) == (["000"], "", [])


def test_past_year_sources_are_not_service_tables() -> None:
    ind = {"sources": [NEW, OLD]}
    assert catalog.base_sources(ind) == [NEW]  # run_all 검사·indicator_sources에서 뺀다


def test_spec_tables_skip_past_year_sources(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "specs", lambda: {"시험 비율": SPEC})
    assert indicators.spec_tables() == ["T_NEW"]  # 서버 시작 때 미리 받지 않는다


def test_unmarried_rate_has_1980_source() -> None:
    """실제 지표 사전: 미혼 비율 1980년은 인구총조사 표 (질문할 때 받음)"""
    indicators.specs.cache_clear()
    old = [s for s in indicators.specs()["미혼 비율"]["sources"] if s.get("years")]
    assert [(s["table"], s["years"]) for s in old] == [("DT_1IN8003", [1980, 1980])]
    assert "000" not in old[0]["age_default"] and "990" not in old[0]["age_default"]


# ---- qa: 적재 안 된 표 오류를 계산 단계에서 삼키지 않는다 ---------------------------------


def test_compute_raises_missing_table_for_fetch(monkeypatch) -> None:
    def missing(*a, **k):
        raise StatsTableMissing("적재 안 됨", "DT_1IN8003")

    monkeypatch.setattr(qa, "_compute_lookup", missing)
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: False)
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    q = {"type": "trend", "indicator": "미혼 비율", "regions": ["전국"], "year": 2025}
    with pytest.raises(StatsTableMissing):
        qa.compute(q)


def test_compute_still_reports_missing_schema_as_error(monkeypatch) -> None:
    def missing(*a, **k):
        raise StatsTableMissing("통계 테이블이 없습니다")  # 표 ID 없음 = DB 자체가 비어 있음

    monkeypatch.setattr(qa, "_compute_lookup", missing)
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: False)
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    q = {"type": "trend", "indicator": "미혼 비율", "regions": ["전국"], "year": 2025}
    assert qa.compute(q)["status"] == "error"


def test_one_region_two_years_compare_is_trend(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(qa, "_lookup_trend", lambda *a: seen.append("trend") or {"status": "ok"})
    monkeypatch.setattr(qa.lookup, "value", lambda *a: seen.append("value") or {"status": "ok"})
    sp = indicators.specs()["미혼 비율"]
    q = {"start_year": 1980, "end_year": 2025}
    qa._compute_lookup(q, sp, "compare", ["전국"], 2025, "계", None)
    qa._compute_lookup({}, sp, "compare", ["전국"], 2025, "계", None)
    assert seen == ["trend", "value"]


def test_gap_year_change_is_not_called_previous_year() -> None:
    r = {
        "region": "전국",
        "sex": "계",
        "indicator": "미혼 비율",
        "unit": "%",
        "series": [
            {"year": 1980, "value": 26.93},
            {"year": 2022, "value": 29.54, "diff_from_prev": 2.61},
            {"year": 2023, "value": 29.52, "diff_from_prev": -0.02},
        ],
        "change": {"from_year": 1980, "to_year": 2023, "diff": 2.59, "diff_unit": "%p"},
    }
    lines = qa._body({**r, "status": "ok", "kind": "ratio"})
    assert any("2022년: 29.54% (1980년 대비 +2.61%p)" in x for x in lines)
    assert any("2023년: 29.52% (전년 대비 -0.02%p)" in x for x in lines)
