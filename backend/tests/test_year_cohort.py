from datetime import date

import pytest

from app.services import ask_followup, qa
from app.services import slots as sl

TODAY = date(2026, 11, 22)


@pytest.fixture(autouse=True)
def no_statistics_db(monkeypatch):
    monkeypatch.setattr(qa.indicators, "find_regions", lambda *a: [])
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (1990, 2025))
    monkeypatch.setattr(qa, "_full_region_names", lambda *a: None)
    monkeypatch.setattr(qa, "_ground_regions", lambda *a: None)


def _slots(question: str) -> dict:
    return sl.correct(question, sl.empty_slots(question))


@pytest.mark.parametrize(
    ("question", "span"),
    [
        ("1990년대 서울 인구 변화", [(1990, 1999)]),
        ("90년대 출생아", [(1990, 1999)]),
        ("2020년대 출생아", [(2020, 2026)]),
        ("2030년대 인구 전망", [(2030, 2039)]),
        ("2000년대 초반 부산 인구", [(2000, 2003)]),
        ("90년대 후반 출생아", [(1997, 1999)]),
        ("1990년대 말에", [(1997, 1999)]),
        ("1990년대 초등학생 수", [(1990, 1999)]),
        ("1990년대 말레이시아 이민", [(1990, 1999)]),
        ("2000년대와 2010년대 비교", [(2000, 2009), (2010, 2019)]),
        ("1990년대생 인구", []),
        ("2000년 대비 2020년", []),
        ("2000년 대구 인구", []),
    ],
)
def test_decades(question: str, span: list[tuple[int, int]]) -> None:
    assert sl.decades(question, TODAY) == span


@pytest.mark.parametrize(
    ("question", "born"),
    [
        ("1990년생 인구", {1990}),
        ("90년생 인구", {1990}),
        ("1990년대생 인구", set(range(1990, 2000))),
        ("2020년 생산가능인구", set()),
    ],
)
def test_birth_years(question: str, born: set[int]) -> None:
    assert sl.birth_years(question) == born


@pytest.mark.parametrize(
    ("question", "year", "year_end"),
    [
        ("1990년대 서울 인구 변화", 1990, 1999),
        ("1990년대부터 2010년까지", 1990, 2010),
        ("2000년대 초반 부산 인구", 2000, 2003),
        ("1990년생 인구", None, None),
        ("1990년대생 인구", None, None),
        ("1990년생이 2020년에 몇 명", 2020, None),
        ("2000년 대비 2020년 인구", 2000, 2020),
        ("2020년 생산가능인구", 2020, None),
    ],
)
def test_slot_year(question: str, year: int | None, year_end: int | None) -> None:
    c = _slots(question)["conditions"]
    assert (c["year"], c["year_end"]) == (year, year_end)


def test_current_decade_is_not_projection() -> None:
    assert _slots("2020년대 출생아")["projection"] is False
    assert _slots("2030년대 인구")["projection"] is True


def _parsed(**kw) -> dict:
    base = {"type": "value", "indicator": "총인구", "regions": ["서울"], "year": None}
    return {**base, **kw}


@pytest.mark.parametrize(
    ("question", "parsed", "expected"),
    [
        ("1990년대 서울 인구", _parsed(year=1990), ("trend", None, 1990, 1999)),
        ("2000년대 초반 서울 인구", _parsed(year=2000), ("trend", None, 2000, 2003)),
        (
            "1990년대부터 2010년까지 서울 인구 변화",
            _parsed(type="trend", start_year=1990, end_year=2010),
            ("trend", None, 1990, 2010),
        ),
        ("1990년생 서울 인구", _parsed(year=1990), ("value", None, None, None)),
        (
            "2000년 대비 2020년 서울 인구",
            _parsed(type="trend", start_year=2000, end_year=2020),
            ("trend", None, 2000, 2020),
        ),
    ],
)
def test_indicator_path(question: str, parsed: dict, expected: tuple) -> None:
    p = qa.correct(question, parsed, TODAY)
    assert (p["type"], p.get("year"), p.get("start_year"), p.get("end_year")) == expected


def test_followup_decade_reply_is_a_year() -> None:
    assert ask_followup.has_explicit_year("1990년대")
    assert ask_followup._only_years("1990년대")


@pytest.mark.parametrize(
    ("question", "ref", "span"),
    [
        ("1990년생", 2025, (35, 35)),
        ("90년대생", 2025, (26, 35)),
        ("2020년대생", 2025, (0, 5)),
        ("2030년생", 2025, None),
    ],
)
def test_birth_age(question: str, ref: int, span: tuple | None) -> None:
    assert sl.birth_age(sl.birth_spans(question), ref) == span


@pytest.mark.parametrize(
    ("question", "age"),
    [
        ("1990년생 인구", (36, 36, "2026년 기준 만 나이")),
        ("90년대생 서울 인구", (27, 36, "2026년 기준 만 나이")),
        ("1990년생이 2020년에 몇 명", (30, 30, "2020년 기준 만 나이")),
    ],
)
def test_slot_birth_to_age(question: str, age: tuple, monkeypatch) -> None:
    monkeypatch.setattr(sl, "date", type("D", (), {"today": staticmethod(lambda: TODAY)}))
    a = _slots(question)["age"]
    assert (a["lo"], a["hi"], a["basis"]) == age


