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
        ("1990년대생 인구", {1990}),
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
