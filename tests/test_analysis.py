import pytest

from app.services import analysis
from app.services.analysis import AnalysisError


def test_change_and_growth_rate() -> None:
    assert analysis.change(200, 250) == 50
    assert analysis.growth_rate(200, 250) == pytest.approx(25.0)
    assert analysis.growth_rate(200, 150) == pytest.approx(-25.0)


def test_growth_rate_negative_base() -> None:
    # 기준값이 음수(예: 인구 순유출)여도 늘어난 방향이면 양수
    assert analysis.growth_rate(-100, -50) == pytest.approx(50.0)


def test_growth_rate_zero_base() -> None:
    with pytest.raises(AnalysisError):
        analysis.growth_rate(0, 10)


def test_point_change() -> None:
    assert analysis.point_change(15.0, 20.0) == pytest.approx(5.0)


def test_annual_growth_rate() -> None:
    # 100 → 121 (2년) = 연평균 10%
    assert analysis.annual_growth_rate(100, 121, 2) == pytest.approx(10.0)
    with pytest.raises(AnalysisError):
        analysis.annual_growth_rate(0, 121, 2)
    with pytest.raises(AnalysisError):
        analysis.annual_growth_rate(100, 121, 0)


def test_mean() -> None:
    assert analysis.mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    with pytest.raises(AnalysisError):
        analysis.mean([])


def test_share() -> None:
    assert analysis.share(25, 200) == pytest.approx(12.5)
    with pytest.raises(AnalysisError):
        analysis.share(1, 0)


def test_composition() -> None:
    result = analysis.composition({"남자": 60, "여자": 40})
    assert result == pytest.approx({"남자": 60.0, "여자": 40.0})


def test_composition_with_total() -> None:
    # '계' 값이 따로 있으면 항목 합계가 아니라 그 값을 기준으로 계산
    result = analysis.composition({"0-14세": 10, "15-64세": 70}, total=100)
    assert result == pytest.approx({"0-14세": 10.0, "15-64세": 70.0})


def test_rank_with_ties() -> None:
    values = {"서울": 10.0, "부산": 20.0, "대구": 20.0, "인천": 5.0}
    assert analysis.rank(values) == [
        (1, "부산", 20.0),
        (1, "대구", 20.0),
        (3, "서울", 10.0),
        (4, "인천", 5.0),
    ]
    assert analysis.rank(values, descending=False)[0] == (1, "인천", 5.0)


def test_top_and_bottom() -> None:
    values = {"서울": 10.0, "부산": 20.0, "대구": 20.0, "인천": 5.0}
    assert analysis.top(values) == ["부산", "대구"]
    assert analysis.bottom(values) == ["인천"]
    with pytest.raises(AnalysisError):
        analysis.top({})
