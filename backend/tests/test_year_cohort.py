"""'년대'는 기간, '년생'은 출생 연도: 조회 연도로 읽지 않는다 (슬롯·지표 경로 모두)"""

from datetime import date

import pytest

from app.services import ask_followup, qa
from app.services import slots as sl

TODAY = date(2026, 11, 22)


def _slots(question: str) -> dict:
    return sl.correct(question, sl.empty_slots(question))


@pytest.mark.parametrize(
    ("question", "span"),
    [
        ("1990년대 서울 인구 변화", [(1990, 1999)]),
        ("90년대 출생아", [(1990, 1999)]),
        ("2020년대 출생아", [(2020, 2026)]),  # 지나는 연대는 올해까지
        ("2030년대 인구 전망", [(2030, 2039)]),
        ("2000년대 초반 부산 인구", [(2000, 2003)]),
        ("90년대 후반 출생아", [(1997, 1999)]),
        ("1990년대 말에", [(1997, 1999)]),
        ("1990년대 초등학생 수", [(1990, 1999)]),  # '초등학생'의 초는 초반이 아니다
        ("1990년대 말레이시아 이민", [(1990, 1999)]),
        ("2000년대와 2010년대 비교", [(2000, 2009), (2010, 2019)]),
        ("1990년대생 인구", []),  # 출생 연대
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
        ("1990년대생 인구", set(range(1990, 2000))),  # 출생 연대는 열 해 모두
        ("2020년 생산가능인구", set()),  # 띄어 쓴 '생'은 출생이 아니다
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
        ("1990년생 인구", None, None),  # 출생 연도 → 최신 자료
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
        ("1990년생", 2025, (35, 35)),  # 연말 기준 만 나이
        ("90년대생", 2025, (26, 35)),
        ("2020년대생", 2025, (0, 5)),  # 아직 다 태어나지 않은 연대는 기준 해까지
        ("2030년생", 2025, None),
    ],
)
def test_birth_age(question: str, ref: int, span: tuple | None) -> None:
    assert sl.birth_age(sl.birth_spans(question), ref) == span


@pytest.mark.parametrize(
    ("question", "age"),
    [
        ("1990년생 인구", (36, 36, "2026년 기준 만 나이")),  # 연도가 없으면 올해 기준
        ("90년대생 서울 인구", (27, 36, "2026년 기준 만 나이")),
        ("1990년생이 2020년에 몇 명", (30, 30, "2020년 기준 만 나이")),  # 물은 연도 기준
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
    # 계산할 때 자료 마지막 해 기준으로 나이를 정하고 답에 기준을 남긴다
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
