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
    assert (
        timeref.resolve("올해 외국인 유학생이 작년보다 늘었는지", TODAY).year == 2026
    )  # 비교 기준은 빼고 시점만
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
    assert timeref.resolve("최근 5년간 인구", TODAY) is None  # QA 쪽 resolve 는 그대로
    # '최근 10년'은 2010년이 아니다 (두 자리 연도 규칙)
    assert sl.short_years("최근 10년 혼인 건수") == set()
    assert sl.short_years("지난 20년간 인구") == set()
    assert sl.short_years("10년 출생아") == {2010}  # 기간 표현이 없으면 그대로
    s = sl.correct("최근 10년 혼인 건수", sl.empty_slots("최근 10년 혼인 건수"))
    assert s["conditions"]["year"] == date.today().year - 10
    assert s["time_ref"]["open_end"] is True


def test_open_period_wants_latest_table_and_drops_period_words() -> None:
    from app.services import search as se

    q = "최근 10년 시군구별 인구"
    s = sl.correct(q, sl.empty_slots(q))
    assert se.wants_current(s)  # 최신 판 가산 대상
    assert "10" not in se.keyword_query(s)  # '10년'은 검색어에서 뺀다
    base = {"start_year": 2000, "sex_coverage": "none", "region_levels": [], "prev_region_levels": []}
    old = {**base, "end_year": 2015}
    new = {**base, "end_year": date.today().year - 1}
    assert se.check({**old}, s).get("year") is False  # 최신까지 안 이어지는 표
    assert se.check({**new}, s).get("year") is True



def test_latest_words() -> None:
    from app.services import synonyms

    # '최신 5년'도 '최근 5년'과 같은 끝이 열린 기간
    s = sl.correct("최신 5년 이혼 건수", sl.empty_slots("최신 5년 이혼 건수"))
    assert s["conditions"]["year"] == date.today().year - 5 and s["time_ref"]["open_end"]
    # '제일 최신 데이터로'·'가장 최근 통계로'는 검색어에서 뺀다 (최신 판 선택은 기본 동작)
    kw = synonyms.keyword_text("부산 인구수 제일 최신 데이터로")
    assert "최신" not in kw and "데이터" not in kw and "부산" in kw
    assert "최근" not in synonyms.keyword_text("가장 최근 통계로 출생아 수")


def test_open_period_latest_edition_of_family() -> None:
    from app.services import search as se

    q = "최근 5년 인구"
    s = sl.correct(q, sl.empty_slots(q))
    base = {"start_year": 2000, "sex_coverage": "none", "region_levels": [], "prev_region_levels": []}
    last = date.today().year - 6
    # 5년 주기 조사의 최신 판: 통과하되 요청 기간 일부만이면 부드러운 감점
    r = se.check({**base, "end_year": last, "family_last_year": last}, s)
    assert r["year"] is True and r["period"] is False
    # 같은 통계에 더 새 판이 있으면 옛 판은 그대로 탈락
    assert se.check({**base, "end_year": last, "family_last_year": last + 5}, s)["year"] is False
    # 최신 판이라도 너무 오래되면 탈락
    assert se.check({**base, "end_year": last - 5, "family_last_year": last - 5}, s)["year"] is False


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
    assert se.age_coverage(
        se.norm("주요 연령계층별 추계인구(생산연령인구, 고령인구 등)/시도"), ["인구종류별"], ""
    ) == (0, None)
    assert se.age_coverage(se.norm("생활비원천별 고령자(65세 이상)-시군구"), [], "") == (65, None)
    assert se.age_coverage(se.norm("세대구성별 고령자 - 시군구"), [], "") == (65, None)
    # '10년 넘게'는 연도가 아니다
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
    base = {"tbl_id": "A", "survey_name": "인구총조사", "organization": "", "period": "", "url": "",
            "path_texts": ["주제별통계 > 인구 > 미혼모"], "start_year": 2015, "end_year": 2025,
            "region_levels": ["시도"], "prev_region_levels": [], "sex_coverage": "none", "has_total": None,
            "family_id": "F", "family_name": "", "target": None, "sex": None, "edition_order": 1, "edition": ""}  # fmt: skip
    mom = se.prepare([{**base, "table_name": "연령별 미혼모, 미혼부 - 시도", "items": [{"name": "미혼모"}],
                       "classifications": [{"name": "연령별", "sample_values": ["20세미만", "20~24세", "50세이상"]}]}])[0]  # fmt: skip
    s = sl.correct("q", sl.empty_slots("q"))
    s["conditions"]["items"] = ["청소년", "20대", "30대 이상"]
    assert all(se.check(mom, s).values())
    # 연령 표현 말고 남는 말('자녀 셋')이 표에 없으면 연령 축만으로 통과시키지 않는다
    s["conditions"]["items"] = ["미성년 자녀 셋 이상"]
    assert se.check(mom, s)["item:미성년 자녀 셋 이상"] is False
