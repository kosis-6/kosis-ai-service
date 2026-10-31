from pipeline import build_families as bf


def _t(tbl_id: str, name: str, start: int, end: int, survey: str = "조사") -> dict:
    return {
        "tbl_id": tbl_id,
        "survey_name": survey,
        "table_name": name,
        "start_year": start,
        "end_year": end,
    }


def test_edition_name_strips_year_and_revision() -> None:
    assert bf.edition_name("한국국적을 취득한 자(계)(2006~2015)") == "한국국적을 취득한 자(계)"
    assert bf.edition_name("행정구역(읍면동)별/5세별 주민등록인구(2011년~)") == (
        "행정구역(읍면동)별/5세별 주민등록인구"
    )
    assert bf.edition_name("시도/직업별(~2007) 혼인") == "시도/직업별 혼인"
    assert bf.edition_name("성/연령(5세)/직업별(신) 사망자수(1993~2017)") == (
        "성/연령(5세)/직업별 사망자수"
    )
    # 연령 표기처럼 네 자리 연도가 아닌 괄호는 남긴다
    assert bf.edition_name("시군구/성/연령(5세)별 사망률") == "시군구/성/연령(5세)별 사망률"


def test_member_of() -> None:
    assert bf.member_of("한국국적을 취득한 자-혼인 귀화자(남)") == (
        "한국국적을 취득한 자",
        "혼인 귀화자",
        "남",
    )
    assert bf.member_of("한국국적을 취득한 자(계)") == ("한국국적을 취득한 자", "전체", "계")
    # 성별 표기가 없으면 '-시군구'도 표명의 일부
    assert bf.member_of("혼인상태별 인구-시군구") == ("혼인상태별 인구-시군구", None, None)


def test_build_groups_members_and_orders_editions() -> None:
    rows = bf.build(
        [
            _t("A017", "한국국적을 취득한 자(계)(2006~2015)", 2006, 2015),
            _t("A020", "한국국적을 취득한 자-혼인 귀화자(계)(2007~2015)", 2007, 2015),
            _t("A020_A", "한국국적을 취득한 자-혼인 귀화자(계)", 2015, 2015),
            _t("X", "한국국적을 취득한 자(계)", 2016, 2020, survey="다른 조사"),
        ]
    )
    by_id = {r["tbl_id"]: r for r in rows}
    family = {by_id[t]["family_id"] for t in ("A017", "A020", "A020_A")}
    assert len(family) == 1
    # 조사가 다르면 다른 패밀리
    assert by_id["X"]["family_id"] not in family
    # 같은 연도를 담은 판이 둘이면 기간이 긴 판이 1번
    assert (by_id["A020"]["edition_order"], by_id["A020"]["edition"]) == (1, "2007~2015")
    assert (by_id["A020_A"]["edition_order"], by_id["A020_A"]["edition"]) == (2, "2015")
    assert by_id["A017"]["target"] == "전체"


def _tc(tbl_id: str, name: str, start: int, end: int, *class_ids: str) -> dict:
    return {
        **_t(tbl_id, name, start, end),
        "classifications": [{"id": c, "name": c} for c in class_ids],
    }


def test_renamed_successor_joins_old_member() -> None:
    tables = [
        _tc("A017", "한국국적을 취득한 자(계)(2006~2015)", 2006, 2015, "R", "N"),
        _tc("A017_A", "이전국적별 한국국적을 취득한 자(계)", 2015, 2024, "R", "N"),
        _tc("A020", "한국국적을 취득한 자-혼인 귀화자(계)", 2007, 2015, "R"),
        _tc("A008", "외국인-국제결혼 이주자(계)", 2007, 2015, "R"),
        _tc("A008_A", "외국인-결혼이민자(계)", 2015, 2024, "R"),
        # ID는 맞지만 분류 구성이 다르면 붙이지 않는다
        _tc("A029", "국제결혼 가정 자녀(2007~2015)", 2007, 2015, "R"),
        _tc("A029_A", "가정 자녀(연령별)", 2015, 2015, "R", "AGE"),
    ]
    by_id = {r["tbl_id"]: r for r in bf.build(tables)}
    new, old = by_id["A017_A"], by_id["A017"]
    assert new["family_id"] == old["family_id"] == by_id["A020"]["family_id"]
    assert (new["target"], new["edition_order"], new["renamed_from"]) == ("전체", 1, "A017")
    assert (old["edition_order"], old["renamed_from"]) == (2, None)
    # 대상 이름은 최신 표 기준
    assert by_id["A008"]["target"] == by_id["A008_A"]["target"] == "결혼이민자"
    assert by_id["A029"]["family_id"] != by_id["A029_A"]["family_id"]
    assert bf.successors(tables)[1] == [("A029", "A029_A")]


def test_spacing_and_symbol_variants_are_editions() -> None:
    rows = bf.build(
        [
            _t("OLD", "성별 외국인 - 시군구", 2000, 2010),
            _t("NEW", "성별 외국인-시군구", 2015, 2024),
            _t("C1", "성별 통근ㆍ통학인구(12세이상)-시군구", 2000, 2010),
            _t("C2", "성별 통근 통학 인구(12세 이상)-시군구", 2015, 2024),
            _t("S", "성별 외국인-시도", 2015, 2024),
        ]
    )
    by_id = {r["tbl_id"]: r for r in rows}
    assert by_id["OLD"]["family_id"] == by_id["NEW"]["family_id"]
    assert by_id["C1"]["family_id"] == by_id["C2"]["family_id"]
    # 보여 줄 이름은 최신 판 표기, 판 순서는 최신 판이 1번
    assert by_id["OLD"]["family_name"] == "성별 외국인-시군구"
    assert (by_id["NEW"]["edition_order"], by_id["OLD"]["edition_order"]) == (1, 2)
    # 지역 수준이 다르면 다른 패밀리
    assert by_id["S"]["family_id"] != by_id["NEW"]["family_id"]
