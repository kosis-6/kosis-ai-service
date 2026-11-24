"""기준이 여럿인 말: 되묻지 않고 기본 기준으로 답하고 다른 기준을 안내한다
(qa.basis_note · age_basis_note)"""

import tomllib

import pytest

from app.services import indicators, qa


@pytest.fixture(autouse=True)
def no_statistics_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기준 안내와 질문 보정만 검증한다. 통계 원자료는 CI에 없다."""
    monkeypatch.setattr(indicators, "find_regions", lambda *a: [])
    monkeypatch.setattr(indicators, "_data_years", lambda: (1990, 2025))
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)


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
        # HCX가 지표 대신 주제('저출산')를 줬다: 통계표 바로 계산으로 가 뜻 · 최신 안내가 빠졌다
        ("요즘 애 많이 낳아?", "저출산", "합계출산율", True),
        ("요즘 애 많이 안 낳지?", "출생아 수", "합계출산율", True),
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
    assert r["split"][0]["notes"][0].startswith(qa.TFR_MEANING)


def test_per_woman_question_explains_tfr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "_compute", lambda q: {"status": "ok", "value": 0.799, "notes": []})
    p = qa.correct(
        "여자 한 명이 애를 몇 명 낳아?",
        {"type": "value", "indicator": None, "regions": [], "sex": "계"},
    )
    r = qa.compute(p)
    assert r["notes"][0] == qa.TFR_MEANING


def test_every_indicator_has_definition() -> None:
    """지표 사전의 지표는 모두 답에 붙일 설명(definition)이 있다"""
    from app.services import indicators

    assert [n for n, sp in indicators.specs().items() if not sp.get("definition")] == []


def test_definition_note_on_dictionary_answer() -> None:
    """지표 사전으로 답하면 설명을 안내 맨 앞에, 통계표 바로 계산은 붙이지 않는다"""
    r = {"status": "ok", "indicator": "노령화지수", "value": 1, "notes": ["기준 안내"]}
    qa._add_definition_note(r)
    assert r["notes"][0].startswith("노령화지수는 0~14세 인구 100명당")
    qa._add_definition_note(r)
    assert len(r["notes"]) == 2  # 두 번 붙이지 않는다
    dyn = {"status": "ok", "indicator": "노령화지수", "method": "dynamic", "value": 1}
    qa._add_definition_note(dyn)
    assert not dyn.get("notes")


def test_render_puts_definitions_first_and_drops_contained() -> None:
    """나뉜 답: 설명을 앞에 모으고, 다른 안내에 통째로 든 설명은 한 번만"""
    r = {
        "status": "ok",
        "split_by": "indicator",
        "split": [
            {
                "status": "ok",
                "indicator": "합계출산율",
                "notes": [
                    qa.BIRTH_PAIR_NOTE,
                    qa.TFR_MEANING,
                    "가장 최근 자료인 2025년 기준으로 답했습니다",
                ],
            },
            {
                "status": "ok",
                "indicator": "출생아 수",
                "notes": ["출생아 수는 그해(1월 1일~12월 31일) 태어나 출생 신고된 아이 수입니다"],
            },
        ],
    }
    notes = [line for line in qa.render(r).splitlines() if line.startswith("※")]
    assert notes[0] == f"※ {qa.BIRTH_PAIR_NOTE}"
    assert notes[1].startswith("※ 출생아 수는")
    assert notes[-1] == "※ 가장 최근 자료인 2025년 기준으로 답했습니다"
    assert sum(qa.TFR_MEANING in n for n in notes) == 1


def test_latest_note_when_dynamic_filled_year() -> None:
    """통계표 바로 계산이 채운 마지막 해(year_filled)는 사용자가 말한 연도가 아니다"""
    r = {"status": "ok", "indicator": "1인가구", "year": 2025, "value": 1}
    qa._add_latest_note({"year": 2025, "year_filled": True}, r)
    assert "가장 최근 자료인 2025년 기준으로 답했습니다" in r["notes"]
    r2 = {"status": "ok", "indicator": "1인가구", "year": 2025, "value": 1}
    qa._add_latest_note({"year": 2025}, r2)  # 사용자가 2025년이라고 말했다
    assert not r2.get("notes")


@pytest.mark.parametrize(
    "question, found",
    [
        ("2020년 대구 성비", True),  # 띄어 쓴 '구 성비'는 '구성비'가 아니다 (11/24)
        ("달서구 성비", True),
        ("달서구성비", True),  # 붙여 써도 '구'는 지역 이름의 끝
        ("대구성비", True),
        ("중구 성비", True),
        ("연령 구성비", False),  # 한 낱말인 '구성비'는 그대로 막는다
        ("대구 연령 구성비", False),
        ("부산 여성 비율", False),
    ],
)
def test_sex_ratio_not_blocked_across_words(question: str, found: bool) -> None:
    from app.services import indicators

    assert ("성비" in [n for *_, n in indicators.find_in_text(question)]) is found
    assert (indicators.excluded_phrase("성비", question) is None) is found


@pytest.mark.parametrize("question", ["대전 출생아", "대전출생아"])
def test_region_boundary_still_blocks_split_alias(question: str) -> None:
    """지역 경계가 별칭('전출')을 가르면 그 별칭이 우연이다: 전출자 수로 세지 않는다"""
    from app.services import indicators

    assert [n for *_, n in indicators.find_in_text(question)] == ["출생아 수"]
