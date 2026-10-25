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
