"""기준이 여럿인 말: 되묻지 않고 기본 기준으로 답하고 다른 기준을 안내한다
(qa.basis_note · age_basis_note)"""

import tomllib

import pytest

from app.services import indicators, qa


@pytest.mark.parametrize(
    ("term", "name", "default"),
    [
        ("이혼율", "조이혼율", "조이혼율"),
        ("혼인율", "조혼인율", "조혼인율"),
        ("출산율", "합계출산율", "합계출산율"),
        ("사망률", "조사망률", "조사망률"),
        ("외국인 수", "외국인주민 수", "외국인주민"),
        ("외국인 비율", "외국인주민 비율", "외국인주민"),
        ("부양비", "총부양비", "총부양비"),
        ("노인 비율", "고령인구 비율", "65세 이상"),
        ("인구", "총인구", "주민등록인구"),
    ],
)
def test_ambiguous_term_gets_basis_note(term, name, default):
    sp = indicators.spec(term)
    assert sp["name"] == name
    note = qa.basis_note(sp, term)
    assert note and default in note


@pytest.mark.parametrize(("term", "name"), [("조이혼율", "조이혼율"), ("합계출생율", "합계출산율")])
def test_exact_or_misspelled_name_has_no_basis_note(term, name):
    """정확한 이름이나 오타는 기준을 고른 것이 아니라 안내하지 않는다
    (오타는 기존 '…로 보고 답했습니다')"""
    sp = indicators.spec(term)
    assert sp["name"] == name
    assert qa.basis_note(sp, term) is None


def test_basis_terms_are_aliases():
    """basis_terms는 그 지표의 이름·별칭이어야 한다 (안 그러면 안내가 붙을 일이 없다)"""
    with open(indicators.NOTES_PATH, "rb") as f:
        rows = tomllib.load(f)["indicators"]
    for ind in rows:
        names = {indicators._norm(n) for n in [ind["name"], *ind.get("aliases", [])]}
        for term in ind.get("basis_terms", []):
            assert indicators._norm(term) in names, (ind["name"], term)
        assert bool(ind.get("basis_terms")) == bool(ind.get("basis_note")), ind["name"]


def test_age_basis_note():
    basis = "청년기본법 19~34세 (고용 통계는 15~29세)"
    group = {"expr": "청년", "range": "19~34세", "basis": basis}
    assert qa.age_basis_note(group) == (
        "'청년'은 19~34세로 계산했습니다 (청년기본법 19~34세 · 고용 통계는 15~29세)"
    )
