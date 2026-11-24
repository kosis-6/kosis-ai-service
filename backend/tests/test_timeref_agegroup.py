from datetime import date

import pytest

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


def test_jeon_starting_word_is_not_years_ago() -> None:
    for q in ["최근 5년 전국 합계출산율", "최근 3년 전라남도 인구", "지난 10년 전북 출생아 수"]:
        assert timeref.recent_period(q, TODAY) is not None, q
        assert timeref.resolve(q, TODAY) is None, q
    assert timeref.resolve("5년 전체 인구", TODAY) is None
    assert timeref.resolve("5년 전 전국 인구", TODAY).year == 2021
    assert timeref.resolve("3년 전에 전남 인구", TODAY).year == 2023
    assert timeref.recent_period("최근 5년 전보다", TODAY) is None


def test_compare_base_and_non_years_are_not_time() -> None:
    assert timeref.resolve("작년보다 인구가 늘었어?", TODAY) is None
    assert timeref.resolve("5년 전이랑 비교해서 혼자 사는 집이 얼마나 늘었나", TODAY) is None
    assert timeref.resolve("올해 외국인 유학생이 작년보다 늘었는지", TODAY).year == 2026
    assert timeref.resolve("전년 대비 출생아", TODAY) is None
    assert timeref.resolve("10년 전 대비 인구", TODAY) is None
    assert timeref.resolve("몇 년 전 인구", TODAY) is None
    assert timeref.resolve("20년간 인구 변화", TODAY) is None


def test_recent_n_years_is_open_period_not_two_digit_year() -> None:
    r = timeref.recent_period("최근 5년간 부산 1인가구수", TODAY)
    assert (r.year, r.open_end) == (2021, True)
    r = timeref.recent_period("지난 10년 동안 이혼 건수", TODAY)
    assert (r.year, r.open_end) == (2016, True)
    assert timeref.recent_period("최근 3개년 출생아", TODAY).year == 2023
    assert timeref.recent_period("5년 전 인구", TODAY) is None
    assert timeref.resolve("최근 5년간 인구", TODAY) is None
    assert sl.short_years("최근 10년 혼인 건수") == set()
    assert sl.short_years("지난 20년간 인구") == set()
    assert sl.short_years("10년 출생아") == {2010}
    s = sl.correct("최근 10년 혼인 건수", sl.empty_slots("최근 10년 혼인 건수"))
    assert s["conditions"]["year"] == date.today().year - 10
    assert s["time_ref"]["open_end"] is True


def test_open_period_wants_latest_table_and_drops_period_words() -> None:
    from app.services import search as se

    q = "최근 10년 시군구별 인구"
    s = sl.correct(q, sl.empty_slots(q))
    assert se.wants_current(s)
    assert "10" not in se.keyword_query(s)
    base = {
        "start_year": 2000,
        "sex_coverage": "none",
        "region_levels": [],
        "prev_region_levels": [],
    }
    old = {**base, "end_year": 2015}
    new = {**base, "end_year": date.today().year - 1}
    assert se.check({**old}, s).get("year") is False
    assert se.check({**new}, s).get("year") is True


def test_latest_words() -> None:
    from app.services import synonyms

    s = sl.correct("최신 5년 이혼 건수", sl.empty_slots("최신 5년 이혼 건수"))
    assert s["conditions"]["year"] == date.today().year - 5 and s["time_ref"]["open_end"]
    kw = synonyms.keyword_text("부산 인구수 제일 최신 데이터로")
    assert "최신" not in kw and "데이터" not in kw and "부산" in kw
    assert "최근" not in synonyms.keyword_text("가장 최근 통계로 출생아 수")


def test_open_period_latest_edition_of_family() -> None:
    from app.services import search as se

    q = "최근 5년 인구"
    s = sl.correct(q, sl.empty_slots(q))
    base = {
        "start_year": 2000,
        "sex_coverage": "none",
        "region_levels": [],
        "prev_region_levels": [],
    }
    last = date.today().year - 6
    r = se.check({**base, "end_year": last, "family_last_year": last}, s)
    assert r["year"] is True and r["period"] is False
    assert se.check({**base, "end_year": last, "family_last_year": last + 5}, s)["year"] is False
    assert (
        se.check({**base, "end_year": last - 5, "family_last_year": last - 5}, s)["year"] is False
    )


def test_slots_relative_year_and_projection() -> None:
    s = sl.correct("작년 출생아 수", sl.empty_slots("작년 출생아 수"))
    assert s["conditions"]["year"] == date.today().year - 1
    assert s["time_ref"]["expr"] == "작년" and s["projection"] is False
    s = sl.correct("내년 인구", sl.empty_slots("내년 인구"))
    assert s["projection"] is True
    s = sl.correct("2020년 작년 대비", sl.empty_slots("2020년 작년 대비"))
    assert (s["conditions"]["year"], s["time_ref"]) == (2020, None)


