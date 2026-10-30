from typing import Any

from pipeline import load_catalog as lc


def _cls(name: str, values: list[str] | None = None, levels: list[str] | None = None) -> dict:
    c: dict[str, Any] = {"name": name, "sample_values": values or []}
    if levels:
        c["level_names"] = levels
    return c


def _items(*names: str) -> list[dict[str, str]]:
    return [{"name": n} for n in names]


def test_clean_text() -> None:
    assert lc.clean_text("  시군구별 연령별  외국인(계) ") == "시군구별 연령별 외국인(계)"
    assert lc.clean_text("'세대구성별") == "세대구성별"
    assert lc.clean_text("   ") is None


def test_years_monthly_and_multi_period() -> None:
    periods = [
        {"frequency_label": "년", "start": "1992", "end": "2025"},
        {"frequency_label": "월", "start": "201101", "end": "202609"},
    ]
    assert lc.years(periods) == (1992, 2026)


def test_region_levels_split_current_and_previous() -> None:
    classes = [
        _cls("현거주지별", levels=["시도", "시군구"]),
        _cls("5년전 거주지별", levels=["시도"]),
        _cls("연령별"),
    ]
    assert lc.region_levels(classes) == (["시도", "시군구"], ["시도"])


def test_sex_from_class() -> None:
    assert lc.sex_info("표", [_cls("성별", ["계", "남자", "여자"])], []) == ("class", "both", True)
    # 생명표처럼 남/여만 있고 합계가 없는 표
    assert lc.sex_info("표", [_cls("성별", ["남자", "여자"])], []) == ("class", "both", False)
    # 가구주 성별은 성별 축이 아니다
    assert lc.sex_info("표", [_cls("가구주의 성별", ["계", "남자", "여자"])], []) == (
        "none",
        "none",
        None,
    )


def test_sex_from_items() -> None:
    assert lc.sex_info("표", [], _items("총인구", "총인구_남자", "총인구_여자")) == (
        "item",
        "both",
        True,
    )
    # 첫 항목이 성별 항목이 아니면 합계로 본다
    assert lc.sex_info("표", [], _items("외국인", "남자", "여자"))[2] is True
    assert lc.sex_info("표", [], _items("남자", "여자")) == ("item", "both", False)
    # 한 성별만 다루는 표는 합계를 따지지 않는다
    assert lc.sex_info("표", [], _items("15세 이상 여자", "기혼여성")) == (
        "item",
        "female_only",
        None,
    )
    # '남편'·'여성가구주'처럼 단어 일부인 경우는 성별 항목이 아니다
    assert lc.sex_info("표", [], _items("여성가구주", "남편 연령"))[0] == "none"


def test_sex_from_member_table_name() -> None:
    assert lc.sex_info("한국국적을 취득한 자-혼인 귀화자(계)", [], []) == ("member", "none", True)
    assert lc.sex_info("한국국적을 취득한 자(여)(2006~2015)", [], []) == (
        "member",
        "female_only",
        None,
    )


def test_clean_record_and_check() -> None:
    raw = {
        "org_id": "110",
        "tbl_id": "TX_X",
        "table_name": "표  이름",
        "survey_name": "조사",
        "organization": "기관",
        "period": "년 (2007 ~ 2015)",
        "catalog_periods": [{"frequency_label": "년", "start": "2007", "end": "2015"}],
        "paths": [{"path": "주제별통계 > 인구 > 표 이름"}],
        "classifications": [_cls("행정구역별", ["서울특별시"], ["시도", "시군구"])],
        "items": [{"name": "계", "unit": "명"}],
        "comments": [],
        "comment_text": "",
    }
    rec = lc.clean_record(raw)
    assert rec["table_name"] == "표 이름"
    assert (rec["start_year"], rec["end_year"], rec["is_single_period"]) == (2007, 2015, False)
    assert rec["region_levels"] == ["시도", "시군구"]
    assert rec["has_comment"] is False
    assert rec["url"] == "https://kosis.kr/statHtml/statHtml.do?orgId=110&tblId=TX_X"
    errors = lc.check([rec, rec])
    assert any("중복" in e for e in errors)
    assert any("표 수" in e for e in errors)
