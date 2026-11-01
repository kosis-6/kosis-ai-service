from datetime import date

from app.services import agegroup, timeref
from app.services import slots as sl

TODAY = date(2026, 11, 1)


def test_relative_words() -> None:
    assert timeref.resolve("작년 출생아 수", TODAY).year == 2025
    assert timeref.resolve("재작년 서울 인구는?", TODAY).year == 2024
    assert timeref.resolve("올해 혼인 건수", TODAY).year == 2026
    assert timeref.resolve("내년 인구", TODAY).year == 2027


def test_n_years_ago_and_open_end() -> None:
    r = timeref.resolve("2년 전에 결혼한 사람", TODAY)
    assert (r.year, r.open_end) == (2024, False)
    r = timeref.resolve("3년 전부터 인구 변화", TODAY)
    assert (r.year, r.open_end) == (2023, True)
    assert timeref.resolve("작년부터 늘었나", TODAY).open_end is True


def test_compare_base_and_non_years_are_not_time() -> None:
    assert timeref.resolve("작년보다 인구가 늘었어?", TODAY) is None
    assert timeref.resolve("5년 전이랑 비교해서 혼자 사는 집이 얼마나 늘었나", TODAY) is None
    assert timeref.resolve("올해 외국인 유학생이 작년보다 늘었는지", TODAY).year == 2026  # 비교 기준은 빼고 시점만
    assert timeref.resolve("전년 대비 출생아", TODAY) is None
    assert timeref.resolve("10년 전 대비 인구", TODAY) is None
    assert timeref.resolve("몇 년 전 인구", TODAY) is None
    assert timeref.resolve("20년간 인구 변화", TODAY) is None


def test_slots_relative_year_and_projection() -> None:
    s = sl.correct("작년 출생아 수", sl.empty_slots("작년 출생아 수"))
    assert s["conditions"]["year"] == date.today().year - 1
    assert s["time_ref"]["expr"] == "작년" and s["projection"] is False
    s = sl.correct("내년 인구", sl.empty_slots("내년 인구"))
    assert s["projection"] is True
    # 절대 연도가 있으면 상대시점은 보지 않는다
    s = sl.correct("2020년 작년 대비", sl.empty_slots("2020년 작년 대비"))
    assert (s["conditions"]["year"], s["time_ref"]) == (2020, None)


def test_age_groups() -> None:
    assert agegroup.resolve("노인 인구가 많은 동네").label() == "65세 이상"
    assert agegroup.resolve("청년들이 많이 떠나는 지역").label() == "19~34세"
    assert agegroup.resolve("일하는 나이대 100명이 노인 몇 명").lo == 15  # 앞에 나온 집단
    # 숫자 구간이 있으면 이름은 보지 않는다. 시설 이름은 집단이 아니다
    assert agegroup.resolve("65세 이상 노인 수") is None
    assert agegroup.resolve("어린이집 다니는 애들") is None
    assert agegroup.resolve("서울 인구") is None
    s = sl.correct("노인 인구", sl.empty_slots("노인 인구"))
    assert s["age"]["lo"] == 65 and s["age"]["hi"] is None


def test_age_coverage_and_long_years() -> None:
    from app.services import search as se

    # 연령 축이 있는 추계 표는 전 연령 (이름의 '고령인구'에 속지 않는다)
    assert se.age_coverage(se.norm("주요 연령계층별 추계인구(생산연령인구, 고령인구 등)/시도"), ["인구종류별"], "") == (0, None)
    assert se.age_coverage(se.norm("생활비원천별 고령자(65세 이상)-시군구"), [], "") == (65, None)
    assert se.age_coverage(se.norm("세대구성별 고령자 - 시군구"), [], "") == (65, None)
    # '10년 넘게'는 연도가 아니다
    s = sl.correct("한 집에서 10년 넘게 산 가구", sl.empty_slots("한 집에서 10년 넘게 산 가구"))
    assert s["conditions"]["year"] is None
