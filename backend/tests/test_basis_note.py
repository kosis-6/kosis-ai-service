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


@pytest.mark.parametrize(
    "question, llm, indicator, pair",
    [
        ("요즘 애를 얼마나 낳아?", "출생아 수", "합계출산율", True),  # D-096
        ("요즘 아이 몇 명 낳아?", None, "합계출산율", True),
        ("서울은 애를 얼마나 낳아?", "합계출산율", "합계출산율", True),
        ("출산 얼마나 해?", "출생아 수", "합계출산율", True),
        ("여자 한 명이 애를 몇 명 낳아?", "출생아 수", "합계출산율", False),
        ("작년에 애 몇 명 낳았어?", "출생아 수", "출생아 수", False),  # 몇 명 태어났는지
        ("작년에 애기 몇 명 태어났어?", "출생아 수", "출생아 수", False),
    ],
)
def test_how_many_babies(question: str, llm: str | None, indicator: str, pair: bool) -> None:
    """ "얼마나 낳아"는 합계출산율로 답하고 출생아 수를 함께. 뜻이 분명한 말은 그 지표 하나"""
    p = qa.correct(question, {"type": "value", "indicator": llm, "regions": [], "sex": "계"})
    assert p["indicator"] == indicator
    assert (p.get("indicators") == ["합계출산율", "출생아 수"]) is pair
    assert bool((p.get("asked") or {}).get("birth_pair")) is pair


def test_how_many_babies_explains_tfr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(q):
        return {"status": "ok", "indicator": q["indicator"], "value": 1, "year": 2025}

    monkeypatch.setattr(qa, "compute", fake)
    q = {
        "type": "value",
        "indicators": ["합계출산율", "출생아 수"],
        "regions": ["전국"],
        "asked": {"birth_pair": True},
    }
    r = qa._compute(q)
    assert r["split"][0]["notes"][0].startswith("합계출산율은 여자 한 명이 평생(15~49세) 낳을")


def test_per_woman_question_explains_tfr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "_compute", lambda q: {"status": "ok", "value": 0.799, "notes": []})
    p = qa.correct(
        "여자 한 명이 애를 몇 명 낳아?",
        {"type": "value", "indicator": None, "regions": [], "sex": "계"},
    )
    r = qa.compute(p)
    assert r["notes"][0] == qa.TFR_MEANING
