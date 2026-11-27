from datetime import date

import pytest

from app.core import stats_db
from app.services import charts, qa


@pytest.fixture(autouse=True)
def no_stats_db(monkeypatch: pytest.MonkeyPatch) -> None:

    def no_db():
        pytest.fail("QA 단위 테스트에서 실제 통계 DB를 호출했습니다")

    monkeypatch.setattr(stats_db, "_engine", no_db)
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)


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
VALUE = {
    "status": "ok",
    "indicator": "고령인구 비율",
    "unit": "%",
    "region": "부산광역시",
    "year": 2025,
    "sex": "여",
    "value": 27.66,
    "source": "테스트 출처",
    "basis": "테스트 기준",
    "tbl_id": "DT_TEST",
    "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_TEST",
}


def test_render_trend() -> None:
    text = qa.render(TREND)
    assert "- 2024년: 900명 (전년 대비 -100명, -10.0%)" in text
    assert "- 2023년 → 2025년 전체 변화: -190명, -19.0%" in text
    assert text.endswith("출처: 테스트 출처 (테스트 기준)")


def test_summary_trend() -> None:
    assert qa.summary(TREND) == (
        "대구광역시의 총인구는 2023년 1,000명, 2025년 810명입니다. "
        "2023년 대비 190명 감소했습니다(증감률 -19.0%). 이 기간 동안 총인구는 매년 감소했습니다."
    )


def test_summary_trend_turning_year() -> None:
    r = {
        **TREND,
        "series": [
            {"year": 2023, "value": 1000},
            {"year": 2024, "value": 1100, "diff_from_prev": 100},
            {"year": 2025, "value": 1050, "diff_from_prev": -50},
        ],
        "change": {"from_year": 2023, "to_year": 2025, "diff": 50, "diff_unit": "명"},
    }
    text = qa.summary(r)
    assert "매년" not in text
    assert text.endswith("총인구는 2024년까지 증가하다가 이후 감소했습니다.")


def test_compute_group_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    values = [{"region": "인천광역시", "value": 2}, {"region": "부산광역시", "value": 1}]
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(qa, "_compute_lookup", lambda *a: {"status": "ok", "values": values})
    q = {
        "type": "compare",
        "indicator": "혼인 건수",
        "regions": ["인천", "부산"],
        "group": "5대 광역시",
    }
    assert qa.compute(q)["answer_note"] == "5대 광역시는 인천광역시, 부산광역시 기준입니다."


def test_calc_error_hides_internal_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a):
        raise KeyError("raw.stat_value DT_TEST")

    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(qa, "_compute_lookup", broken)
    r = qa.compute({"type": "value", "indicator": "혼인 건수", "regions": ["서울"], "year": 2024})
    assert r["status"] == "error" and r["reason"] == qa.CALC_ERROR_TEXT
    assert len(r["error_id"]) == 8 and "raw" not in str(r) and "DT_TEST" not in str(r)


def test_summary_value_with_sex() -> None:
    assert qa.summary(VALUE) == "2025년 부산광역시의 여성 고령인구 비율은 27.66%입니다."


def test_summary_aging_index_meaning() -> None:
    r = {**VALUE, "indicator": "노령화지수", "unit": "유소년인구 100명당 명", "sex": "계"}
    r["value"] = 210.5
    assert qa.summary(r) == (
        "2025년 부산광역시의 노령화지수는 210.5입니다. "
        "유소년인구(0~14세) 100명당 65세 이상 인구가 210.5명이라는 뜻입니다."
    )


def test_summary_value_spells_out_rate_unit() -> None:
    r = {**VALUE, "indicator": "조혼인율", "unit": "인구 1000명당 건", "sex": "계", "value": 3.8}
    assert qa.summary(r) == "2025년 부산광역시의 조혼인율은 인구 1,000명당 3.8건입니다."


def test_summary_trend_spells_out_rate_unit() -> None:
    r = {
        **TREND,
        "indicator": "조혼인율",
        "unit": "인구 1000명당 건",
        "series": [{"year": 2015, "value": 5.4}, {"year": 2020, "value": 3.8}],
        "change": {"from_year": 2015, "to_year": 2020, "diff": -1.6, "diff_unit": "건"},
    }
    assert qa.summary(r).startswith(
        "대구광역시의 조혼인율은 2015년 인구 1,000명당 5.4건, 2020년 3.8건입니다. "
        "2015년 대비 1.6건 감소했습니다."
    )


