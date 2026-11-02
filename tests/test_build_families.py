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


def test_notation_variants_are_editions_of_one_member() -> None:
    def table(tbl_id: str, name: str, year: int) -> dict:
        return {"tbl_id": tbl_id, "survey_name": "인구총조사", "table_name": name,
                "start_year": year, "end_year": year, "classifications": []}  # fmt: skip

    rows = {
        r["tbl_id"]: r
        for r in bf.build(
            [
                table("OLD", "성, 연령 및 근로장소별 취업 인구(15세 이상)-시도", 2010),
                table("NEW", "성별/연령별/근로장소별 취업인구(15세이상)-시도", 2020),
                table("SGG", "성별/연령별/근로장소별 취업인구(15세이상)-시군구", 2020),
                table("MOM", "모의 연령별 출생", 2020),
                table("DAD", "부의 연령별 출생", 2020),
            ]
        )
    }
    # '및'·'별'·기호만 다르면 같은 표의 판, 최신 판이 먼저
    assert rows["OLD"]["family_id"] == rows["NEW"]["family_id"]
    assert (rows["NEW"]["edition_order"], rows["OLD"]["edition_order"]) == (1, 2)
    # 지역 단위나 다른 낱말이 다르면 합치지 않는다
    assert rows["SGG"]["family_id"] != rows["NEW"]["family_id"]
    assert rows["MOM"]["family_id"] != rows["DAD"]["family_id"]


def _shaped(
    tbl_id: str,
    name: str,
    year: int,
    folder: str = "인구 > 통근통학",
    classes: tuple[str, ...] = ("성별",),
    items: tuple[str, ...] = ("인구",),
) -> dict:
    return {
        "tbl_id": tbl_id,
        "survey_name": "조사",
        "table_name": name,
        "start_year": year,
        "end_year": year,
        "classifications": [{"id": c, "name": c} for c in classes],
        "items": [{"name": i} for i in items],
        "path_texts": [f"{folder} > {name}"],
    }


def test_target_split_without_sex_tag() -> None:
    rows = {
        r["tbl_id"]: r
        for r in bf.build(
            [
                _shaped("H", "차별 받은 경험 - 병원 및 복지기관", 2024),
                _shaped("S", "차별 받은 경험 - 가게 및 식당", 2024),
                # '-' 뒤가 지역 수준이면 대상이 아니다
                _shaped("A", "성별 외국인-시도", 2024),
                _shaped("B", "성별 외국인-시군구", 2024),
                # 분류 구성이 다르면 앞부분이 같아도 다른 패밀리
                _shaped("X", "차별 받은 경험 - 동네 주민", 2024, classes=("연령",)),
            ]
        )
    }
    assert rows["H"]["family_id"] == rows["S"]["family_id"]
    assert (rows["H"]["family_name"], rows["H"]["target"]) == ("차별 받은 경험", "병원 및 복지기관")
    assert rows["A"]["family_id"] != rows["B"]["family_id"]
    assert rows["A"]["target"] is None
    assert rows["X"]["family_id"] != rows["H"]["family_id"]


def test_region_split_members() -> None:
    rows = {
        r["tbl_id"]: r
        for r in bf.build(
            [
                _shaped("SU", "통근통학 인구(서울, 인천, 경기)(12세 이상)-시군구", 2020),
                _shaped("GW", "통근통학 인구(강원)(12세 이상)-시군구", 2020),
                # 같은 멤버의 다른 판은 기간이 달라도 따라온다
                _shaped("GW15", "통근통학 인구(강원)(12세이상)-시군구", 2015),
                # 같은 폴더·구조라도 시도 이름 말고 다른 말이 다르면 묶지 않는다
                _shaped("M", "혼인상태별 인구", 2020, folder="인구 > 기타"),
                _shaped("E", "교육정도별 인구", 2020, folder="인구 > 기타"),
                # 구조가 다르면 시도 이름만 달라도 묶지 않는다
                _shaped("BS", "통근통학 인구(부산)(12세 이상)-시군구", 2020, items=("인구", "%")),
            ]
        )
    }
    family = rows["SU"]["family_id"]
    assert rows["GW"]["family_id"] == rows["GW15"]["family_id"] == family
    assert rows["SU"]["family_name"] == "통근통학 인구(12세 이상)-시군구"
    assert (rows["SU"]["target"], rows["GW"]["target"]) == ("서울·인천·경기", "강원")
    assert rows["M"]["family_id"] != rows["E"]["family_id"]
    assert rows["BS"]["family_id"] != family


def test_strip_region() -> None:
    assert bf.strip_region("통근·통학 인구(대구,경북-시군구)") == "통근·통학 인구(시군구)"
    assert bf.strip_region("현거주지/성/목적지별 통근·통학인구(강원도,12세이상)") == (
        "현거주지/성/목적지별 통근·통학인구(12세이상)"
    )
    assert bf.strip_region("서울특별시 총조사인구 총괄(읍면동/성/연령별)") == (
        "총조사인구 총괄(읍면동/성/연령별)"
    )
    assert bf.region_label("통근통학 인구(서울, 인천, 경기)") == "서울·인천·경기"


def test_same_period_with_other_structure_is_not_an_edition() -> None:
    rows = {
        r["tbl_id"]: r
        for r in bf.build(
            [
                _shaped(
                    "A75",
                    "시도/산업(대분류),직업(대분류)/성별 취업자",
                    1975,
                    classes=("시도별", "산업직업별"),
                ),
                _shaped(
                    "A70",
                    "시도/산업(대분류),직업(대분류)/성별 취업자",
                    1970,
                    classes=("시도별", "산업직업별"),
                ),
                _shaped(
                    "B75",
                    "시도/산업(대분류)/직업(대분류)/성별 취업자",
                    1975,
                    classes=("시도별", "성별", "직업별"),
                ),
            ]
        )
    }
    # 기호만 다른 이름이라도 같은 해에 구조가 다른 표가 둘이면 판이 아니다
    assert rows["A75"]["family_id"] == rows["A70"]["family_id"]
    assert rows["B75"]["family_id"] != rows["A75"]["family_id"]
    assert (rows["A75"]["edition_order"], rows["A70"]["edition_order"]) == (1, 2)