def test_age_groups() -> None:
    assert agegroup.resolve("노인 인구가 많은 동네").label() == "65세 이상"
    assert agegroup.resolve("청년들이 많이 떠나는 지역").label() == "19~34세"
    assert agegroup.resolve("일하는 나이대 100명이 노인 몇 명").lo == 15
    assert agegroup.resolve("65세 이상 노인 수") is None
    assert agegroup.resolve("어린이집 다니는 애들") is None
    assert agegroup.resolve("서울 인구") is None
    s = sl.correct("노인 인구", sl.empty_slots("노인 인구"))
    assert s["age"]["lo"] == 65 and s["age"]["hi"] is None


def test_age_coverage_and_long_years() -> None:
    from app.services import search as se

    assert se.age_coverage(
        se.norm("주요 연령계층별 추계인구(생산연령인구, 고령인구 등)/시도"), ["인구종류별"], ""
    ) == (0, None)
    assert se.age_coverage(se.norm("생활비원천별 고령자(65세 이상)-시군구"), [], "") == (65, None)
    assert se.age_coverage(se.norm("세대구성별 고령자 - 시군구"), [], "") == (65, None)
    s = sl.correct("한 집에서 10년 넘게 산 가구", sl.empty_slots("한 집에서 10년 넘게 산 가구"))
    assert s["conditions"]["year"] is None


def test_age_span_and_item_check() -> None:
    from app.services import search as se

    assert (agegroup.span("20대").lo, agegroup.span("20대").hi) == (20, 29)
    assert (agegroup.span("30대 이상").lo, agegroup.span("30대 이상").hi) == (30, None)
    assert agegroup.span("청소년").lo == 9
    assert agegroup.span("가구수") is None
    assert (agegroup.span("15세 미만").lo, agegroup.span("15세 미만").hi) == (0, 14)
    assert (agegroup.span("15세 이하").lo, agegroup.span("15세 이하").hi) == (0, 15)
    assert (agegroup.span("20대 후반").lo, agegroup.span("20대 후반").hi) == (27, 29)
    base = {
        "tbl_id": "A",
        "survey_name": "인구총조사",
        "organization": "",
        "period": "",
        "url": "",
        "path_texts": ["주제별통계 > 인구 > 미혼모"],
        "start_year": 2015,
        "end_year": 2025,
        "region_levels": ["시도"],
        "prev_region_levels": [],
        "sex_coverage": "none",
        "has_total": None,
        "family_id": "F",
        "family_name": "",
        "target": None,
        "sex": None,
        "edition_order": 1,
        "edition": "",
    }
    mom = se.prepare(
        [
            {
                **base,
                "table_name": "연령별 미혼모, 미혼부 - 시도",
                "items": [{"name": "미혼모"}],
                "classifications": [
                    {"name": "연령별", "sample_values": ["20세미만", "20~24세", "50세이상"]}
                ],
            }
        ]
    )[0]
    s = sl.correct("q", sl.empty_slots("q"))
    s["conditions"]["items"] = ["청소년", "20대", "30대 이상"]
    assert all(se.check(mom, s).values())
    s["conditions"]["items"] = ["미성년 자녀 셋 이상"]
    assert se.check(mom, s)["item:미성년 자녀 셋 이상"] is False


@pytest.mark.parametrize(
    ("text", "digits", "single"),
    [
        ("서른 살 여성의 출산율", "30세 여성의 출산율", "30세"),
        ("스물다섯 살", "25세", "25세"),
        ("스무 살 남자", "20세 남자", "20세"),
        ("서른한 살", "31세", "31세"),
        ("한 살 단위로", "1세 단위로", None),
        ("몇 살", "몇 살", None),
        ("두 살림", "두 살림", None),
        ("혼자 살고 있는", "혼자 살고 있는", None),
        ("65세 이상", "65세 이상", None),
        ("만 13세~만 15세", "만 13세~만 15세", None),
        ("20세와 30세", "20세와 30세", None),
    ],
)
def test_native_age_words_become_digits(text: str, digits: str, single: str | None) -> None:
    from app.services import agegroup

    assert agegroup.native_to_digits(text) == digits
    assert agegroup.single_age(text) == single


def test_age_said() -> None:
    from app.services import agegroup

    assert all(agegroup.said(q) for q in ("서른 살 여성", "20대 인구", "노인 인구"))
    assert not agegroup.said("몇 살에 결혼하는지") and not agegroup.said("서울 인구")