def test_summary_compare_two() -> None:
    r = {
        "status": "ok",
        "indicator": "고령인구 비율",
        "unit": "%",
        "year": 2025,
        "sex": "계",
        "values": [
            {"region": "부산광역시", "value": 25.01},
            {"region": "서울특별시", "value": 20.43},
        ],
        "gap": 4.58,
        "gap_unit": "%p",
    }
    assert qa.summary(r) == (
        "2025년 고령인구 비율은 부산광역시가 서울특별시보다 4.58%p 높습니다"
        "(부산광역시 25.01%, 서울특별시 20.43%, 1.22배)."
    )


def test_summary_rank_top_n() -> None:
    r = {
        "status": "ok",
        "indicator": "고령인구 비율",
        "unit": "%",
        "year": 2025,
        "sex": "계",
        "level": "시도",
        "order": "높은 순",
        "total_regions": 17,
        "ranking": [
            {"rank": 1, "region": "전라남도", "value": 28.1},
            {"rank": 2, "region": "경상북도", "value": 27.05},
        ],
    }
    assert qa.summary(r) == (
        "2025년 고령인구 비율이 가장 높은 시도 2곳은 전라남도(28.1%), 경상북도(27.05%)입니다."
    )
    r["ranking"] = r["ranking"][:1]
    assert qa.summary(r) == "2025년 고령인구 비율이 가장 높은 시도는 전라남도(28.1%)입니다."


def test_summary_composition() -> None:
    r = {
        "status": "ok",
        "indicator": "인구 구성",
        "unit": "%",
        "region": "서울특별시",
        "year": 2025,
        "sex": "계",
        "by": "age",
        "total": 1000,
        "parts": [
            {"label": "유소년인구(0~14세)", "count": 100, "share": 10.0},
            {"label": "생산연령인구(15~64세)", "count": 700, "share": 70.0},
            {"label": "고령인구(65세 이상)", "count": 200, "share": 20.0},
        ],
    }
    assert qa.summary(r) == (
        "2025년 서울특별시 인구 1,000명의 연령대별 구성은 유소년인구(0~14세) 10.0%, "
        "생산연령인구(15~64세) 70.0%, 고령인구(65세 이상) 20.0%입니다. "
        "가장 큰 비중은 생산연령인구(15~64세)입니다."
    )
    assert "- 고령인구(65세 이상): 200명 (20.0%)" in qa.render(r)


def test_compute_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_composition(*args):
        calls.append(args)
        return {"status": "ok"}

    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "composition", fake_composition)
    assert qa.compute({"type": "composition", "regions": []})["status"] == "missing_region"
    qa.compute({"type": "composition", "indicator": "남녀 비중", "regions": ["서울"], "by": "sex"})
    assert calls == [("서울", 2025, "sex", "계")]


def test_compute_composition_without_parts_answers_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = "나눠 볼 구성 항목이 없어 구성비 대신 값으로 답했습니다"
    value = {"status": "ok", "indicator": "출생아 수", "region": "서울특별시", "year": 2024}
    value["value"] = 1
    spec = {"name": "출생아 수", "method": "lookup", "sources": [{"table": "T"}]}
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: spec)
    monkeypatch.setattr(qa.indicators, "canonical_name", lambda term: term)
    monkeypatch.setattr(qa.lookup, "needs_split", lambda sp, sex: False)
    monkeypatch.setattr(qa.lookup, "value", lambda *args: dict(value))
    asked = {"type": "composition", "indicator": "출생아 수", "regions": ["서울"], "year": 2024}
    r = qa.compute(asked)
    assert r["status"] == "ok" and r["value"] == 1
    assert r["notes"] == [f"출생아 수는 {note}"]
    assert "parts_dim" not in qa.summary(r) and f"※ 출생아 수는 {note}" in qa.render(r)

    spec = {"name": "고령인구 비율", "method": "population"}
    monkeypatch.setattr(qa.indicators, "value", lambda *args: {**value, "notes": ["기존 주의"]})
    r = qa.compute({**asked, "indicator": "고령인구 비율"})
    assert r["status"] == "ok" and r["notes"] == ["기존 주의", f"고령인구 비율은 {note}"]
    monkeypatch.setattr(qa.indicators, "value", lambda *args: {"status": "missing_data"})
    assert qa.compute({**asked, "indicator": "고령인구 비율"}) == {
        "status": "missing_data",
        "asked": {"top": None},
    }


