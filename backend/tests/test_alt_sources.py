"""다른 조사의 공표값으로 답하기 (지표 사전의 alt_sources)

"인구총조사 기준 영도구 중위연령"처럼 조사를 말하면 주민등록인구로 계산하지 않고
그 조사의 표로 답한다.
"""

import pytest

from app.services import ask_session, indicators, qa


@pytest.fixture(autouse=True)
def no_statistics_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """조사 선택·계산 경로를 검증하며 지역과 통계 원자료는 읽지 않는다."""
    monkeypatch.setattr(qa, "_full_region_names", lambda *a: None)
    monkeypatch.setattr(qa, "_ground_regions", lambda *a: None)
    monkeypatch.setattr(indicators, "find_regions", lambda *a: [])
    monkeypatch.setattr(indicators, "_data_years", lambda: (1990, 2025))
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)
    monkeypatch.setattr(qa, "_lookup_span", lambda *a: (1990, 2025))
    monkeypatch.setattr(
        indicators,
        "_survey_of",
        lambda table: "인구총조사" if table.startswith(("DT_1IN", "DT_1PM")) else "인구동향조사",
    )


def test_year_change_keeps_survey():
    previous = parsed(year=2025, asked={"survey": "인구총조사", "recent": 5})
    changed, _ = ask_session.apply_changes(previous, {"year": 2020})
    assert changed["year"] == 2020
    assert changed["asked"] == {"survey": "인구총조사"}
    assert previous["asked"]["recent"] == 5


def test_saved_trend_keeps_survey():
    previous = parsed(type="trend", asked={"survey": "인구총조사", "recent": 5})
    saved = ask_session.effective(
        previous,
        {"status": "ok", "series": [{"year": 2020, "value": 1}, {"year": 2025, "value": 2}]},
    )
    assert saved["asked"] == {"survey": "인구총조사"}
    assert (saved["start_year"], saved["end_year"]) == (2020, 2025)


def test_inherited_indicator_keeps_survey_without_repeating_it():
    previous = parsed(asked={"survey": "인구총조사"})
    inherited, _ = ask_session.inherit(
        parsed(indicator=None, regions=["서울"]),
        {"indicator": "default", "regions": "user", "period": "default"},
        previous,
        {"indicator": "user", "regions": "user", "period": "default"},
        "서울은?",
    )
    assert inherited["indicator"] == "중위연령"
    assert inherited["asked"]["survey"] == "인구총조사"


@pytest.mark.parametrize("name", ["고령인구 비율", "유소년인구 비율", "생산연령인구 비율"])
def test_census_ratio_uses_same_sex_in_numerator_and_denominator(name, monkeypatch):
    selected = []

    def value(spec, region, year, sex, ages):
        source = qa.lookup._effective(spec["sources"][0], sex)
        selected.append((sex, source["item_id"], source["den_item_id"]))
        return {"status": "ok", "indicator": name, "value": 20.2, "year": year}

    monkeypatch.setattr(qa.lookup, "value", value)
    monkeypatch.setattr(indicators, "value", lambda *a: {"status": "missing_data"})
    result = qa.compute(
        parsed(
            indicator=name, regions=["서울"], year=2025, sex="여", asked={"survey": "인구총조사"}
        )
    )
    assert result["status"] == "ok"
    assert selected == [("여", "T02", "T02")]


def test_unavailable_survey_sex_is_not_silently_total():
    result = qa.compute(
        parsed(indicator="중위연령", year=2025, sex="여", asked={"survey": "인구총조사"})
    )
    assert result["status"] == "unknown_indicator"
    assert "성별" in result["reason"]


def parsed(**kw):
    base = {"type": "value", "indicator": "중위연령", "regions": ["영도구"], "sex": "계"}
    return {**base, **kw}


@pytest.mark.parametrize(
    "question, indicator, survey",
    [
        ("인구총조사 기준으로 영도구 중위연령 다시 구해줘", "중위연령", "인구총조사"),
        ("인구주택총조사로 영도구 중위연령", "중위연령", "인구총조사"),
        ("센서스 기준 영도구 중위연령", "중위연령", "인구총조사"),
        ("영도구 중위연령 외국인 포함해서", "중위연령", "인구총조사"),
        ("장래인구추계 기준 서울 성비", "성비", "장래인구추계"),
        ("추계인구로 서울 노인 비율", "고령인구 비율", "장래인구추계"),
        ("영도구 중위연령", "중위연령", None),  # 조사를 말하지 않음 → 사전의 계산
        ("인구총조사 기준 서울 합계출산율", "합계출산율", None),  # 그 지표에 다른 조사 표가 없음
        ("외국인 포함 서울 인구", "총인구", "인구총조사"),
    ],
)
def test_survey_in_question(question: str, indicator: str, survey: str | None) -> None:
    p = qa.correct(question, parsed(indicator=indicator))
    assert (p.get("asked") or {}).get("survey") == survey


