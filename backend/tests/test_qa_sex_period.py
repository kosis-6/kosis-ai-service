from datetime import date

import pytest

from app.services import indicators, qa


@pytest.fixture(autouse=True)
def no_region_db(monkeypatch):
    monkeypatch.setattr(qa, "_full_region_names", lambda *args: None)
    monkeypatch.setattr(qa, "_ground_regions", lambda *args: None)
    monkeypatch.setattr(indicators, "find_regions", lambda *args: [])
    monkeypatch.setattr(indicators, "_data_years", lambda: (1990, 2025))


def correct(question, **overrides):
    parsed = {"type": "value", "indicator": "총인구", "regions": ["서울"], "sex": "계"}
    return qa.correct(question, {**parsed, **overrides}, date(2026, 11, 20))


@pytest.mark.parametrize("sex", ["남", "여"])
@pytest.mark.parametrize("question", ["서울 인구", "서울 청소년 인구", "서울 청소년기 인구"])
def test_unspecified_sex_is_total(question, sex):
    assert correct(question, sex=sex)["sex"] == "계"


@pytest.mark.parametrize(
    "question,sex",
    [("서울 소년 인구", "남"), ("서울 소녀 인구", "여"), ("서울 엄마 평균 나이", "여")],
)
def test_implied_sex_is_preserved(question, sex):
    assert correct(question, sex=sex)["sex"] == sex


@pytest.mark.parametrize(
    "question",
    [
        "서울 남자 여자 합계 인구",
        "서울 여자랑 남자 인구 합계",
        "서울 남성과 여성 합산 인구",
        "서울 남녀 합계 인구",
        "서울 성별 구분 없이 인구",
    ],
)
def test_combined_sexes_are_not_split(question):
    parsed = correct(question, sex="남")
    assert parsed["sex"] == "계"
    assert not parsed.get("split_sex")


@pytest.mark.parametrize("question", ["서울 남녀 인구 비교", "서울 남자랑 여자 인구 비교"])
def test_sex_comparison_is_split(question):
    parsed = correct(question, type="compare", sex="남")
    assert parsed["type"] == "value"
    assert parsed["sex"] == "계"
    assert parsed["split_sex"]


@pytest.mark.parametrize("question", ["서울 인구 추이 보여줘", "요새 서울 인구 추이 보여줘"])
def test_unspecified_period_is_cleared(question):
    parsed = correct(question, type="trend", start_year=2015, end_year=2024)
    assert parsed.get("start_year") is None
    assert parsed.get("end_year") is None


def test_split_compute_runs_both_sexes(monkeypatch):
    monkeypatch.setattr(
        qa, "_compute", lambda q: {"status": "ok", "indicator": "총인구", "sex": q["sex"]}
    )
    result = qa.compute(correct("서울 남녀 인구 비교", type="compare"))
    assert [part["sex"] for part in result["split"]] == ["남", "여"]