def test_causal_question_refuses_and_points_to_trend_table() -> None:
    r = qa._limit_result("causal_question", TREND)
    text = qa.summary(r)
    assert r["status"] == "causal_question" and r["source"] == TREND["source"]
    assert text.startswith("원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 답할 수 없습니다.")
    assert "대구광역시 총인구의 변화는 아래 통계표에서 확인할 수 있습니다" in text
    assert not any(ch.isdigit() for ch in text)
    assert "출처: 테스트 출처" in qa.render(r) and qa.sources(r)[0]["name"] == "테스트 출처"


def test_limit_without_indicator_asks_for_one() -> None:
    r = qa._limit_result("causal_question", None)
    assert "궁금한 지표와 지역·기간을 넣어 다시 질문해 주세요" in qa.summary(r)
    p = qa._limit_result("prediction_question", TREND)
    assert qa.summary(p).startswith("앞으로의 변화는 지금까지의 통계로 예측해 답할 수 없습니다.")
    assert "장래인구추계" in qa.summary(p)


def test_band_mismatch_text() -> None:
    bands = {
        "status": "band_mismatch",
        "reason": "사망자 수은(는) 표의 연령 구간이 질문한 62~67세 구간과 맞지 않습니다.",
        "available_bands": ["60~64세", "65~69세"],
        "alternative": "대신 볼 수 있는 구간: 60~64세, 65~69세",
    }
    assert qa.summary(bands).endswith("60~64세, 65~69세 (구간을 바꿔 다시 질문해 주세요)")
    cond = {
        **bands,
        "available_bands": None,
        "alternative": "대신 조건 없는 전국 출생아 수는 확인할 수 있습니다",
    }
    assert qa.summary(cond).endswith("확인할 수 있습니다.")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."},
            "어느 지역인지 질문에 없습니다. 지역을 넣어 다시 질문해 주세요.",
        ),
        (
            {"status": "unknown_indicator", "indicator": "혼인율"},
            "'혼인율'은 아직 지원하지 않는 지표입니다. 아래 지원 지표로 다시 질문해 주세요.",
        ),
        (
            {"status": "parse_error", "reason": "질문을 이해하지 못했습니다."},
            "질문을 이해하지 못했습니다.",
        ),
    ],
)
def test_summary_refusal(result: dict, expected: str) -> None:
    assert qa.summary(result) == expected


def test_out_of_range_with_projection() -> None:
    projection = {
        "status": "ok",
        "indicator": "고령인구 비율",
        "region": "서울특별시",
        "year": 2030,
        "value": 24.2,
        "unit": "%",
        "scenario": "중위",
        "source": "장래인구추계",
        "basis": "2022년 기준",
        "notes": ["주민등록인구와 기준이 달라 실적과 직접 이어서 비교하지 않습니다."],
    }
    r = {
        "status": "out_of_range",
        "reason": "수집된 주민등록인구 자료는 2008~2025년(연간)만 있습니다.",
        "suggestion": projection,
    }
    answer = f"{qa.summary(r)}\n\n{qa.render(r)}"
    assert answer.startswith("수집된 주민등록인구 자료는 2008~2025년(연간)만 있습니다. 대신")
    assert answer.count("실제 통계가 아닌 추계값입니다") == 1
    assert "출처: 장래인구추계 (2022년 기준)" in answer


def test_josa() -> None:
    assert qa._josa("총인구", "은는") == "총인구는"
    assert qa._josa("고령인구 비율", "은는") == "고령인구 비율은"
    assert qa._josa("부산광역시", "이가") == "부산광역시가"


def test_sources_and_link() -> None:
    assert qa.sources(VALUE) == [
        {"name": "테스트 출처", "basis": "테스트 기준", "tbl_id": "DT_TEST", "url": VALUE["url"]}
    ]
    assert qa.render(VALUE).endswith(f"통계표: {VALUE['url']}")
    assert qa.sources({"status": "missing_region"}) == []


def test_add_yearly_change() -> None:
    result = {
        "indicator": "총인구",
        "series": [{"year": 2024, "value": 200}, {"year": 2025, "value": 250}],
    }
    qa._add_yearly_change(result)
    assert result["series"][1]["diff_from_prev"] == 50
    assert result["series"][1]["rate_from_prev"] == 25.0


def test_json_objects() -> None:
    text = '```json\n{"type": "value", "sex": "남"}\n{"type": "value", "sex": "여"}\n```\n※ 참고'
    assert [o["sex"] for o in qa._json_objects(text)] == ["남", "여"]
    assert qa._json_objects("JSON 없음") == []


