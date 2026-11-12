"""차트 정보(charts.build): 계산 결과의 모양에 따라 차트 종류와 행을 고른다"""

from app.schemas.ask import Chart
from app.services import charts

BASE = {"status": "ok", "indicator": "합계출산율", "unit": "가임여성 1명당 명", "sex": "계"}


def test_series_is_line() -> None:
    series = [{"year": 2022, "value": 0.593}, {"year": 2023, "value": 0.552}]
    chart = charts.build({**BASE, "region": "서울특별시", "series": series})
    assert chart["type"] == "line" and chart["category"] == "연도"
    assert chart["title"] == "서울특별시 합계출산율 2022~2023년"
    assert chart["rows"] == [{"label": "2022", "value": 0.593}, {"label": "2023", "value": 0.552}]
    assert chart["unit"] == "가임여성 1명당 명"
    Chart(**chart)  # 응답 스키마에 맞는다


def test_series_keeps_missing_year_as_gap() -> None:
    """자료가 없는 연도는 값을 채우지 않고 비워 둔다 (화면에서 선이 끊긴다)"""
    series = [{"year": y, "value": v} for y, v in ((2020, 1.0), (2021, None), (2022, 3.0))]
    assert [r["value"] for r in charts.build({**BASE, "series": series})["rows"]] == [
        1.0,
        None,
        3.0,
    ]
    one = [{"year": 2020, "value": 1.0}, {"year": 2021, "value": None}]
    assert charts.build({**BASE, "series": one}) is None  # 점 하나로는 추이를 그릴 수 없다


def test_region_values_are_bars() -> None:
    values = [
        {"region": "부산광역시", "value": 23.9},
        {"region": "세종특별자치시", "value": None},  # 값이 없는 지역은 그리지 않는다
        {"region": "서울특별시", "value": 19.44},
    ]
    chart = charts.build(
        {**BASE, "indicator": "고령인구 비율", "unit": "%", "year": 2024, "values": values}
    )
    assert chart["type"] == "bar" and chart["title"] == "2024년 고령인구 비율"
    assert [r["label"] for r in chart["rows"]] == ["부산광역시", "서울특별시"]


def test_long_ranking_is_cut_with_note() -> None:
    ranking = [{"region": f"지역{i}", "value": 100 - i, "rank": i + 1} for i in range(40)]
    chart = charts.build(
        {**BASE, "sex": "여", "year": 2024, "order": "높은 순", "ranking": ranking}
    )
    assert chart["title"] == "2024년 합계출산율 (여) (높은 순)"
    assert len(chart["rows"]) == charts.MAX_BARS and chart["rows"][0]["label"] == "지역0"
    assert "40개 지역 중 위에서 15개" in chart["note"]


def test_composition_is_parts_or_bars() -> None:
    parts = [
        {"label": "유소년인구(0~14세)", "count": 835816, "share": 8.96},
        {"label": "생산연령인구(15~64세)", "count": 6682364, "share": 71.61},
        {"label": "고령인구(65세 이상)", "count": 1813648, "share": 19.44},
    ]
    result = {
        "status": "ok",
        "indicator": "인구 구성",
        "unit": "%",
        "region": "서울특별시",
        "year": 2024,
    }
    chart = charts.build({**result, "parts": parts})
    assert chart["type"] == "parts" and chart["title"] == "2024년 서울특별시 인구 구성"
    assert chart["rows"][1] == {"label": "생산연령인구(15~64세)", "value": 71.61, "count": 6682364}
    assert chart["unit"] == "%" and chart["count_unit"] == "명"
    Chart(**chart)

    many = [{"label": f"항목{i}", "count": i, "share": float(i)} for i in range(1, 9)]
    chart = charts.build({**result, "count_unit": "건", "parts": many})
    assert (
        chart["type"] == "bar" and chart["rows"][0]["label"] == "항목8"
    )  # 조각이 많으면 큰 순 막대
    assert chart["count_unit"] == "건"


def test_no_chart_for_single_value_or_refusal() -> None:
    assert charts.build({**BASE, "region": "부산광역시", "year": 2024, "value": 23.9}) is None
    assert charts.build({"status": "missing_year", "reason": "..."}) is None
    assert charts.build({**BASE, "split": [{"series": [{"year": 1, "value": 1}]}]}) is None
    assert charts.build(None) is None
