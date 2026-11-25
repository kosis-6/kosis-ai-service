"""달 범위 읽기 (monthref): 묶음으로 말한 달만 읽고, 달 하나 · 시점 표현은 읽지 않는다.
문장은 평가셋과 다르게"""

from app.services import monthref


def test_ranges_halves_and_quarters() -> None:
    assert monthref.find("올해 1월부터 3월까지 결혼한 사람") == {
        "m0": 1,
        "m1": 3,
        "expr": "1월부터 3월까지",
    }
    assert (
        monthref.find("4~6월 이혼 건수")["m0"] == 4 and monthref.find("4~6월 이혼 건수")["m1"] == 6
    )
    assert monthref.find("7월에서 9월 사이 태어난 아기") == {
        "m0": 7,
        "m1": 9,
        "expr": "7월에서 9월 사이",
    }
    assert (
        monthref.find("작년 하반기 사망자")["m0"],
        monthref.find("작년 하반기 사망자")["m1"],
    ) == (7, 12)
    assert (monthref.find("3분기 혼인")["m0"], monthref.find("3분기 혼인")["m1"]) == (7, 9)


def test_single_month_and_as_of_are_not_ranges() -> None:
    assert monthref.find("10월 부산 인구") is None
    assert monthref.find("2025년 10월 말 기준 인구") is None
    assert monthref.find("5월부터 2월까지") is None  # 뒤집힌 범위
    assert monthref.label(1, 3) == "1~3월" and monthref.label(5, 5) == "5월"