def test_refusal_says_not_implemented_or_not_collected() -> None:
    collected = {
        "status": "unknown_indicator",
        "indicator": "외국인 근로자 수",
        "gap": "not_implemented",
        "related": ["시도별 외국인주민 현황 (TX_11025_A000_A)"],
    }
    assert "수집한 통계표(시도별 외국인주민 현황 (TX_11025_A000_A))에 관련 자료가 있지만" in (
        qa.summary(collected)
    )
    missing = {"status": "unknown_indicator", "indicator": "1인 가구", "gap": "not_collected"}
    assert "아직 수집하지 않은 통계입니다." in qa.summary(missing)


def _region_trend(region, values, start=2023):
    series = [{"year": start + i, "value": v} for i, v in enumerate(values)]
    return {**TREND, "region": region, "series": series, "notes": []}


def test_compute_trend_for_each_region(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = []

    def fake(q, sp, kind, regions, *rest):
        asked.append((kind, regions))
        return _region_trend(regions[0], [1000, 900, 810])

    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(qa, "_compute_lookup", fake)
    q = {"type": "trend", "indicator": "출생아 수", "regions": ["서울", "부산"]}
    r = qa.compute(q)
    assert asked == [("trend", ["서울"]), ("trend", ["부산"])]
    assert r["split_by"] == "region" and [p["region"] for p in r["split"]] == ["서울", "부산"]
    assert r["split"][1]["series"][1]["diff_from_prev"] == -100
    asked.clear()
    qa.compute({**q, "aggregate": "sum"})
    assert asked == [("trend", ["서울"]), ("trend", ["부산"])]


def test_trend_for_many_regions_keeps_every_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(
        qa,
        "_compute_lookup",
        lambda q, sp, kind, regions, *rest: _region_trend(regions[0], [3, 2, 1]),
    )
    names = ["서울", "부산", "대구", "인천", "광주광역시", "대전", "울산", "세종"]
    r = qa.compute({"type": "trend", "indicator": "출생아 수", "regions": names})
    assert [p["region"] for p in r["split"]] == names
    assert not any("앞의" in n for p in r["split"] for n in p.get("notes") or [])
    chart = charts.build(r)
    assert chart["type"] == "line" and chart["series"] == names
    assert charts.omission_reason(r) is None


def test_summary_region_trends_ends_with_last_year_comparison() -> None:
    r = {
        "status": "ok",
        "split_by": "region",
        "split": [
            _region_trend("서울특별시", [1000, 900]),
            _region_trend("부산광역시", [800, 950]),
        ],
    }
    for p in r["split"]:
        p["change"] = {
            "from_year": 2023,
            "to_year": 2024,
            "diff": p["series"][1]["value"] - p["series"][0]["value"],
            "diff_unit": "명",
        }
    text = qa.summary(r)
    assert text.startswith("서울특별시의 총인구는 2023년 1,000명")
    assert text.endswith("2024년에는 부산광역시가 서울특별시보다 50명 높습니다.")


@pytest.mark.parametrize(
    ("question", "names"),
    [
        ("2024년 서울 출생아 수와 사망자 수", ["출생아 수", "사망자 수"]),
        ("서울 출생아 수, 사망자 수, 혼인 건수 알려줘", ["출생아 수", "사망자 수", "혼인 건수"]),
        ("서울 인구 중 고령인구 비율", []),
        ("2024년 서울 출생아 수", []),
    ],
)
def test_asked_indicators(question, names) -> None:
    assert qa.asked_indicators(question) == names


def test_compute_each_indicator(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(q, sp, kind, regions, *rest):
        value = {"출생아 수": 41605, "사망자 수": 47000}.get(sp["name"])
        if value is None:
            return {"status": "missing_data", "reason": "없음"}
        tbl = "T_" + sp["name"]
        return {
            **VALUE,
            "indicator": sp["name"],
            "unit": "명",
            "sex": "계",
            "value": value,
            "source": sp["name"] + " 표",
            "tbl_id": tbl,
            "url": "https://kosis.kr/" + tbl,
        }

    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(qa, "_compute_lookup", fake)
    q = {
        "type": "value",
        "indicator": "출생아 수",
        "regions": ["서울"],
        "year": 2025,
        "indicators": ["출생아 수", "사망자 수", "혼인 건수"],
    }
    r = qa.compute(q)
    assert r["split_by"] == "indicator" and [p["indicator"] for p in r["split"]] == [
        "출생아 수",
        "사망자 수",
    ]
    text = qa.summary(r)
    assert "출생아 수는 41,605명" in text and "사망자 수는 47,000명" in text
    assert "더 높습니다" not in text
    assert [s["tbl_id"] for s in qa.sources(r)] == ["T_출생아 수", "T_사망자 수"]
    assert "혼인 건수는 이 조건으로 답할 수 없어 뺐습니다" in qa.render(r)


@pytest.mark.parametrize(
    ("question", "dates", "expected"),
    [
        ("2024년 경기도 시군별 인구를 전년 대비 비교해줘", {}, [2024, 2023]),
        ("작년과 재작년 경기도 시군별 인구 비교", {}, [2025, 2024]),
        ("2020년과 2024년 경기도 시군별 인구 비교", {}, [2024, 2020]),
        ("2020~2024년 경기도 시군별 인구 비교", {}, [2024, 2020]),
        ("24년 경기도 시군별 인구 비교", {"year": 2025}, [2024]),
        ("2024년 경기도 시군별 인구 비교", {"start_year": 2020, "end_year": 2024}, [2024]),
    ],
)
def test_region_comparison_queries_years_said_in_the_question(
    monkeypatch, question, dates, expected
):
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (1990, 2025))
    monkeypatch.setattr(
        qa.indicators, "find_regions", lambda text: ["경기도"] if "경기도" in text else []
    )

    def resolve(name, *args, **kwargs):
        if name == "경기도":
            return {"status": "ok", "kind": "시도", "region_code": "41", "region_name": name}
        return {"status": "unknown_region"}

    monkeypatch.setattr(qa.indicators, "resolve_region", resolve)
    queried = []

    def rank(indicator, year, level, sido, *args, within=None):
        queried.append(year)
        assert level == "시군구" and sido == "경기도" and within is None
        return {
            "status": "ok",
            "indicator": indicator,
            "year": year,
            "unit": "명",
            "ranking": [{"region": "지역 A", "value": year}],
            "total_regions": 1,
        }

    monkeypatch.setattr(qa.indicators, "rank", rank)
    parsed = {"type": "compare", "indicator": "총인구", "regions": ["경기도"], **dates}
    result = qa.compute(qa.correct(question, parsed, date(2026, 11, 28)))
    assert result["status"] == "ok"
    assert queried == expected


def test_national_region_list_clears_the_guessed_province(monkeypatch):
    monkeypatch.setattr(qa.indicators, "find_regions", lambda _: [])
    monkeypatch.setattr(
        qa.indicators, "resolve_region", lambda *a, **kw: {"status": "unknown_region"}
    )
    question = "2024년 시도별 고령인구 비율 비교해줘"
    parsed = {
        "type": "compare",
        "indicator": "고령인구 비율",
        "sido": "경기도",
        "regions": ["경기도", "서울"],
    }
    got = qa.correct(question, parsed, date(2026, 11, 28))
    assert got["type"] == "rank" and got["level"] == "시도" and got["regions"] == []
    assert qa.indicators.rank_where(got["level"], got.get("sido"), got.get("within")) == (
        "r.kind = '시도'",
        [],
        "시도",
    )


def test_compute_population_rank_passes_the_asked_region(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    target = {"region": "부산광역시", "rank": 2, "value": 24.5}
    ranking = [
        {"region": "전라남도", "value": 27.2, "rank": 1},
        {"region": "부산광역시", "value": 24.5, "rank": 2},
    ]
    ranked = {"status": "ok", "indicator": "고령인구 비율", "unit": "%", "year": 2024}
    ranked.update(level="시도", order="높은 순", total_regions=2, ranking=ranking, target=target)

    def fake_rank(*args, **kwargs):
        calls.append(args)
        return dict(ranked)

    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "population"})
    monkeypatch.setattr(qa.indicators, "canonical_name", lambda term: term)
    monkeypatch.setattr(qa.indicators, "rank", fake_rank)
    q = {"type": "rank", "indicator": "고령인구 비율", "regions": ["부산"], "target": "부산"}
    r = qa.compute({**q, "year": 2024, "level": "시도"})
    assert calls[0][-1] == "부산"
    assert qa.summary(r) == (
        "2024년 부산광역시의 고령인구 비율은 24.5%로 시도 2곳 중 높은 순으로 2위입니다."
    )


