from app.services import search_answer as sa
from app.services import slots as sl


def _slots(**cond) -> dict:
    s = sl.empty_slots("q")
    s["conditions"].update(cond)
    return s


def test_other_versions_compact() -> None:
    family = {
        "current": {"target": "혼인 귀화자", "sex": "여"},
        "members": [
            {"target": "혼인 귀화자", "sex": "계"},
            {"target": "혼인 귀화자", "sex": "남"},
            {"target": "전체", "sex": "계"},
            {"target": "전체", "sex": "남"},
            {"target": "기타", "sex": "계"},
        ],
        "editions": [{"tbl_id": "X", "period": "2015"}],
    }
    assert sa.other_versions(family) == "계/남 · 전체, 기타 · 2015년판"
    assert (
        sa.other_versions({"current": {"target": None, "sex": None}, "members": [], "editions": []})
        == ""
    )


def test_notice_uses_labels_and_skips_explained_year() -> None:
    result = {
        "slots": _slots(year=2024, region_level="읍면동"),
        "notes": ["2024년을 담은 판이 없어 가장 가까운 2007~2015년 판을 골랐습니다"],
        "failed": ["year", "region_level"],
    }
    assert sa.notice(result) == (
        "2024년을 담은 판이 없어 가장 가까운 2007~2015년 판을 골랐습니다; "
        "읍면동 조건을 충족하지 않습니다"
    )
    assert sa.notice({"slots": _slots(), "notes": [], "failed": []}) is None


def test_status_messages_without_table() -> None:
    for status in ("out_of_scope", "not_statistics"):
        result = {"status": status, "slots": _slots(), "reason": None}
        response = sa.respond(result, [], "S_test")
        assert (
            response["recommendations"] == [] and response["answer"] == sa.STATUS_MESSAGES[status]
        )
    s = _slots()
    s["parse_error"] = True
    response = sa.respond({"status": "out_of_scope", "slots": s, "reason": None}, [], "S_test")
    assert response["status"] == "parse_error"


def test_together_tables_are_listed_in_the_answer() -> None:
    wife = {
        "tbl_id": "W", "org_id": "101", "table_name": "외국인 아내의 국적별 이혼",
        "survey_name": "인구동향조사", "organization": "국가데이터처", "edition": "2000~2024",
        "url": "https://kosis.kr/W",
    }  # fmt: skip
    husband = {**wife, "tbl_id": "H", "url": "https://kosis.kr/H"}
    husband["table_name"] = "외국인 남편의 국적별 이혼"
    by_id = {"W": wife, "H": husband}
    cards = sa.together_cards({"together": ["H", "없는표"]}, by_id)
    assert [c["tbl_id"] for c in cards] == ["H"]
    assert cards[0]["tbl_nm"] == "외국인 남편의 국적별 이혼"
    assert sa.together_cards({}, by_id) == []
    rec = {
        "survey_nm": "인구동향조사", "tbl_nm": wife["table_name"], "org_nm": "국가데이터처",
        "period": "2000~2024", "checks": {}, "notes": [], "url": wife["url"],
        "family": {"current": {"target": None, "sex": None}, "members": [], "editions": []},
    }  # fmt: skip
    result = {"status": "ok", "slots": {}}
    text = sa.answer_text(result, rec, cards)
    assert "이 통계는 표가 나뉘어 있어 아래 표도 함께 보셔야 합니다." in text
    line = (
        "함께 볼 통계표: 외국인 남편의 국적별 이혼 (국가데이터처, 2000~2024년) https://kosis.kr/H"
    )
    assert line in text
    assert "함께 볼 통계표" not in sa.answer_text(result, rec)


def _option(tbl_id: str, name: str, **more) -> dict:
    return {
        "tbl_id": tbl_id, "table_name": name, "survey_name": "조사", "edition": "2015~2024",
        "family_id": f"F_{tbl_id}", "family_name": name, "url": f"https://kosis.kr/{tbl_id}",
        "organization": "국가데이터처", "region_levels": ["시도"], "classifications": [],
        "comments": [], **more,
    }  # fmt: skip


def test_ambiguous_options_show_how_they_differ() -> None:
    a = _option(
        "A", "시도별 주민 현황", organization="행정안전부",
        classifications=[{"name": "행정구역별"}, {"name": "성별"}],
        comments=[{"lines": ["매년 11월1일 기준"]}],
    )  # fmt: skip
    b = _option(
        "B", "성별 주민 - 시군구", region_levels=["시도", "시군구"],
        classifications=[{"name": "행정구역별(시군구)"}],
        comments=[{"lines": ["주1) 국내거주 주민을 대상으로 집계", "단, 집단가구 제외"]}],
    )  # fmt: skip
    by_id = {"A": a, "B": b}
    diffs = sa.option_diffs([a, b], by_id)
    assert diffs["A"] == "행정안전부 · 시도 · 분류: 성별 · 매년 11월1일 기준"
    assert diffs["B"].startswith("국가데이터처 · 시도, 시군구 · ")
    assert "집단가구 제외" in diffs["B"] or "대상으로 집계" in diffs["B"]

    result = {"status": "ambiguous", "slots": _slots(), "options": [a, b], "reason": None}
    response = sa.respond(result, [a, b], "S_test")
    lines = response["answer"].split("\n")
    assert lines[1] == "① 시도별 주민 현황 (조사, 2015~2024년)"
    assert lines[2] == "   행정안전부 · 시도 · 분류: 성별 · 매년 11월1일 기준"
    assert [f["diff"] for f in response["clarify"]["families"]] == [diffs["A"], diffs["B"]]


def test_ambiguous_options_without_difference_add_no_line() -> None:
    a = _option("A", "서울특별시 시군구별 이동자수")
    b = _option("B", "부산광역시 시군구별 이동자수")
    assert sa.option_diffs([a, b], {"A": a, "B": b}) == {}
    result = {"status": "ambiguous", "slots": _slots(), "options": [a, b], "reason": None}
    response = sa.respond(result, [a, b], "S_test")
    assert len(response["answer"].split("\n")) == 4
    assert [f["diff"] for f in response["clarify"]["families"]] == [None, None]
    assert sa.option_diffs([a, b], {"A": a}) == {}