def test_slot_named_age_group_wins_over_birth_year() -> None:
    assert _slots("청년 1990년생")["age"]["expr"] == "청년"


def test_indicator_birth_year_becomes_age() -> None:
    p = qa.correct("1990년생 서울 인구", _parsed(year=1990), TODAY)
    assert p["year"] is None and p["born"]["expr"] == "1990년생"
    q = dict(p)
    assert qa.born_ages(q, 2025) == [(35, 35)]
    assert q["age_group"] == {"expr": "1990년생", "range": "35세", "basis": "2025년 기준 만 나이"}
    q = {**p, "year": 2020}
    assert qa.born_ages(q, 2025) == [(30, 30)]


def test_indicator_birth_decade_trend_drops_birth_years() -> None:
    parsed = _parsed(type="trend", start_year=2000, end_year=2009)
    p = qa.correct("2000년대생 서울 인구 변화", parsed, TODAY)
    assert (p.get("start_year"), p.get("end_year")) == (None, None)
    assert p["born"]["spans"] == [[2000, 2009]]


def test_single_age_label() -> None:
    from app.services import indicators

    assert indicators.age_label((35, 35)) == "35세"
    assert indicators.age_label((30, 39)) == "30대"


def test_separate_cohorts_do_not_include_intermediate_birth_years():
    spans = sl.birth_spans("1990년생과 2000년생 인구")
    assert sl.birth_ages(spans, 2025) == [(25, 25), (35, 35)]
    assert sl.birth_age(spans, 2025) is None
    assert sl.birth_ages([(1990, 1999), (1995, 2004)], 2025) == [(21, 35)]
    assert sl.birth_ages([], 2025) == []


def test_cohort_population_trend_uses_each_years_age(monkeypatch):
    calls = []

    def value(indicator, region, year, sex, ages):
        calls.append((year, ages))
        return {
            "status": "ok",
            "indicator": "인구",
            "kind": "count",
            "unit": "명",
            "year": year,
            "value": 100 - (year - 2020),
            "notes": [],
        }

    monkeypatch.setattr(qa.indicators, "value", value)
    q = _parsed(
        type="trend",
        start_year=2020,
        end_year=2022,
        born={"expr": "1990년생", "spans": [[1990, 1990]]},
    )
    result = qa._compute_population(q, "총인구", "trend", ["서울"], 2025, "계", [(32, 32)], 2025)
    assert calls == [(2020, [(30, 30)]), (2021, [(31, 31)]), (2022, [(32, 32)])]
    assert [r["value"] for r in result["series"]] == [100, 99, 98]
    assert result["change"]["diff"] == -2
    assert result["change"]["rate"] == -2
    assert result["indicator"] == "1990년생 총인구"


def test_separate_cohorts_are_computed_separately(monkeypatch):
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)
    calls = []

    def value(indicator, region, year, sex, ages):
        calls.append(ages)
        return {"status": "ok", "value": 10, "notes": []}

    monkeypatch.setattr(qa.indicators, "value", value)
    q = _parsed(
        year=2025, born={"expr": "1990년생과 2000년생", "spans": [[1990, 1990], [2000, 2000]]}
    )
    result = qa.compute(q)
    assert result["status"] == "ok"
    assert len(result["split"]) == 2
    assert calls == [[(35, 35)], [(25, 25)]]


def test_cohort_trend_before_birth_has_no_values():
    def never_called(*args):
        pytest.fail("출생 전에는 인구 값을 조회하면 안 된다")

    q = {"born": {"expr": "2030년생", "spans": [[2030, 2030]]}}
    result = qa._cohort_trend(q, "총인구", 2020, 2025, never_called)
    assert result["status"] == "missing_data"


def test_value_before_birth_does_not_fall_back_to_total_population(monkeypatch):
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)
    q = qa.correct("2030년생 서울 인구", _parsed(), TODAY)
    assert q["born"]["spans"] == [[2030, 2030]]
    assert qa.compute(q)["status"] == "missing_data"


def test_cohort_lookup_trend_uses_each_years_age(monkeypatch):
    calls = []
    monkeypatch.setattr(qa, "_lookup_span", lambda *a: (2020, 2025))

    def value(spec, region, year, sex, ages):
        calls.append((year, ages))
        return {"status": "ok", "value": year, "kind": "count", "unit": "명", "notes": []}

    monkeypatch.setattr(qa.lookup, "value", value)
    q = {
        "start_year": 2020,
        "end_year": 2021,
        "born": {"expr": "1990년생", "spans": [[1990, 1990]]},
    }
    result = qa._lookup_trend(q, {"name": "출생 집단"}, "서울", 2025, "계", [(35, 35)])
    assert result["status"] == "ok"
    assert calls == [(2020, [(30, 30)]), (2021, [(31, 31)])]


def test_latest_cohort_value_uses_actual_latest_year(monkeypatch):
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)
    monkeypatch.setattr(qa.indicators, "latest_year", lambda *a: 2026)
    calls = []

    def value(indicator, region, year, sex, ages):
        calls.append((year, ages))
        return {"status": "ok", "value": 10, "notes": []}

    monkeypatch.setattr(qa.indicators, "value", value)
    q = _parsed(asked={"latest": True}, born={"expr": "1990년생", "spans": [[1990, 1990]]})
    result = qa.compute(q)
    assert result["status"] == "ok"
    assert calls == [(2026, [(36, 36)])]
    assert any("2026년 기준" in n for n in result["notes"])
