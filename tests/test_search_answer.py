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
