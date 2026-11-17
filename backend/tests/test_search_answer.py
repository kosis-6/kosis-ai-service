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
    # JSON을 못 읽고 찾지도 못했으면 parse_error
    s = _slots()
    s["parse_error"] = True
    response = sa.respond({"status": "out_of_scope", "slots": s, "reason": None}, [], "S_test")
    assert response["status"] == "parse_error"


def test_together_tables_are_listed_in_the_answer() -> None:
    """한 통계가 여러 표로 나뉘면 함께 볼 표를 답과 응답 칸에 넣는다"""
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
    line = "함께 볼 통계표: 외국인 남편의 국적별 이혼 (국가데이터처, 2000~2024년) https://kosis.kr/H"
    assert line in text
    assert "함께 볼 통계표" not in sa.answer_text(result, rec)