def test_alt_spec_is_lookup_of_that_survey() -> None:
    sp = indicators.alt_spec(indicators.spec("중위연령"), "인구총조사")
    assert sp["method"] == "lookup" and sp["name"] == "중위연령" and sp["unit"] == "세"
    assert [s["table"] for s in sp["sources"]] == ["DT_1IN2030"]
    assert "survey" not in sp["sources"][0]
    assert sp["note"].startswith("인구총조사 기준입니다. 실제로 사는 곳에서 외국인까지 센 인구")
    proj = indicators.alt_spec(indicators.spec("중위연령"), "장래인구추계")
    assert [s["level"] for s in proj["sources"]] == ["시도", "전국"]
    assert indicators.alt_spec(indicators.spec("합계출산율"), "인구총조사") is None


def test_alt_ratio_is_computed_from_counts() -> None:
    """인구총조사는 고령인구 비율을 공표하지 않는다: 연령별 인구로 나눴다고 밝힌다"""
    sp = indicators.alt_spec(indicators.spec("고령인구 비율"), "인구총조사")
    assert sp["sources"][0]["den_item_id"] == "T00"
    assert "인구총조사 연령별 인구로 계산" in sp["formula"]
    assert sp["note"].startswith("인구총조사 연령별 인구로 계산했습니다 (비율은 공표되지 않음)")


def test_every_alt_source_is_well_formed() -> None:
    """지표 사전의 alt_sources: 알려진 조사 · 표 · 지역 수준"""
    for name, sp in indicators.specs().items():
        for a in sp.get("alt_sources") or []:
            assert a["survey"] in indicators.ALT_NOTES, name
            assert a["table"] and a["level"] in ("전국", "시도", "시군구"), name


def test_survey_routes_to_alt_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """조사를 말하면 그 조사의 표로 조회하고, 지표 정의의 연령("노인" 65세 이상)을
    다시 나누지 않는다"""
    seen = {}

    def fake_value(sp, region, year, sex="계", ages=None):
        seen.update(spec=sp, ages=ages)
        return {"status": "ok", "indicator": sp["name"], "value": 20.2, "year": year}

    monkeypatch.setattr(qa.lookup, "value", fake_value)
    p = qa.correct(
        "인구총조사 기준 서울 노인 비율", parsed(indicator="고령인구 비율", regions=["서울"])
    )
    r = qa.compute(p)
    assert r["value"] == 20.2
    assert seen["spec"]["survey"] == "인구총조사"
    assert seen["spec"]["sources"][0]["table"] == "DT_1IN1503"
    assert seen["ages"] is None


def test_without_survey_keeps_dictionary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("조사를 말하지 않으면 다른 표를 보지 않는다")

    monkeypatch.setattr(indicators, "alt_spec", boom)
    p = qa.correct("영도구 중위연령", parsed())
    assert not (p.get("asked") or {}).get("survey")


def test_multi_indicator_keeps_dictionary() -> None:
    """여러 지표를 함께 물으면 바꾸지 않는다 (외국인 포함 총조사 인구에 등록외국인을 또 더함)"""
    p = qa.correct(
        "춘천 인구, 외국인까지 합쳐서 등록외국인이랑 몇 명이야?",
        parsed(indicator="총인구", regions=["춘천"], indicators=["총인구", "등록외국인 수"]),
    )
    assert not (p.get("asked") or {}).get("survey")


def test_total_population_alt_keeps_sex_and_ages() -> None:
    """총인구의 인구총조사 표는 성별 · 연령으로 나뉘어 있어 "20대 여자"도 그 표로 답한다"""
    sp = indicators.alt_spec(indicators.spec("총인구"), "인구총조사")
    src = sp["sources"][0]
    assert src["age_dim"] == "c2" and src["by_sex"]["여"]["item_id"] == "T02"


def test_default_value_shown_next_to_alt(monkeypatch: pytest.MonkeyPatch) -> None:
    """다른 조사로 답하면 같은 해 주민등록인구 기준 값을 함께 둔다"""

    def fake(q):
        if (q.get("asked") or {}).get("survey"):
            return {"status": "ok", "value": 55.2, "unit": "세", "year": 2025, "notes": []}
        assert q["year"] == 2025  # 같은 해로 비교
        return {"status": "ok", "value": 56.8, "unit": "세", "year": 2025}

    monkeypatch.setattr(qa, "_compute", fake)
    r = qa.compute({"type": "value", "indicator": "중위연령", "asked": {"survey": "인구총조사"}})
    # 연도를 말하지 않은 질문이라 가장 최근 자료 안내(_add_latest_note)가 뒤에 함께 붙는다
    assert r["notes"] == [
        "2025년 주민등록인구 기준으로는 56.8세입니다",
        "가장 최근 자료인 2025년 기준으로 답했습니다",
    ]


