import pytest

from scripts import enrich_catalog_meta as ecm
from scripts.collect_kosis import KosisError


def _row(obj_id: str, obj_nm: str, itm_id: str, itm_nm: str, **extra: str) -> dict[str, str]:
    return {"ORG_ID": "101", "TBL_ID": "DT_X", "OBJ_ID": obj_id, "OBJ_NM": obj_nm,
            "ITM_ID": itm_id, "ITM_NM": itm_nm, **extra}


ROWS = [
    _row("ITEM", "항목", "T00", "총인구(명)"),
    _row("ITEM", "항목", "T03", "총인구_성비"),
    _row("ITEM", "항목", "T20", "중위연령", UNIT_NM="세"),
    _row("A", "행정구역별(읍면동)", "00", "전국", OBJ_ID_SN="1"),
    _row("A", "행정구역별(읍면동)", "03", "동부", OBJ_ID_SN="1"),
    _row("A", "행정구역별(읍면동)", "11", "서울특별시", OBJ_ID_SN="1"),
    _row("A", "행정구역별(읍면동)", "11010", "종로구", OBJ_ID_SN="1", UP_ITM_ID="11"),
    _row("A", "행정구역별(읍면동)", "11010530", "사직동", OBJ_ID_SN="1", UP_ITM_ID="11010"),
    _row("B", "연령별", "000", "합계", OBJ_ID_SN="2"),
    _row("B", "연령별", "005", "0~4세", OBJ_ID_SN="2"),
]


def test_split_unit() -> None:
    assert ecm.split_unit("총인구(명)", None) == ("총인구", "명")
    assert ecm.split_unit("총인구_성비", None) == ("총인구_성비", None)
    assert ecm.split_unit("중위연령", "세") == ("중위연령", "세")
    assert ecm.split_unit("인구밀도(명/㎢)", None) == ("인구밀도", "명/㎢")
    # 이름의 일부인 괄호는 단위로 보지 않는다
    assert ecm.split_unit("중국(한국계)", None) == ("중국(한국계)", None)


def test_summarize() -> None:
    result = ecm.summarize(ROWS)
    assert [i["name"] for i in result["items"]] == ["총인구", "총인구_성비", "중위연령"]
    assert [i["unit"] for i in result["items"]] == ["명", None, "세"]
    region, age = result["classifications"]
    assert region["levels"] == 3
    assert region["level_names"] == ["시도", "시군구", "읍면동"]
    assert region["sample_values"] == ["서울특별시"]  # 전국·동부 같은 합계 값은 제외
    assert region["value_count"] == 5
    assert age["name"] == "연령별" and age["level_names"] is None
    assert result["meta_summary"] == (
        "분류: 행정구역별(읍면동)[시도·시군구·읍면동] / 연령별"
        " | 항목: 총인구, 총인구_성비, 중위연령"
    )


def test_region_level_names_use_value_names() -> None:
    # 시도 아래가 동부·읍부·면부뿐이면 시군구가 아니다
    rows = [
        _row("A", "행정구역별", "00", "전국", OBJ_ID_SN="1"),
        _row("A", "행정구역별", "21", "부산광역시", OBJ_ID_SN="1"),
        _row("A", "행정구역별", "21003", "동부", OBJ_ID_SN="1", UP_ITM_ID="21"),
        _row("A", "행정구역별", "21004", "읍부", OBJ_ID_SN="1", UP_ITM_ID="21"),
    ]
    assert ecm.summarize_axis("A", rows)["level_names"] == ["시도"]
    # 지역 분류가 아니면 '구'로 끝나는 값이 있어도 단계 이름을 붙이지 않는다
    rows = [_row("B", "가구원수별", "1", "1인가구", OBJ_ID_SN="2"),
            _row("B", "가구원수별", "2", "2인가구", OBJ_ID_SN="2")]
    assert ecm.summarize_axis("B", rows)["level_names"] is None


def test_validate_rejects_bad_response() -> None:
    ecm.validate(ROWS, "101", "DT_X")
    with pytest.raises(KosisError):
        ecm.validate([], "101", "DT_X")
    with pytest.raises(KosisError):
        ecm.validate(ROWS, "101", "DT_OTHER")
    with pytest.raises(KosisError):
        ecm.validate([r for r in ROWS if r["OBJ_ID"] != "ITEM"], "101", "DT_X")


def test_clean_comments_drops_blank_and_separator_lines() -> None:
    rows = [
        {"CMMT_NM": "통계표", "CMMT_DC": "주1) 18세이상 내국인 대상"},
        {"CMMT_NM": "통계표", "CMMT_DC": ""},
        {"CMMT_NM": "통계표", "CMMT_DC": "-----------------------"},
        {"CMMT_NM": "통계표", "CMMT_DC": " ㅇ 용어 정비 : 노후기간 → 건축 경과연수"},
        {"CMMT_NM": "총인구", "CMMT_DC": "외국인 포함"},
    ]
    result = ecm.clean_comments(rows)
    assert result["comments"] == [
        {"target": "통계표", "lines": ["주1) 18세이상 내국인 대상",
                                      "ㅇ 용어 정비 : 노후기간 → 건축 경과연수"]},
        {"target": "총인구", "lines": ["외국인 포함"]},
    ]
    assert result["comment_text"] == (
        "주1) 18세이상 내국인 대상\nㅇ 용어 정비 : 노후기간 → 건축 경과연수\n[총인구] 외국인 포함"
    )
    assert ecm.clean_comments([]) == {"comments": [], "comment_text": ""}
