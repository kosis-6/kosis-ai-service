import pytest

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
        "2023년 대비 190명 감소했습니다(증감률 -19.0%). 이 기간 동안 매년 감소했습니다."
    )


def test_summary_value_with_sex() -> None:
    assert qa.summary(VALUE) == "2025년 부산광역시의 여성 고령인구 비율은 27.66%입니다."


def test_summary_aging_index_meaning() -> None:
    r = {**VALUE, "indicator": "노령화지수", "unit": "유소년인구 100명당 명", "sex": "계"}
    r["value"] = 210.5
    assert qa.summary(r) == (
        "2025년 부산광역시의 노령화지수는 210.5입니다. "
        "유소년인구(0~14세) 100명당 65세 이상 인구가 210.5명이라는 뜻입니다."
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
        "(부산광역시 25.01%, 서울특별시 20.43%)."
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
    # 구성비는 지표 확인 없이 총인구를 나눠 본다. 지역이 없으면 되묻는다
    assert qa.compute({"type": "composition", "regions": []})["status"] == "missing_region"
    qa.compute({"type": "composition", "indicator": "남녀 비중", "regions": ["서울"], "by": "sex"})
    assert calls == [("서울", 2025, "sex", "계")]


def test_summary_causal_prefix() -> None:
    r = {**TREND, "causal_question": "원인은 판단하지 않습니다."}
    assert qa.summary(r).startswith("원인은 판단하지 않습니다. 대구광역시의 총인구는")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."},
            "어느 지역인지 질문에 없습니다. 지역을 넣어 다시 질문해 주세요.",
        ),
        (
            {"status": "unknown_indicator", "indicator": "혼인율"},
            "'혼인율'은(는) 아직 지원하지 않는 지표입니다. 아래 지원 지표로 다시 질문해 주세요.",
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
    """수록 기간 밖 연도: status는 답변 불가로 두고 추계값을 대신 보여준다. 추계 안내는 한 번만"""
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
    # 멘토링 피드백: 거절이 데이터상 불가능(수집 안 함)인지 미구현인지 구분
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