def test_lookup_band_mismatch_lists_nearby_bands(monkeypatch: pytest.MonkeyPatch) -> None:
    labels = (
        ("0", "계"),
        ("060", "60~64세"),
        ("065", "65~69세"),
        ("070", "70~74세"),
        ("020", "20~24세"),
    )
    monkeypatch.setattr(qa.lookup, "_dim_labels", lambda tbl, dim: labels)
    spec = {"name": "사망자 수"}
    src = {"table": "T", "age_dim": "c2", "age_total": "0", "age_min": 0}
    r = qa.lookup._band_mismatch(spec, src, [[62, 67]])
    assert r["status"] == "band_mismatch"
    assert r["available_bands"] == ["60~64세", "65~69세", "70~74세"]
    no_age = qa.lookup._band_mismatch(spec, {"table": "T"}, [[62, 67]])
    assert no_age["status"] == "unknown_indicator"


def test_sigungu_future_year_is_prediction_with_sido_projection(monkeypatch) -> None:
    from app.services import indicators

    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(indicators, "_monthly", lambda *a: False)
    proj = {"status": "ok", "region": "부산광역시", "year": 2040, "value": 2853768}
    monkeypatch.setattr(indicators, "projection", lambda name, code, year: proj)
    monkeypatch.setattr(
        indicators, "_base", lambda name, ind, status, **kw: {"status": status, **kw}
    )
    monkeypatch.setattr(indicators, "_out_of_range_reason", lambda lo, hi, ind=None: "자료 기간 밖")
    reg = {
        "kind": "시군구",
        "region_code": "26350",
        "region_name": "해운대구",
        "sido_code": "26",
        "sido_name": "부산광역시",
    }
    out = indicators._check("총인구", indicators.INDICATORS["총인구"], reg, 2040, 2040)
    assert out["status"] == "prediction_question" and out["suggestion"] is proj
    assert (
        out["reason"].startswith("부산광역시 해운대구의 2040년 값은")
        and "시도 단위까지만" in out["reason"]
    )


