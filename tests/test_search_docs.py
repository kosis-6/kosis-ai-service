import pytest

from pipeline import search_docs as sd

TABLE = {
    "survey_name": "지방자치단체외국인주민현황",
    "table_name": "한국국적을 취득한 자-혼인 귀화자(계)",
    "classifications": [
        {"name": "행정구역(시군구)별", "sample_values": ["계", "서울특별시", "부산광역시"]},
        {"name": "국적별", "sample_values": ["중국", "기타"]},
    ],
    "items": [{"name": "계"}, {"name": "남자"}, {"name": "혼인귀화자"}],
    "path_texts": [
        "주제별통계 > 인구 > 지방자치단체외국인주민현황 > 2015년 이후(11월1일 기준) > "
        "한국국적을 취득한 자 > 한국국적을 취득한 자-혼인 귀화자(계)"
    ],
}


def test_presets_add_fields_in_order() -> None:
    assert (
        sd.build(TABLE, "name")
        == "지방자치단체외국인주민현황 | 한국국적을 취득한 자-혼인 귀화자(계)"
    )
    assert sd.build(TABLE, "classes").endswith("| 행정구역(시군구)별 국적별")
    full = sd.build(TABLE, "full")
    # 범용 단어는 빼고, 경로는 공통 앞부분과 표명(마지막 단계)을 뺀다
    assert "서울특별시 부산광역시 중국 | 혼인귀화자 |" in full
    assert full.endswith(
        "지방자치단체외국인주민현황 > 2015년 이후(11월1일 기준) > 한국국적을 취득한 자"
    )
    assert " 계 " not in full and "남자" not in full


def test_unknown_preset() -> None:
    with pytest.raises(ValueError):
        sd.build(TABLE, "all")