@pytest.mark.parametrize(
    "question, survey",
    [
        ("인구총조사로 보면?", "인구총조사"),
        ("외국인 포함하면?", "인구총조사"),
        ("장래인구추계로는?", "장래인구추계"),
        ("인구총조사 기준 의성군 노령화지수", None),  # 지표 · 지역을 말함: 새 질문
        ("서울은?", None),  # 조사를 말하지 않음
    ],
)
def test_survey_switch_keeps_previous_conditions(question: str, survey: str | None) -> None:
    """이어 묻기 "인구총조사로 보면?": 앞 조건 그대로 조사만 바꾼다"""
    prev = parsed(indicator="고령인구 비율", regions=["서울"])
    got = ask_session.survey_switch(question, prev)
    if survey is None:
        assert got is None
    else:
        assert got["asked"]["survey"] == survey
        assert got["indicator"] == "고령인구 비율" and got["regions"] == ["서울"]


def test_survey_switch_without_alt_table_explains() -> None:
    """앞 지표에 그 조사의 표가 없으면 기본 표로 다시 답하고 이유를 밝힌다 (합계출산율)"""
    got = ask_session.survey_switch("인구총조사로 보면?", parsed(indicator="합계출산율"))
    assert got["asked"] == {"survey_missing": "인구총조사"}


def test_survey_word_is_not_indicator() -> None:
    """ "인구총조사 기준으로 다시 구해줘"의 "인구"는 지표(총인구)가 아니다: 앞 지표를 이어받는다"""
    assert not ask_session.names_indicator("인구총조사 기준으로 다시 구해줘", {})
    assert ask_session.names_indicator("인구총조사 기준 노령화지수", {})


def test_inherited_indicator_gets_survey() -> None:
    prev = parsed()
    p, _ = ask_session.inherit(
        parsed(indicator="총인구", regions=[]),
        {"indicator": "user", "regions": "default", "period": "default"},
        prev,
        {"indicator": "user", "regions": "user", "period": "default"},
        "인구총조사 기준으로 다시 구해줘",
    )
    assert p["indicator"] == "중위연령" and p["asked"]["survey"] == "인구총조사"


def test_unavailable_level_names_the_survey(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qa,
        "_compute",
        lambda q: {
            "status": "missing_data",
            "reason": "중위연령은 시도 단위까지만 자료가 있습니다.",
        },
    )
    r = qa.compute({"type": "value", "indicator": "중위연령", "asked": {"survey": "장래인구추계"}})
    assert r["reason"] == "장래인구추계 중위연령은 시도 단위까지만 자료가 있습니다."


@pytest.mark.parametrize(
    "question, indicator, missing",
    [
        ("인구총조사 기준 서울 합계출산율", "합계출산율", "인구총조사"),
        ("장래인구추계 기준 서울 이혼 건수", "이혼 건수", "장래인구추계"),
        ("인구총조사 기준 서울 미혼 비율", "미혼 비율", None),  # 원래 인구총조사 표
        ("외국인 포함 서울 출생아 수", "출생아 수", None),  # 조사 이름을 말하지 않음
        ("인구총조사 기준 영도구 중위연령", "중위연령", None),  # 대체 표로 답함
    ],
)
def test_survey_missing(question: str, indicator: str, missing: str | None) -> None:
    p = qa.correct(question, parsed(indicator=indicator, regions=["서울"]))
    assert (p.get("asked") or {}).get("survey_missing") == missing


def test_survey_missing_leads_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """물은 조사의 값이 없으면 이유를 답 첫머리에 두고, 원래 조사의 값임을 밝힌다"""
    monkeypatch.setattr(qa, "_compute", lambda q: {"status": "ok", "value": 0.632, "notes": []})
    monkeypatch.setattr(qa.indicators, "source_surveys", lambda sp: ["인구동향조사"])
    monkeypatch.setattr(qa, "_summary", lambda r: "2025년 서울특별시의 합계출산율은 0.632명입니다.")
    r = qa.compute(
        {"type": "value", "indicator": "합계출산율", "asked": {"survey_missing": "인구총조사"}}
    )
    assert r["notes"] == []  # ※ 줄로 따로 두지 않는다
    assert qa.summary(r) == (
        "합계출산율은 인구동향조사로만 공표되어 인구총조사 기준 값은 없습니다.\n"
        "인구동향조사 기준 2025년 서울특별시의 합계출산율은 0.632명입니다."
    )
