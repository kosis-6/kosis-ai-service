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
    Chart(**chart)


def test_series_keeps_missing_year_as_gap() -> None:
    series = [{"year": y, "value": v} for y, v in ((2020, 1.0), (2021, None), (2022, 3.0))]
    assert [r["value"] for r in charts.build({**BASE, "series": series})["rows"]] == [
        1.0,
        None,
        3.0,
    ]
    one = [{"year": 2020, "value": 1.0}, {"year": 2021, "value": None}]
    assert charts.build({**BASE, "series": one}) is None


def test_region_values_are_bars() -> None:
    values = [
        {"region": "부산광역시", "value": 23.9},
        {"region": "세종특별자치시", "value": None},
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
    assert chart["type"] == "bar" and chart["rows"][0]["label"] == "항목8"
    assert chart["count_unit"] == "건"


def test_no_chart_for_single_value_or_refusal() -> None:
    assert charts.build({**BASE, "region": "부산광역시", "year": 2024, "value": 23.9}) is None
    assert charts.build({"status": "missing_year", "reason": "..."}) is None
    assert charts.build({**BASE, "split": [{"series": [{"year": 1, "value": 1}]}]}) is None
    assert charts.build(None) is None


def test_region_trends_are_several_lines() -> None:
    seoul = {
        **BASE,
        "region": "서울특별시",
        "series": [{"year": 2022, "value": 0.59}, {"year": 2023, "value": 0.55}],
    }
    busan = {
        **BASE,
        "region": "부산광역시",
        "series": [{"year": 2023, "value": 0.66}, {"year": 2024, "value": 0.68}],
    }
    chart = charts.build({"status": "ok", "split_by": "region", "split": [seoul, busan]})
    assert chart["type"] == "line" and chart["series"] == ["서울특별시", "부산광역시"]
    assert chart["title"] == "합계출산율 2022~2024년" and chart["unit"] == "가임여성 1명당 명"
    assert [r["values"] for r in chart["rows"]] == [[0.59, None], [0.55, 0.66], [None, 0.68]]
    assert chart["crowded"] is False
    Chart(**chart)
    names = [f"지역{i}" for i in range(charts.MAX_LINES + 2)]
    many = {"status": "ok", "split_by": "region",
            "split": [{**seoul, "region": n} for n in names]}  # fmt: skip
    crowded = charts.build(many)
    assert crowded["series"] == names
    # 겹쳐 그릴지는 서버가 정해 싣는다: 화면은 이 값을 따른다 (11/26 리뷰: 서버는 막대로
    # 바꿨다고 하고 화면은 지역별 꺾은선을 그렸다)
    assert crowded["crowded"] is True and Chart(**crowded).crowded is True
    nested = {"status": "ok", "split_by": "region", "split": [{"split": [seoul]}, busan]}
    assert charts.build(nested) is None


def test_crowded_follows_the_flag_and_counts_only_old_charts() -> None:
    assert charts.crowded({"type": "line", "series": ["가", "나"], "crowded": True})
    assert not charts.crowded({"type": "line", "series": list("가나다라마바사"), "crowded": False})
    # 이 값이 생기기 전에 저장된 대화의 차트는 선 수로 센다
    assert charts.crowded({"type": "line", "series": list("가나다라마바사")})
    assert not charts.crowded({"type": "bar", "series": None})


def test_region_trends_require_comparable_indicators_units_and_sexes():
    import copy

    a = {
        **BASE,
        "region": "서울",
        "sex": "계",
        "series": [
            {"year": 2022, "value": 10},
            {"year": 2023, "value": 20},
        ],
    }
    for change in ({"indicator": "다른 지표"}, {"unit": "%"}, {"sex": "여"}, {"unit": None}):
        result = {
            "status": "ok",
            "split_by": "region",
            "split": [a, {**a, "region": "부산", **change}],
        }
        before = copy.deepcopy(result)
        assert charts.build(result) is None
        assert "기준이 달라" in charts.omission_reason(result)
        assert result == before


def test_region_trends_require_two_real_points_per_region_and_unique_years():
    a = {
        **BASE,
        "region": "서울",
        "series": [
            {"year": 2022, "value": 10},
            {"year": 2023, "value": 20},
        ],
    }
    for series in (
        [{"year": 2022, "value": None}, {"year": 2023, "value": 20}],
        [{"year": 2022, "value": float("inf")}, {"year": 2023, "value": 20}],
        [{"year": 2022, "value": 10}, {"year": 2022, "value": 20}],
    ):
        result = {"status": "ok", "split_by": "region", "split": [a, {**a, "series": series}]}
        assert charts.build(result) is None
        assert charts.omission_reason(result)


def test_non_finite_values_are_not_sent_to_charts():
    assert (
        charts.build(
            {
                **BASE,
                "series": [
                    {"year": 2020, "value": float("nan")},
                    {"year": 2021, "value": 10},
                    {"year": 2022, "value": 20},
                ],
            }
        )["rows"][0]["value"]
        is None
    )
    assert (
        charts.build(
            {
                **BASE,
                "values": [
                    {"region": "서울", "value": float("inf")},
                    {"region": "부산", "value": 1},
                ],
            }
        )
        is None
    )


def _bars(key: str, n: int) -> dict:
    items = [{"region": f"지역{i}", "value": 100 - i, "rank": i + 1} for i in range(n)]
    return charts.build({**BASE, "year": 2024, key: items})


def test_bars_are_cut_only_past_fifteen() -> None:
    for key in ("values", "ranking"):
        exact = _bars(key, charts.MAX_BARS)
        assert len(exact["rows"]) == charts.MAX_BARS and exact["note"] is None
        over = _bars(key, charts.MAX_BARS + 1)
        assert len(over["rows"]) == charts.MAX_BARS
        assert f"전체 {charts.MAX_BARS + 1}개 지역 중" in over["note"]


def test_long_comparison_keeps_the_highest_and_lowest() -> None:
    """11/27 차트 점검: 17개 시도 비교에서 답은 '가장 낮은 지역은 세종'인데 막대에 세종이 없었다"""
    values = [{"region": f"지역{i}", "value": float(v)} for i, v in enumerate(range(20, 37))]
    values.insert(5, {"region": "가장 낮은 곳", "value": 1.0})
    values.insert(9, {"region": "가장 높은 곳", "value": 99.0})
    chart = charts.build({**BASE, "year": 2024, "values": values})
    labels = [r["label"] for r in chart["rows"]]
    assert len(labels) == charts.MAX_BARS
    assert "가장 낮은 곳" in labels and "가장 높은 곳" in labels
    # 남긴 막대는 원래 순서를 지킨다
    order = [v["region"] for v in values]
    assert labels == sorted(labels, key=order.index)
    assert "가장 높은 곳과 가장 낮은 곳은 포함" in chart["note"]


def test_long_comparison_keeps_the_same_tied_lowest_as_the_answer() -> None:
    from app.services import qa

    values = [{"region": f"지역{i}", "value": 17 - i if i < 15 else 1} for i in range(17)]
    result = {**BASE, "year": 2024, "values": values, "gap": 16, "gap_unit": BASE["unit"]}
    chart = charts.build(result)
    assert "가장 낮은 지역은 지역16" in qa.summary(result)
    assert len(chart["rows"]) == charts.MAX_BARS
    assert chart["rows"][-1]["label"] == "지역16"


def test_ranking_cut_stays_the_top_fifteen() -> None:
    """순위는 위에서부터 자른다 (중간에 꼴찌를 끼우면 순위가 이어지지 않는다)"""
    chart = _bars("ranking", charts.MAX_BARS + 5)
    assert [r["label"] for r in chart["rows"]] == [f"지역{i}" for i in range(charts.MAX_BARS)]
    assert f"위에서 {charts.MAX_BARS}개" in chart["note"]


def test_ranking_keeps_its_own_order() -> None:
    ranking = [
        {"region": "가", "value": 1, "rank": 1},
        {"region": "나", "value": 3, "rank": 2},
        {"region": "다", "value": 2, "rank": 3},
    ]
    chart = charts.build({**BASE, "year": 2024, "ranking": ranking})
    assert [r["label"] for r in chart["rows"]] == ["가", "나", "다"]


def test_composition_switches_to_bars_past_six_parts() -> None:
    result = {"status": "ok", "indicator": "인구 구성", "unit": "%", "year": 2024}
    six = [{"label": f"항목{i}", "share": float(i)} for i in range(1, charts.MAX_PARTS + 1)]
    chart = charts.build({**result, "parts": six})
    assert chart["type"] == "parts" and [r["label"] for r in chart["rows"]][0] == "항목1"
    seven = [{"label": f"항목{i}", "share": float(i)} for i in range(1, charts.MAX_PARTS + 2)]
    chart = charts.build({**result, "parts": seven})
    assert chart["type"] == "bar" and chart["rows"][0]["label"] == f"항목{charts.MAX_PARTS + 1}"


def test_many_parts_are_cut_to_fifteen_bars() -> None:
    """11/27 차트 점검: 구성 항목이 7개를 넘어 막대로 바뀌면 15개 제한이 없었다"""
    result = {"status": "ok", "indicator": "연령별 인구 구성", "unit": "%", "year": 2024}
    exact = [{"label": f"항목{i}", "share": float(i)} for i in range(1, charts.MAX_BARS + 1)]
    chart = charts.build({**result, "parts": exact})
    assert len(chart["rows"]) == charts.MAX_BARS and chart["note"] is None
    many = [{"label": f"항목{i}", "share": float(i)} for i in range(1, 21)]
    chart = charts.build({**result, "parts": many})
    assert chart["type"] == "bar" and len(chart["rows"]) == charts.MAX_BARS
    assert [r["label"] for r in chart["rows"]][:2] == ["항목20", "항목19"]
    assert chart["note"] == f"전체 20개 항목 중 비율이 큰 {charts.MAX_BARS}개만 그렸습니다."
    Chart(**chart)


def test_region_lines_are_crowded_only_past_six() -> None:
    region = {**BASE, "series": [{"year": 2022, "value": 1}, {"year": 2023, "value": 2}]}

    def lines(n: int) -> dict:
        split = [{**region, "region": f"지역{i}"} for i in range(n)]
        return charts.build({"status": "ok", "split_by": "region", "split": split})

    assert lines(charts.MAX_LINES)["crowded"] is False
    assert lines(charts.MAX_LINES + 1)["crowded"] is True


def test_view_fits_matches_what_the_screen_offers():
    line = {"type": "line", "series": ["서울", "부산"]}
    crowded_line = {"type": "line", "crowded": True}
    bar = {"type": "bar"}
    assert all(charts.view_fits(line, v) for v in ("line", "bar", "table"))
    assert not charts.view_fits(crowded_line, "bar")  # 선이 많으면 막대 보기가 없다
    assert charts.view_fits(crowded_line, "table")
    assert charts.view_fits(bar, "bar") and not charts.view_fits(bar, "line")
    # 구성비의 [차트] 버튼은 막대를 보낸다. 꺾은선을 보내 표 → 차트 저장이 409로 거절됐다
    parts = {"type": "parts"}
    assert charts.view_fits(parts, "bar") and charts.view_fits(parts, "table")
    assert not charts.view_fits(parts, "line")
