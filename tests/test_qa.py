from app.services import qa

# indicators.trend()가 돌려주는 형태 (DB 없이 테스트하기 위해 직접 만든 값)
TREND = {
    "status": "ok",
    "indicator": "총인구",
    "unit": "명",
    "formula": "주민등록 총인구",
    "source": "테스트 출처",
    "basis": "테스트 기준",
    "region": "대구광역시",
    "sex": "계",
    "series": [
        {"year": 2023, "value": 1000},
        {"year": 2024, "value": 900, "diff_from_prev": -100, "rate_from_prev": -10.0},
        {"year": 2025, "value": 810, "diff_from_prev": -90, "rate_from_prev": -10.0},
    ],
    "change": {
        "from_year": 2023,
        "to_year": 2025,
        "diff": -190,
        "diff_unit": "명",
        "rate": -19.0,
        "rate_unit": "%",
    },
    "notes": [],
}


def test_facts_for_llm_hides_values() -> None:
    facts = qa.facts_for_llm(TREND)
    assert facts["overall"] == "감소"
    assert facts["yearly"] == "매년 감소"
    assert facts["period"] == "2023~2025년"
    # LLM에는 값·차이·비율을 넘기지 않는다
    assert "series" not in facts and "change" not in facts


def test_render_trend() -> None:
    text = qa.render(TREND)
    assert "- 2024년: 900명 (전년 대비 -100명, -10.0%)" in text
    assert "- 2023년 → 2025년 전체 변화: -190명, -19.0%" in text
    assert text.endswith("출처: 테스트 출처 (테스트 기준)")


def test_check_numbers() -> None:
    question = "2023년부터 대구 인구 변화"
    # 질문이나 LLM에 넘긴 정보(연도 등)에 있는 숫자는 허용
    assert qa.check_numbers("2023~2025년 대구 인구는 줄었습니다.", question, TREND) == []
    # 계산 결과 값이라도 LLM에 넘기지 않은 숫자는 근거 없는 숫자로 본다
    assert qa.check_numbers("190명 줄었습니다.", question, TREND) == ["190"]


def test_add_yearly_change() -> None:
    result = {
        "indicator": "총인구",
        "series": [{"year": 2024, "value": 200}, {"year": 2025, "value": 250}],
    }
    qa._add_yearly_change(result)
    assert result["series"][1]["diff_from_prev"] == 50
    assert result["series"][1]["rate_from_prev"] == 25.0
