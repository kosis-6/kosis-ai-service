import pytest

from scripts import eval_golden as ev


def case(**kw) -> dict:
    return {"id": "t", "question": "질문", **kw}


def test_parse_compares_only_filled_fields() -> None:
    parsed = {"type": "value", "regions": ["서울특별시"], "year": 2025}
    ok, wrong = ev.grade_parse(case(type="value", regions="서울", year="2025", sex="계"), parsed)
    assert ok and not wrong  # 지역 접미사 차이와 비어 있는 sex(= 계)는 같은 것으로 본다
    ok, wrong = ev.grade_parse(case(regions="서울;부산"), {"regions": ["서울"]})
    assert not ok and wrong[0].startswith("regions=")
    assert ev.grade_parse(case(), parsed) == (None, [])


@pytest.mark.parametrize(
    ("expected", "got", "ok"),
    [
        ("20.4", 20.43, True),  # 적은 자릿수 반올림 오차 허용 (±0.05)
        ("20.43", 20.44, False),
        ("2353032", 2353032, True),
        ("2,353,032", 2353031, False),
    ],
)
def test_value_tolerance_follows_written_digits(expected: str, got: float, ok: bool) -> None:
    assert ev.grade_value(case(target="value", expected=expected), {"value": got})[0] is ok


def test_ranking_compares_expected_length() -> None:
    result = {"ranking": [{"region": "세종특별자치시"}, {"region": "경기도"}]}
    ok, got = ev.grade_value(case(target="ranking", expected="세종"), result)
    assert ok and got == ["세종특별자치시"]
    assert ev.grade_value(case(target="ranking", expected="경기"), result)[0] is False


def test_share_and_missing_value() -> None:
    result = {"parts": [{"label": "남자", "share": 49.76}, {"label": "여자", "share": 50.24}]}
    assert ev.grade_value(case(target="share:여자", expected="50.24"), result)[0] is True
    # 답해야 하는데 값이 없으면 오답, 정답 칸이 비면 채점하지 않음
    refused = {"status": "missing_region"}
    assert ev.grade_value(case(target="value", expected="1"), refused)[0] is False
    assert ev.grade_value(case(), {})[0] is None


def test_refusal_and_reason() -> None:
    assert ev.grade_refusal(case(expected_status="unknown_indicator"), "unknown_indicator") == (
        True,
        True,
    )
    # 거절은 맞지만 사유 코드가 다름
    assert ev.grade_refusal(case(expected_status="unknown_indicator"), "out_of_range") == (
        True,
        False,
    )
    assert ev.grade_refusal(case(), "missing_region") == (False, False)


def test_sheet_actions() -> None:
    sheet = case(expected_status="refuse:대안안내", allowed_action="대안안내")
    assert ev.grade_refusal(sheet, "out_of_range", {"status": "out_of_range"}) == (True, True)
    assert ev.grade_refusal(sheet, "unknown_indicator", {"status": "unknown_indicator"}) == (
        True,
        False,
    )
    # 수록 기간 밖이지만 추계값으로 대신 답했으면 '답변'
    projected = {"status": "out_of_range", "result": {"suggestion": {"status": "ok"}}}
    assert ev.action_of(projected) == "답변"
    both = case(expected_status="ok", allowed_action="명확화, 답변")
    clarified = {"status": "ambiguous_region"}
    assert ev.grade_refusal(both, "ambiguous_region", clarified) == (True, True)


def test_table_choice() -> None:
    c = case(allowed_tables="DT_1BB0006 + DT_1B81A23")
    ok = {"status": "ok", "sources": [{"tbl_id": "DT_1BB0006"}]}
    assert ev.grade_tables(c, ok) is True
    assert ev.grade_tables(c, {"status": "ok", "sources": [{"tbl_id": "DT_1B04006"}]}) is False
    assert ev.grade_tables(c, {"status": "unknown_indicator"}) is None
    assert ev.grade_tables(case(allowed_tables="-"), ok) is None
