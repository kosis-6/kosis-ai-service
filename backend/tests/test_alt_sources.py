"""다른 조사의 공표값으로 답하기 (지표 사전의 alt_sources)

"인구총조사 기준 영도구 중위연령"처럼 조사를 말하면 주민등록인구로 계산하지 않고
그 조사의 표로 답한다.
"""

import pytest

from app.services import indicators, qa


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
    assert sp["note"].startswith("인구총조사 공표값입니다")
    proj = indicators.alt_spec(indicators.spec("중위연령"), "장래인구추계")
    assert [s["level"] for s in proj["sources"]] == ["시도", "전국"]
    assert indicators.alt_spec(indicators.spec("합계출산율"), "인구총조사") is None


def test_alt_ratio_is_computed_from_counts() -> None:
    """인구총조사는 고령인구 비율을 공표하지 않는다: 연령별 인구로 나눴다고 밝힌다"""
    sp = indicators.alt_spec(indicators.spec("고령인구 비율"), "인구총조사")
    assert sp["sources"][0]["den_item_id"] == "T00"
    assert "인구총조사 연령별 인구로 계산" in sp["formula"]
    assert sp["note"].startswith("인구총조사 인구(외국인 포함, 11월 1일 기준)로 계산한 값입니다")


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