def test_lookup_trend_entirely_before_coverage_is_out_of_range(monkeypatch) -> None:
    monkeypatch.setattr(qa, "_lookup_span", lambda sp, region, sex: (1990, 2025))
    sp = {"name": "이혼 건수"}
    q = {"type": "trend", "start_year": 1970, "end_year": 1980}
    r = qa._lookup_trend(q, sp, "서울", None, "계", None)
    assert r["status"] == "out_of_range" and r["available_years"] == [1990, 2025]
    assert r["asked_year"] == 1970 and r["reason"] == "이혼 건수 자료는 1990~2025년만 있습니다."


def test_subject_does_not_repeat_sex_in_name() -> None:
    assert qa._subject({"sex": "남", "indicator": "남편"}) == "남편"
    assert qa._subject({"sex": "여", "indicator": "고령인구 비율"}) == "여성 고령인구 비율"


def test_latest_month_out_of_range_retries_with_annual(monkeypatch) -> None:
    seen = []

    def fake(q):
        seen.append((q.get("asked") or {}).get("latest"))
        if (q.get("asked") or {}).get("latest"):
            return {"status": "out_of_range"}
        return {"status": "ok", "value": 1}

    monkeypatch.setattr(qa, "_compute", fake)
    assert qa.compute({"asked": {"latest": True}})["status"] == "ok" and seen == [True, False]
    seen.clear()
    assert qa.compute({"asked": {"latest": True}, "year": 2030})["status"] == "out_of_range"


def test_mean_of_regions_averages_ranking(monkeypatch) -> None:
    rows = [{"region": "가", "value": 10, "rank": 1}, {"region": "나", "value": 20, "rank": 2},
            {"region": "다", "value": None, "rank": 3}]  # fmt: skip

    def fake(q):
        assert not q["region_mean"] and q["top"] is None
        return {"status": "ok", "kind": "count", "unit": "명", "ranking": rows, "level": "시도"}

    monkeypatch.setattr(qa, "_compute", fake)
    r = qa._mean_of_regions({"region_mean": True, "level": "시도", "top": 3})
    assert r["value"] == 15 and r["region"] == "2개 시도 평균" and "ranking" not in r


def test_unknown_ratio_indicator_is_not_population_composition(monkeypatch) -> None:
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: None)
    monkeypatch.setattr(qa.lookup, "data_gap", lambda term: {})
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    r = qa.compute({"type": "composition", "indicator": "재혼 비율", "regions": [], "year": 2024})
    assert r["status"] == "unknown_indicator"


def test_compare_times_against_nation_is_region_over_nation() -> None:
    assert qa._times({"region": "전국", "value": 21.21}, {"region": "순천시", "value": 20.66}) == (
        ", 0.97배"
    )
    assert qa._times({"region": "A", "value": 0}, {"region": "B", "value": 3}) == ""
