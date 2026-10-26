from scripts import check_raw as cr


def test_age_names() -> None:
    assert cr._age("계") is None
    assert cr._age("0세") == 0
    assert cr._age("100세 이상") == 100
