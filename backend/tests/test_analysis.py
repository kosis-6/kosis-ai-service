import pytest

from app.services import analysis
from app.services.analysis import AnalysisError


def test_change_and_growth_rate() -> None:
    assert analysis.change(200, 250) == 50
    assert analysis.growth_rate(200, 250) == pytest.approx(25.0)
    assert analysis.growth_rate(200, 150) == pytest.approx(-25.0)


def test_growth_rate_negative_base() -> None:
    assert analysis.growth_rate(-100, -50) == pytest.approx(50.0)


def test_growth_rate_zero_base() -> None:
    with pytest.raises(AnalysisError):
        analysis.growth_rate(0, 10)


def test_point_change() -> None:
    assert analysis.point_change(15.0, 20.0) == pytest.approx(5.0)


def test_share() -> None:
    assert analysis.share(25, 200) == pytest.approx(12.5)
    with pytest.raises(AnalysisError):
        analysis.share(1, 0)


def test_composition() -> None:
    result = analysis.composition({"남자": 60, "여자": 40})
    assert result == pytest.approx({"남자": 60.0, "여자": 40.0})


def test_composition_with_total() -> None:
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


def test_times() -> None:
    assert analysis.times(30, 20) == pytest.approx(1.5)
    with pytest.raises(AnalysisError):
        analysis.times(1, 0)


def test_rank_rows_ties_share_rank() -> None:
    rows = [
        {"region": "가", "value": 0.72},
        {"region": "나", "value": 0.8},
        {"region": "다", "value": 0.72},
    ]
    ranked = analysis.rank_rows(rows)
    assert [(r["region"], r["rank"]) for r in ranked] == [("나", 1), ("가", 2), ("다", 2)]
    ranked = analysis.rank_rows(rows, descending=False)
    assert [(r["region"], r["rank"]) for r in ranked] == [("가", 1), ("다", 1), ("나", 3)]
