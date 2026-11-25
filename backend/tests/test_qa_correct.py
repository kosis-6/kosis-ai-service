from datetime import date

import pytest

from app.services import indicators, qa

pytestmark = pytest.mark.needs_data

ALIASES = {
    "총인구": ["인구", "인구수", "전체 인구", "주민등록인구"],
    "고령인구 비율": ["고령화율", "노인 비율", "65세 이상 비율", "고령층"],
    "유소년인구 비율": ["유소년 비율", "0~14세 비율"],
    "노령화지수": ["노령화 지수"],
    "합계출산율": ["출산율", "TFR"],
    "순이동": ["인구 순유입", "순유입", "빠져나간"],
    "다문화 출생아 수": ["다문화 출생 수"],
    "출생아 수": ["출생 수", "태어난 아이"],
    "다문화 출생 비중": ["다문화 가정 아이"],
    "출산순위별 출생 구성": ["첫째"],
    "첫째아 평균 출산연령": ["첫째를 낳는 나이"],
    "자연증가 건수": ["출생아보다 사망자"],
}
TODAY = date(2026, 10, 24)


REAL_ALIASES = indicators._alias_map


@pytest.fixture(autouse=True)
def fixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", lambda: ALIASES)


def parsed(**kw) -> dict:
    base = {"type": "value", "indicator": None, "regions": [], "year": None, "sex": "계"}
    return {**base, **kw}


def test_indicator_alias_overrides_llm() -> None:
    p = qa.correct(
        "최근 5년간 부산 고령화율 변화는?",
        parsed(type="trend", indicator="노령화지수", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert "corrections" in p


def test_indicator_phrase_with_change_word() -> None:
    p = qa.correct(
        "2015~2025년 대구 인구 증감률은?",
        parsed(type="trend", indicator="인구 증감률", regions=["대구"]),
        TODAY,
    )
    assert p["indicator"] == "총인구"


def test_generic_population_word_keeps_llm_indicator() -> None:
    p = qa.correct(
        "부산 인구에서 고령층이 차지하는 비중은?",
        parsed(indicator="고령인구 비율", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert "corrections" not in p


def test_female_elderly_ratio() -> None:
    p = qa.correct(
        "작년 부산 여성 노인 비율은?",
        parsed(indicator="여성 노인 비율", regions=["부산"], year=2023),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert p["sex"] == "여"
    assert p["year"] == 2025


@pytest.mark.parametrize(
    ("question", "by"),
    [
        ("2025년 전국 인구 중 남녀 비중은 어떻게 돼?", "sex"),
        ("서울 인구의 남자와 여자 구성비는?", "sex"),
        ("2025년 서울 인구의 연령대별 구성비는?", None),
        ("2020년 서울 아파트 가구 수를 세대구성별로 알려줘", None),
        ("2020년 서울 다문화가구 구성별 비중", None),
        ("서울 가구주성별 1인 가구 구성비", "sex"),
    ],
)
def test_composition_by_sex(question: str, by: str | None) -> None:
    p = qa.correct(question, parsed(type="composition", regions=["서울"]), TODAY)
    assert p.get("by") == by


@pytest.mark.parametrize(
    ("text", "sex"),
    [
        ("세대구성별 아파트 가구 수", False),
        ("특성별 인구", False),
        ("남녀 인구", True),
        ("성별 인구", True),
    ],
)
def test_sex_word_inside_other_class_names(text: str, sex: bool) -> None:
    from app.services import slots

    assert bool(qa.SEX_SPLIT.search(text)) is sex
    assert bool(qa.PEOPLE_INDICATOR.search(text)) is (sex or "인구" in text)
    assert bool(slots.BOTH_SEX.search(text)) is sex


def test_nationwide_region() -> None:
    p = qa.correct(
        "2009년부터 2016년까지 전국 인구 변화는?",
        parsed(type="trend", indicator="총인구", regions=[]),
        TODAY,
    )
    assert p["regions"] == ["전국"]


def test_no_change_when_llm_is_right() -> None:
    p = qa.correct(
        "2025년 서울 고령인구 비율은?",
        parsed(indicator="고령인구 비율", regions=["서울"], year=2025),
        TODAY,
    )
    assert "corrections" not in p


def test_longer_alias_wins() -> None:
    assert {n for *_, n in indicators.find_in_text("세종 유소년인구 비율 비교")} == {
        "유소년인구 비율"
    }
    assert {n for *_, n in indicators.find_in_text("서울 인구 순유입은?")} == {"순이동"}


def test_unsupported_lookup_indicator_is_named() -> None:
    p = qa.correct("2024년 서울 출산율은?", parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["indicator"] == "합계출산율"


@pytest.mark.parametrize(
    "question",
    [
        "서울 고령화율과 노령화지수를 비교해줘",
        "저출산 정책이 효과가 있었나요?",
    ],
)
def test_keeps_llm_indicator(question: str) -> None:
    p = qa.correct(question, parsed(indicator="HCX 해석"), TODAY)
    assert p["indicator"] == "HCX 해석"


def test_two_indicators_are_both_answered() -> None:
    p = qa.correct("서울 고령화율과 노령화지수를 비교해줘", parsed(indicator="HCX 해석"), TODAY)
    assert p["indicators"] == ["고령인구 비율", "노령화지수"]


def test_both_genders_or_rank_untouched() -> None:
    p = qa.correct(
        "2025년 전국 시도 중 남성과 여성 인구 차이가 큰 곳은?",
        parsed(type="rank", indicator="총인구"),
        TODAY,
    )
    assert p["sex"] == "계"
    assert p["regions"] == []


def test_relative_years() -> None:
    assert qa.correct("올해 서울 인구는?", parsed(indicator="총인구"), TODAY)["year"] == 2026
    assert qa.correct("재작년 서울 인구는?", parsed(indicator="총인구"), TODAY)["year"] == 2024
    p = qa.correct("작년까지 5년간 변화", parsed(type="trend", indicator="총인구"), TODAY)
    assert p["year"] is None


def test_age_band_population_count() -> None:
    p = qa.correct(
        "2025년 대전 유성구의 만 6~11세 인구는 몇 명이에요?",
        parsed(indicator="유소년인구 비율", regions=["대전 유성구"]),
        TODAY,
    )
    assert p["indicator"] == "총인구"
    assert p["ages"] == [[6, 11]]
    p = qa.correct("서울 20대(20~29세) 인구는?", parsed(indicator="총인구"), TODAY)
    assert p["ages"] == [[20, 29]]


def test_extract_ages() -> None:
    assert qa.extract_ages("서울로 이사 온 20대랑 30대") == [[20, 29], [30, 39]]
    assert qa.extract_ages("65세 이상 인구") == [[65, None]]
    assert qa.extract_ages("2024년 다문화 혼인 건수를 5대 광역시끼리") == []
    assert qa.extract_ages("15세 미만 인구") == [[0, 14]]
    assert qa.extract_ages("15세 이하 인구") == [[0, 15]]
    assert qa.extract_ages("60대 이상 인구") == [[60, None]]
    assert qa.extract_ages("20~30대 인구") == [[20, 39]]
    assert qa.extract_ages("20대 후반 남자") == [[27, 29]]
    assert qa.extract_ages("30대 초반과 40대 중반") == [[30, 33], [44, 46]]
    assert qa.extract_ages("20대(20~29세) 인구") == [[20, 29]]


def test_ratio_indicator_for_count_question_is_refused() -> None:
    p = qa.correct(
        "2025년 서울 1인 가구는 몇 가구예요?",
        parsed(indicator="고령인구 비율", regions=["서울"], term="1인 가구"),
        TODAY,
    )
    assert p["unsupported"] is True
    assert p["indicator"] == "1인 가구"


def test_other_with_stats_words_is_unsupported_indicator() -> None:
    p = qa.correct("2025년 서울 1인 가구는 몇 가구예요?", parsed(type="other"), TODAY)
    assert p["type"] == "value" and p["unsupported"] is True
    p = qa.correct("내일 울산에 비 와요?", parsed(type="other"), TODAY)
    assert p["type"] == "other"


def test_composition_needs_share_words() -> None:
    p = qa.correct(
        "2023년 서울 지역의 구별 20대(20~29세) 인구는 몇 명인가요?",
        parsed(type="composition", indicator="총인구", regions=["서울"]),
        TODAY,
    )
    assert (p["type"], p["level"], p["sido"], p["regions"]) == ("rank", "시군구", "서울", [])


def test_more_specific_indicator_wins() -> None:
    q = "작년에 인구가 가장 많이 빠져나간 시군구는?"
    p = qa.correct(q, parsed(type="rank", indicator="총인구"), TODAY)
    assert p["indicator"] == "순이동" and p["ascending"] is True
    p = qa.correct(
        "2024년 안산시에서 태어난 아이 중 다문화 가정 아이는 몇 %예요?",
        parsed(indicator="출생아 수", regions=["안산시"]),
        TODAY,
    )
    assert p["indicator"] == "다문화 출생 비중"
    p = qa.correct("2021년 시흥시의 다문화 출생 수는?", parsed(indicator="출생아 수"), TODAY)
    assert p["indicator"] == "다문화 출생아 수"


def test_question_conditions() -> None:
    p = qa.correct("2024년 충청권 다문화 혼인 건수 합계는요?", parsed(), TODAY)
    assert p["regions"] == ["대전", "세종", "충북", "충남"] and p["aggregate"] == "sum"
    p = qa.correct(
        "1990년 이후 전북 합계출산율이 가장 낮았던 해는 언제예요?",
        parsed(type="rank", regions=["전북"], start_year=1990),
        TODAY,
    )
    assert p["type"] == "trend" and p["asked"]["extreme"] == "min"
    p = qa.correct(
        "2025년 출생아 수는 작년보다 얼마나 늘었어요?",
        parsed(indicator="출생아 수", year=2025),
        TODAY,
    )
    assert (p["type"], p["start_year"], p["end_year"]) == ("trend", 2024, 2025)
    q = "2024년 안산시 다문화 출생아는 수원시의 몇 배예요?"
    p = qa.correct(q, parsed(type="compare"), TODAY)
    assert p["asked"]["ratio"] is True
    p = qa.correct(
        "2024년 다문화 출생아 수로 보면 안산시는 전국 시 중 몇 위예요?",
        parsed(type="rank", regions=["안산시"]),
        TODAY,
    )
    assert (p["level"], p["target"]) == ("시", "안산시")
    p = qa.correct("2025년 서울 25개 구 중에 합계출산율 꼴찌는?", parsed(type="rank", top=1), TODAY)
    assert p["top"] == 1 and p["ascending"] is True


def test_span_causal_and_nationwide() -> None:
    p = qa.correct("2015년 대비 2025년 대구 총인구 증감률은?", parsed(regions=["대구"]), TODAY)
    assert (p["type"], p["start_year"], p["end_year"]) == ("trend", 2015, 2025)
    p = qa.correct("최근 10년간 저출산 정책이 효과가 있었나요?", parsed(type="trend"), TODAY)
    assert p["type"] == "causal"
    p = qa.correct("2025년 우리나라 합계출산율은 얼마예요?", parsed(regions=["우리나라"]), TODAY)
    assert p["regions"] == ["전국"]
    q = "부산으로 이사 온 사람 중 일자리 때문에 온 비율은?"
    p = qa.correct(q, parsed(type="composition"), TODAY)
    assert p["type"] != "causal"


def test_round2_misreads() -> None:
    p = qa.correct(
        "2025년에 결혼을 더 많이 한 곳은 부산이에요, 대구예요?",
        parsed(type="other", indicator="혼인 건수", regions=["부산", "대구"]),
        TODAY,
    )
    assert (p["type"], p["indicator"]) == ("compare", "혼인 건수") and "unsupported" not in p
    p = qa.correct(
        "작년에 인구가 가장 많이 빠져나간 시군구는 어디예요?", parsed(indicator="순이동"), TODAY
    )
    assert (p["type"], p["level"]) == ("rank", "시군구")
    p = qa.correct(
        "1970년 이후 아이가 가장 적게 태어난 해 3개는 언제예요?", parsed(indicator="아이"), TODAY
    )
    assert p["indicator"] == "출생아 수" and p["type"] == "trend"
    p = qa.correct("2025년 복지 정책이 효과적이었나요?", parsed(type="other"), TODAY)
    assert p["type"] == "causal"


def test_since_year_runs_to_latest() -> None:
    p = qa.correct(
        "1970년 이후 아이가 가장 적게 태어난 해 3개는 언제예요?",
        parsed(type="trend", indicator="출생아 수", start_year=1970, end_year=2022),
        TODAY,
    )
    assert (p["start_year"], p["end_year"], p["asked"]["since"]) == (1970, None, 1970)


def test_merge_province_and_district() -> None:
    assert qa._merge_regions(["서울", "중구"]) == ["서울 중구"]
    assert qa._merge_regions(["서울", "부산"]) == ["서울", "부산"]


def test_rank_from_compare_and_target_from_text() -> None:
    p = qa.correct(
        "2025년 여자 평균초혼연령이 가장 높은 시도랑 가장 낮은 시도는?",
        parsed(type="compare", indicator="평균 초혼연령", sex="여"),
        TODAY,
    )
    assert (p["type"], p["level"], p.get("top")) == ("rank", "시도", None)
    p = qa.correct(
        "2024년 다문화 출생아 수로 보면 안산시는 전국 시 중 몇 위예요?",
        parsed(type="rank", indicator="다문화 출생아 수"),
        TODAY,
    )
    assert p["target"] == "안산시" and p["level"] == "시"


def test_streak_uses_whole_period() -> None:
    p = qa.correct(
        "세종시는 몇 년 연속 인구가 순유입됐나요?",
        parsed(
            type="trend", indicator="순이동", regions=["세종시"], start_year=2019, end_year=2023
        ),
        TODAY,
    )
    assert (p["start_year"], p["end_year"], p["asked"]["streak"]) == (None, None, "positive")


def test_sub_area_and_ungrounded_region() -> None:
    q = "2015년부터 2020년까지 송도의 65세 이상 인구의 증감율은 어떻게 되나요?"
    p = qa.correct(q, parsed(type="trend", indicator="총인구", regions=["인천광역시"]), TODAY)
    assert p["sub_area"] == ["송도", "인천 연수구"]
    assert p["ungrounded_region"] == "인천광역시"
    p1 = qa.correct("2025년 부산 해운대구랑 수영구 합친 65세 이상 인구는?", parsed(), TODAY)
    p2 = qa.correct("2025년 성남시 분당구 인구는?", parsed(), TODAY)
    assert "sub_area" not in p1 and "sub_area" not in p2
    p = qa.correct("2025년 서울 인구는?", parsed(regions=["서울특별시"]), TODAY)
    assert "ungrounded_region" not in p
    for q, r in [
        ("세종시는 몇 년 연속 순유입됐나요?", "세종특별자치시"),
        ("2025년 중구 인구는?", "서울 중구"),
        ("시흥시의 다문화 출생 수는?", "경기도 시흥시"),
        ("강원도 일반혼인율은?", "강원특별자치도"),
    ]:
        assert "ungrounded_region" not in qa.correct(q, parsed(regions=[r]), TODAY), q


def test_full_region_name_from_question() -> None:
    q = "서울 광주광역시 2018년부터 2024년까지 인구 추이"
    p = qa.correct(q, parsed(type="trend", indicator="총인구", regions=["서울", "광주"]), TODAY)
    assert p["regions"] == ["서울", "광주광역시"]
    p = qa.correct("광주 인구 추이", parsed(type="trend", regions=["광주"]), TODAY)
    assert p["regions"] == ["광주"]


def test_latest_without_change_is_latest_value() -> None:
    def hcx() -> dict:
        return parsed(
            type="trend", indicator="고령인구 비율", regions=["부산"], year=2025,
            start_year=2020, end_year=2025,
        )  # fmt: skip

    p = qa.correct("최근 부산 고령인구 비율", hcx(), TODAY)
    assert (p["type"], p.get("start_year"), p.get("end_year")) == ("value", None, None)
    assert p["asked"]["latest"] and p.get("year") is None
    p = qa.correct(
        "최근 서울 출생아 수", parsed(indicator="출생아 수", regions=["서울"], year=2024), TODAY
    )
    assert p.get("year") is None
    for q in ["최근 부산 고령인구 비율 추이", "요즘 부산 고령인구 비율이 늘었어?",
              "최근 5년 부산 고령인구 비율"]:  # fmt: skip
        assert qa.correct(q, hcx(), TODAY)["type"] == "trend", q
    p = qa.correct("최근 부산 고령인구 비율 추이", hcx(), TODAY)
    assert (p.get("start_year"), p.get("end_year"), p.get("year")) == (None, None, None)


def test_unit_suffix_in_indicator_name_is_dropped() -> None:
    q = "2024년 서울 출생아 수"
    p = qa.correct(q, parsed(indicator="출생아 수(명)", regions=["서울"]), TODAY)
    assert p["indicator"] == "출생아 수"


def test_other_composition_question() -> None:
    p = qa.correct(
        "2025년 전국 인구 중 남녀 비중은 어떻게 돼?",
        parsed(type="other", indicator="성별 인구 비율", regions=["전국"]),
        TODAY,
    )
    assert (p["type"], p["indicator"], p["by"]) == ("composition", "총인구", "sex")


def test_explicit_year_wins() -> None:
    q = "2024년에 서울로 이사 온 20대랑 30대는 이사 온 이유가 어떻게 달라요?"
    p = qa.correct(q, parsed(type="composition", indicator="전입 사유 구성", year=2025), TODAY)
    assert p["year"] == 2024


def test_region_list_scope_from_question() -> None:
    q = "2024년 서울 지역의 구별 15세 이상 여성중 배우자가 있는 여성의 비율은 어떻게 되나요?"
    p = qa.correct(
        q,
        parsed(
            type="composition",
            indicator="유배우 비율",
            regions=["강남구", "서초구", "송파구"],
            sex="여",
            sido="서울",
        ),
        TODAY,
    )
    assert (p["type"], p["sido"], p["regions"]) == ("rank", "서울", [])


def test_share_of_nation_read_as_compare() -> None:
    p = qa.correct(
        "2025년 전국 출생아 중 서울 아이들 비중은 얼마예요?",
        parsed(type="compare", indicator="출생아 수", regions=["전국", "서울"], year=2025),
        TODAY,
    )
    assert p["type"] == "value"
    assert p["regions"] == ["서울"]
    assert p["share_of"] == "전국"


def test_compare_two_regions_with_nation_stays_compare() -> None:
    p = qa.correct(
        "2025년 출생아 수 서울이랑 부산 비율을 전국과 비교해 주세요",
        parsed(type="compare", indicator="출생아 수", regions=["전국", "서울", "부산"], year=2025),
        TODAY,
    )
    assert p["type"] == "compare"
    assert "share_of" not in p


@pytest.mark.parametrize(
    ("question", "n"),
    [
        ("과거 10년 동안 서울 인구 변화", 10),
        ("지난 5년간 출생아 수 추이", 5),
        ("최근 3개년 혼인 건수", 3),
    ],
)
def test_past_n_years_is_recent_n_years(question, n) -> None:
    p = qa.correct(question, parsed(type="trend", indicator="총인구", regions=["서울"]), TODAY)
    assert p["asked"]["recent"] == n


def test_from_past_is_whole_period_trend() -> None:
    p = qa.correct(
        "과거부터 지금까지 전국 합계출산율 추이",
        parsed(type="value", indicator="합계출산율", regions=["전국"], start_year=2015),
        TODAY,
    )
    assert p["type"] == "trend" and p["asked"]["from_start"] is True
    assert p.get("start_year") is None


def test_from_past_keeps_end_year_in_question() -> None:
    p = qa.correct(
        "과거부터 2010년까지 전국 합계출산율 추이",
        parsed(type="trend", indicator="합계출산율", regions=["전국"], start_year=1970),
        TODAY,
    )
    assert p["asked"]["from_start"] is True
    assert p.get("start_year") is None and p["end_year"] == 2010


def test_from_past_drops_guessed_end_year() -> None:
    p = qa.correct(
        "과거부터 지금까지 전국 합계출산율 추이",
        parsed(type="trend", indicator="합계출산율", regions=["전국"], end_year=2020),
        TODAY,
    )
    assert p.get("end_year") is None


def test_from_past_computed_indicator_starts_at_first_year(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    q = {"type": "trend", "asked": {"from_start": True}, "start_year": None, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    q = {"type": "trend", "asked": {}, "start_year": None, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2008, 2025), (2020, 2025)]


def test_recent_n_years_computed_indicator(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    q = {"type": "trend", "asked": {"recent": 5}, "start_year": 2019, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2021, 2025)]


@pytest.mark.parametrize(
    ("question", "llm", "grounded"),
    [
        ("최근 5년 부산 고령화율", "고령인구 비율", True),
        ("서울 1인 가구 중 여성 비율", "총인구", False),
    ],
)
def test_grounded_marks_indicator_named_in_question(question, llm, grounded) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["grounded"] is grounded


def test_grounded_is_none_without_indicator() -> None:
    p = qa.correct("2025년 복지 정책이 효과적이었나요?", parsed(type="causal", indicator=""), TODAY)
    assert p["grounded"] is None


FOREIGN = {
    **ALIASES,
    "외국인주민 수": ["외국인 주민 수", "외국인주민", "외국인 수", "외국인 주민"],
    "외국인주민 비율": ["외국인 비율"],
}
NOT_IN = {
    "외국인주민 수": ["등록외국인", "체류외국인"],
    "외국인주민 비율": ["등록외국인", "체류외국인"],
}


@pytest.fixture
def foreign_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", lambda: FOREIGN)
    monkeypatch.setattr(indicators, "_exclusions", lambda: NOT_IN)


@pytest.mark.usefixtures("foreign_aliases")
def test_excluded_phrase_is_not_an_alias_hit() -> None:
    assert indicators.find_in_text("2023년 경기 등록외국인 수") == []
    assert [n for *_, n in indicators.find_in_text("2023년 경기 외국인 수")] == ["외국인주민 수"]


@pytest.mark.usefixtures("foreign_aliases")
@pytest.mark.parametrize(
    "question",
    ["2023년 경기 등록외국인 수", "서울 체류외국인 수는?", "2023년 경기 등록외국인 비율"],
)
def test_other_foreigner_statistics_are_unsupported(question: str) -> None:
    for llm_pick in ("외국인주민 수", "외국인주민 비율"):
        p = qa.correct(question, parsed(indicator=llm_pick, regions=["경기"]), TODAY)
        assert p["unsupported"] is True
        assert indicators.canonical_name(p["indicator"]) is None


@pytest.mark.usefixtures("foreign_aliases")
def test_plain_foreigner_question_still_answers() -> None:
    p = qa.correct(
        "2023년 경기 외국인 수", parsed(indicator="외국인주민 수", regions=["경기"]), TODAY
    )
    assert p["indicator"] == "외국인주민 수"
    assert not p.get("unsupported")


def test_extract_ages_with_sal() -> None:
    assert qa.extract_ages("대전청년 19~34살 몇명?") == [[19, 34]]
    assert qa.extract_ages("울산에 100살 이상 어르신") == [[100, None]]
    assert qa.extract_ages("15살 안 된 애들") == [[0, 14]]


@pytest.mark.parametrize(
    ("question", "ages"),
    [
        ("동래구에서 재작년 돌아가신 분 중 62살에서 67살 사이는?", [[62, 67]]),
        ("등록 외국인 가운데 33살부터 38살까지는 몇 명?", [[33, 38]]),
        ("33세부터 38세까지 인구", [[33, 38]]),
        ("62세와 67세 사이 인구", [[62, 67]]),
        ("62에서 67살", [[62, 67]]),
        ("65세 이상 70세 미만 인구", [[65, 69]]),
        ("65살 이상 69살 이하", [[65, 69]]),
        ("20대부터 30대까지 여자", [[20, 39]]),
        ("2020년부터 2025년까지 인구", []),
        ("2015년에서 2020년 사이 30대", [[30, 39]]),
        ("3명에서 5명 사이", []),
    ],
)
def test_extract_ages_spoken_range(question: str, ages: list) -> None:
    assert qa.extract_ages(question) == ages


def test_age_group_grounds_age_defined_indicator() -> None:
    p = qa.correct(
        "우리나라 사람 가운데 노인은 몇 퍼센트쯤 돼요?",
        parsed(indicator="고령인구 비율 (%)", regions=["전국"], term="노인이 몇 퍼센트"),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율" and not p.get("unsupported")
    p = qa.correct(
        "군위군은 애들보다 노인이 몇 배나 많아?",
        parsed(indicator="노령화지수", regions=["군위군"], term="노령화지수"),
        TODAY,
    )
    assert p["indicator"] == "노령화지수" and not p.get("unsupported")
    p = qa.correct(
        "의성군 노인 몇 배야?", parsed(indicator="노령화지수", regions=["의성군"]), TODAY
    )
    assert p.get("unsupported") is True


def test_age_group_count_without_population_word() -> None:
    p = qa.correct(
        "대전이랑 충남 노인 다 합치면 몇 명이야",
        parsed(indicator="고령인구 비율", regions=["대전광역시", "충청남도"]),
        TODAY,
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[65, None]]
    p = qa.correct(
        "요 몇 년 새 부산 어르신들 얼마나 늘었어?",
        parsed(type="trend", indicator="고령인구 비율", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[65, None]]


@pytest.mark.parametrize(
    "question",
    [
        "인구가 많고 집값이 비싼 서울의 2020년 1인 가구 수",
        "사람이 많이 사는 서울의 1인 가구 수는?",
        "인구 밀집 지역인 서울의 1인 가구 수",
    ],
)
def test_population_word_in_modifier_does_not_replace_said_indicator(question: str) -> None:
    p = qa.correct(question, parsed(indicator="1인 가구", term="1인 가구 수", regions=["서울"]))
    assert p["indicator"] == "1인 가구"


@pytest.mark.parametrize(
    ("name", "display"),
    [
        ("울릉도", "경상북도 울릉군"),
        ("강화도", "인천광역시 강화군"),
        ("거제도", "경상남도 거제시"),
        ("경기도", "경기도"),
    ],
)
def test_island_names_resolve_to_their_county(name: str, display: str) -> None:
    r = indicators.resolve_region(name)
    assert r["status"] == "ok" and r["display"] == display


def test_unknown_island_name_stays_unknown() -> None:
    assert indicators.resolve_region("독도")["status"] == "unknown_region"


@pytest.mark.parametrize(
    ("question", "modifier"),
    [
        ("2024년 울릉도 인구는 몇 명이야?", None),
        ("수원 인구 몇 명", None),
        ("통학 인구 몇 명", "통학"),
        ("외국인 인구", "외국인"),
    ],
)
def test_region_word_before_population_is_not_a_modifier(question: str, modifier) -> None:
    assert qa.population_modifier(question) == modifier


def test_population_word_still_replaces_unsaid_indicator() -> None:
    p = qa.correct("서울 인구 몇 명이야?", parsed(indicator="주민 수", regions=["서울"]), TODAY)
    assert p["indicator"] == "총인구"


@pytest.mark.parametrize("hcx", ["성비", "고령인구 비율"])
def test_population_composition_read_as_other_indicator(hcx: str) -> None:
    p = qa.correct(
        "2020년 서울 남녀 인구 구성",
        parsed(type="composition", indicator=hcx, regions=["서울"], year=2020),
        TODAY,
    )
    assert p["indicator"] == "총인구" and not p.get("unsupported") and p["by"] == "sex"


def test_unknown_label_that_is_age_population() -> None:
    p = qa.correct(
        "대전청년 19~34살 몇명?", parsed(indicator="19~34세 인구 수", regions=["대전"]), TODAY
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[19, 34]]
    p = qa.correct("애들 몇 명이야?", parsed(indicator="유소년인구 수", term="애들"), TODAY)
    assert p["indicator"] == "총인구" and p["ages"] == [[0, 14]]
    p = qa.correct(
        "광주에 100살 이상 어르신은 몇 분이야?",
        parsed(type="other", indicator="", term="100살 이상 어르신", regions=["광주"]),
        TODAY,
    )
    assert (p["type"], p["indicator"], p["ages"]) == ("value", "총인구", [[100, None]])


def test_age_population_needs_people_only() -> None:
    p = qa.correct(
        "서울 청년 취업자 몇 명이야?", parsed(indicator="청년 취업자 수", regions=["서울"]), TODAY
    )
    assert p["indicator"] != "총인구"
    p = qa.correct(
        "서울 노인 1인 가구 몇 가구야?", parsed(indicator="고령인구 비율", regions=["서울"]), TODAY
    )
    assert p["indicator"] != "총인구" and p.get("unsupported") is True


@pytest.mark.parametrize("llm", ["고령인구 비율", ""])
@pytest.mark.parametrize("question", ["청년 취업자 몇 명이야?", "취업자 중 청년 몇 명이야?"])
def test_other_target_with_age_is_not_population(question: str, llm: str) -> None:
    p = qa.correct(question, parsed(indicator=llm), TODAY)
    assert p["indicator"] != "총인구"


@pytest.mark.parametrize(
    ("question", "regions"),
    [
        ("대전이랑 충남 노인 다 합치면 몇 명이야", ["대전광역시", "충청남도"]),
        ("30대 중에 서울 사는 사람 몇 명", ["서울"]),
        ("서울 사는 사람 중 30대 몇 명", ["서울"]),
        ("20대 여자 몇 명이야", []),
    ],
)
def test_age_population_with_filler_words(question: str, regions: list) -> None:
    p = qa.correct(question, parsed(indicator="", regions=regions), TODAY)
    assert p["indicator"] == "총인구"


@pytest.mark.parametrize(
    ("question", "ok"),
    [
        ("애들보다 노인이 몇 배 많아?", True),
        ("유소년 대비 노인 몇 배야", True),
        ("노인이랑 애들 몇 배 차이야", True),
        ("노인보다 애들이 몇 배 많아?", False),
        ("노인 대비 유소년 몇 배야", False),
    ],
)
def test_aging_index_direction(question: str, ok: bool) -> None:
    p = qa.correct(question, parsed(indicator="노령화지수"), TODAY)
    assert (p["indicator"] == "노령화지수" and not p.get("unsupported")) is ok


def test_births_versus_deaths_is_natural_increase() -> None:
    p = qa.correct("작년엔 태어난 사람이 많았어 돌아가신 사람이 많았어?", {"type": "other"}, TODAY)
    assert (p["type"], p["indicator"]) == ("value", "자연증가 건수")
    assert not p.get("unsupported")


@pytest.fixture
def data_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))


@pytest.mark.usefixtures("data_years")
@pytest.mark.parametrize(
    ("question", "year", "prediction"),
    [
        ("출산율이 앞으로 다시 오를까요?", None, True),
        ("서울 인구는 계속 줄어들까?", None, True),
        ("향후 고령인구 비율 전망", None, True),
        ("2040년 서울 인구는 몇 명이 될까요?", 2040, False),
        ("2060년 전남 고령인구 비율은 얼마로 예상돼요?", 2060, False),
        ("2025년 서울 출생아 수", 2025, False),
    ],
)
def test_prediction_question(question: str, year, prediction: bool) -> None:
    p = qa.correct(question, parsed(type="trend", indicator="합계출산율", year=year), TODAY)
    assert bool(p.get("prediction")) is prediction


@pytest.mark.usefixtures("data_years")
@pytest.mark.parametrize(
    ("question", "condition"),
    [
        ("2025년 체중 3.2kg으로 태어난 아기는 몇 명이야?", "체중"),
        ("3.2kg 출생아 수", "3.2kg"),
        ("결혼 1년 반 만에 이혼한 부부는 몇 쌍이야?", "1년 반"),
        ("동거기간 7년 이상 이혼 건수", "동거기간"),
        ("2025년 서울 출생아 수", None),
        ("최근 5년 서울 이혼 건수", None),
    ],
)
def test_unsupported_condition(question: str, condition) -> None:
    p = qa.correct(question, parsed(indicator="출생아 수", regions=["서울"]), TODAY)
    assert p.get("unsupported_condition") == condition


def test_unsupported_condition_is_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa.lookup, "refresh_if_reloaded", lambda: False)
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    q = parsed(indicator="출생아 수", regions=[], year=2025, unsupported_condition="3.2kg")
    r = qa.compute(q)
    assert r["status"] == "band_mismatch" and "'3.2kg' 조건" in r["reason"]
    assert "조건 없는 전국 출생아 수는 확인할 수 있습니다" in r["alternative"]


@pytest.mark.parametrize(
    ("question", "word"),
    [
        ("2010년 통학 인구는 몇 명이야?", "통학"),
        ("외국인 인구", "외국인"),
        ("2025년 서울의 인구는?", None),
        ("65세 이상 인구는?", None),
        ("남자 인구", None),
        ("수도권 인구 합계", None),
        ("만 6~11세 초등학생 나이 인구는?", None),
        ("작년에 인구가 줄었나", None),
        ("주민등록인구", None),
        ("강남 인구 알려줘", None),
        ("수원 인구는?", None),
    ],
)
def test_population_modifier(monkeypatch, question: str, word) -> None:
    regions = {"서울", "서울의"}
    monkeypatch.setattr(indicators, "find_regions", lambda t: [t] if t in regions else [])
    assert qa.population_modifier(question) == word


def test_total_population_with_other_statistic_word_is_not_grounded(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "find_regions", lambda t: [])
    p = qa.correct("2010년 전국 통학 인구는 몇 명이야?", parsed(indicator="총인구"), TODAY)
    assert p["grounded"] is False
    p = qa.correct("2010년 전국 인구는 몇 명이야?", parsed(indicator="총인구"), TODAY)
    assert p["grounded"] is True


@pytest.fixture
def registered_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    aliases = {
        **ALIASES,
        "등록외국인 수": ["등록외국인", "등록된 외국인"],
        "등록외국인 비율": ["등록된 외국인 비율"],
    }
    monkeypatch.setattr(indicators, "_alias_map", lambda: aliases)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("마포구 주민 가운데 등록된 외국인은 몇 프로 정도야?", "등록외국인 비율"),
        ("인천 등록외국인 몇 %야", "등록외국인 비율"),
        ("마포구 등록된 외국인 몇 명이야?", "등록외국인 수"),
        ("마포구 등록외국인 작년보다 몇 퍼센트 늘었어?", "등록외국인 수"),
        ("전국 등록외국인 중 마포구는 몇 프로야?", "등록외국인 수"),
    ],
)
def test_percent_question_uses_the_ratio_sibling(registered_aliases, question, expected) -> None:
    for llm in ("등록외국인 수", "몇 프로"):
        p = qa.correct(question, parsed(indicator=llm, term="몇 프로", regions=["마포구"]), TODAY)
        assert p["indicator"] == expected
        assert not p.get("unsupported")


@pytest.mark.parametrize(
    ("question", "llm", "expected"),
    [
        ("2024년 서울에서 아기 몇 명 낳았어?", "출생아 수", "출생아 수"),
        ("2024년 서울에서 아기 몇 명 낳았어?", "합계출산율", "출생아 수"),
        ("2024년 서울 여자 한 명이 평균 아이 몇 명 낳아?", "출생아 수", "합계출산율"),
        ("여성 1명당 아기 몇 명 낳아?", None, "합계출산율"),
    ],
)
def test_babies_born_or_per_woman(question: str, llm, expected: str) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["indicator"] == expected


@pytest.mark.parametrize("question", ["서울 2030세대 인구는?", "서울 2030 세대 인구는?"])
def test_generation_population_is_total_population(question: str) -> None:
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"], year=2030), TODAY)
    assert p["indicator"] == "총인구" and p["grounded"] is not False
    assert p["ages"] == [[20, 39]] and p["year"] is None


@pytest.mark.parametrize(
    ("question", "llm", "expected"),
    [
        ("수영구는 일할 나이 사람 100명당 노인이 몇 명이야?", "총인구", "노년부양비"),
        ("청송군 노인 수가 아이들 수의 몇 배나 돼?", "고령인구 비율", "노령화지수"),
        ("중구 사람들 나이 순으로 세우면 가운데가 몇 살?", None, "중위연령"),
        ("광주랑 강원, 인구에 비해 아기 태어나는 정도 비교해 줘", "출생아 수", "조출생률"),
        ("작년 이사 온 사람에서 떠난 사람 빼면 가장 많이 남은 시도 다섯 곳", "순이동", "순이동"),
        ("노인 비율 제일 높은 지역 다섯 군데", "고령인구 비율%", "고령인구 비율"),
        ("광주랑 울산 중 여자 1명이 평생 낳는 아이 수가 더 많은 곳?", "합계출산율", "합계출산율"),
        ("요즘 사람들은 애를 몇 명씩 낳아?", "출생아 수", "합계출산율"),
        ("북구에 사는 이십대는 얼마나 돼?", "총인구", "총인구"),
        ("대전하고 충청남도 어르신 수 합쳐 줘", "고령인구 수", "총인구"),
        ("해운대구는 일할 나이 100명이 노인 몇 명 부양해?", "노년부양비", "노년부양비"),
        ("고흥이랑 화성, 젊은 사람 대비 노인 부담 얼마나 차이 나?", "노년부양비", "노년부양비"),
        ("세종은 일할 나이 100명이 아이 몇 명 부양해?", "총인구", "유소년부양비"),
    ],
)
def test_spelled_out_indicators(question: str, llm, expected: str) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["indicator"] == expected
    assert p["grounded"] is not False


@pytest.mark.parametrize(
    "question",
    ["노원구가 강남구보다 사람이 많아 적어?", "부산에서 사람 제일 많이 사는 구 세 곳"],
)
def test_people_count_is_grounded_without_other_population_modifier(question: str) -> None:
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["indicator"] == "총인구"
    assert p["grounded"] is True


@pytest.mark.parametrize(
    "question",
    [
        "강원도에 외국인으로 등록한 사람 몇 명이야?",
        "2015년에 지하철로 통근하거나 통학한 사람은 몇 명?",
        "재작년 사망자 가운데 일흔 살 넘은 분 몇 명?",
        "혼자 사는 사람 몇 명이야?",
        "장애가 있는 사람 수 알려줘",
        "아파트에 사는 사람 몇 명?",
    ],
)
def test_other_people_are_not_total_population(question: str) -> None:
    p = qa.correct(question, parsed(indicator=None, regions=["서울"]), TODAY)
    assert p["indicator"] != "총인구"


@pytest.mark.parametrize(
    ("question", "ages"),
    [
        ("달서구에 이십대 몇 명 살아?", [[20, 29]]),
        ("부산에 백 살 이상 되신 분 몇 분 계셔?", [[100, None]]),
        ("재작년 사망자 중 일흔 살 이상", [[70, None]]),
        ("오십 대 남자", [[50, 59]]),
    ],
)
def test_korean_number_ages(question: str, ages) -> None:
    assert qa.extract_ages(question) == ages


def test_korea_population_is_nationwide() -> None:
    p = qa.correct("한국 인구에서 노인 비중은 몇 %?", parsed(indicator="고령인구 비율"), TODAY)
    assert p["regions"] in ([], ["전국"]) and p["indicator"] == "고령인구 비율"


def test_merging_words_before_population_are_plain() -> None:
    assert qa.population_modifier("대구 경북 합쳐서 인구 몇 명?") is None
    assert qa.population_modifier("충남 시군 중 인구 많은 순서로 다섯 곳") is None
    assert qa.population_modifier("2020년 통학 인구") == "통학"


@pytest.mark.parametrize(
    "question",
    [
        "아내 평균 초혼 나이 높은 시도 다섯 곳",
        "전남 시군 중 인구 많은 순으로 네 곳",
        "재작년 출생아 많은 시군구 상위 다섯 곳",
    ],
)
def test_high_rank_overrides_ascending(question) -> None:
    p = qa.correct(question, parsed(type="rank", indicator="출생아 수", ascending=True), TODAY)
    assert p["ascending"] is False


@pytest.mark.parametrize(
    "question",
    [
        "지난해 인구 감소 폭 큰 시도 top5",
        "합계출산율 가장 낮은 시도 3곳",
    ],
)
def test_low_or_decrease_rank_keeps_ascending(question) -> None:
    p = qa.correct(question, parsed(type="rank", indicator="출생아 수", ascending=True), TODAY)
    assert p["ascending"] is True


def test_baby_words_are_not_an_age_for_birth_indicators(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n)
    q0 = "다문화 가정 아기 최근 5년 동안 어떻게 변했어?"
    p = qa.correct(q0, parsed(type="trend", indicator="다문화 출생아 수"), TODAY)
    assert not p.get("ages") and not p.get("age_group")
    q = qa.correct("서울 아이들 수 얼마나 돼?", parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert q["ages"] == [[0, 14]]


def test_counting_words_before_population_are_plain() -> None:
    assert qa.population_modifier("경기도에서 제일 큰 도시 3개 인구로") is None


def test_change_rank_question(monkeypatch) -> None:
    p = qa.correct("작년에 인구 제일 많이 줄어든 시도 세 군데",
                   parsed(type="rank", indicator="자연증가 건수"), TODAY)  # fmt: skip
    assert p["rank_by"] == "decrease" and p["indicator"] == "총인구" and p["type"] == "rank"
    q = qa.correct("인구 증가 폭이 가장 큰 구는?", parsed(type="rank", indicator="총인구"), TODAY)
    assert q["rank_by"] == "increase"
    asked_year = "출생아가 가장 많이 늘어난 해는?"
    y = qa.correct(asked_year, parsed(type="trend", indicator="출생아 수"), TODAY)
    assert "rank_by" not in y


def test_rank_by_change(monkeypatch) -> None:
    def fake(q):
        if q.get("rank_by"):
            return qa._rank_by_change(q, 2025)
        base = {"서울": 100, "부산": 50, "경기": 200} if q["year"] == 2024 else {
            "서울": 80, "부산": 45, "경기": 230}  # fmt: skip
        rows = [{"region": k, "value": v, "rank": 0} for k, v in base.items()]
        return {"status": "ok", "indicator": "총인구", "unit": "명", "ranking": rows,
                "level": "시도", "order": "높은 순", "year": q["year"]}  # fmt: skip

    monkeypatch.setattr(qa, "compute", fake)
    r = qa._rank_by_change({"type": "rank", "rank_by": "decrease", "year": 2025, "top": 2}, 2025)
    assert [(x["region"], x["value"]) for x in r["ranking"]] == [("서울", -20), ("부산", -5)]
    assert r["order"] == "감소 폭이 큰 순" and "2024년 → 2025년" in r["notes"][0]
    assert "가장 많이 줄어든" in qa._rank_text(r)


def test_causal_reading_without_causal_words_is_undone() -> None:
    p = qa.correct("작년 혼인 중에 남녀 모두 초혼인 비율 몇 %야?", parsed(type="causal"), TODAY)
    assert p["type"] != "causal"
    q = qa.correct("출산율이 낮은 이유가 뭐야?", parsed(type="causal"), TODAY)
    assert q["type"] == "causal"


def test_rank_by_other_axis_is_not_region_rank() -> None:
    by_nation = "등록외국인 국적별로 많은 순서 세 나라만 알려줘"
    p = qa.correct(by_nation, parsed(type="rank", top=3), TODAY)
    assert p["rank_axis"] == "국적" and p["grounded"] is False
    q = qa.correct("2024년 등록외국인이 가장 많은 시도 3곳", parsed(type="rank", top=3), TODAY)
    assert "rank_axis" not in q


def test_region_mean_is_mean_of_regions() -> None:
    p = qa.correct("2024년 시도 평균 출생아 수 알려줘", parsed(regions=[]), TODAY)
    assert p["region_mean"] and p["type"] == "rank" and p["level"] == "시도"
    q = qa.correct("2024년 부산 구별 평균 출생아 수", parsed(regions=["부산"]), TODAY)
    assert q["region_mean"] and q["level"] == "시군구" and q["sido"] == "부산"
    assert q["regions"] == []
    c = qa.correct("부산 출생아 수 지역 평균보다 많아?", parsed(regions=["부산"]), TODAY)
    assert not c.get("region_mean")


@pytest.mark.parametrize(
    "question, llm_sex, expected",
    [
        ("서울 인구 몇 명이야?", "남", "계"),
        ("서울 남녀 인구 비교", "남", "계"),
        ("서울 남자랑 여자 인구", "여", "계"),
        ("서울 여자 인구", "여", "여"),
        ("서울 여자 인구", "계", "여"),
        ("서울 경력단절여성이 있는 가구 수", "계", "계"),
        ("서울 경력단절 여성이 있는 가구 수", "여", "계"),
        ("서울 남성 가구주의 경력단절 여성이 있는 가구 수", "남", "남"),
        ("세종 미혼모 몇 명", "여", "여"),
        ("이혼하는 남편 평균 나이", "남", "남"),
        ("엄마 평균 나이", "여", "여"),
    ],
)
def test_sex_not_said(question: str, llm_sex: str, expected: str) -> None:
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"], sex=llm_sex), TODAY)
    assert p["sex"] == expected


@pytest.mark.parametrize(
    "question, llm_span, expected",
    [
        ("서울 인구 추이 보여줘", (2015, 2024), (None, None)),
        ("요새 서울 사람 수 감소 중이야?", (2015, 2024), (None, None)),
        ("2010년부터 서울 인구 추이", (2010, 2025), (2010, None)),
        ("2010년에서 2020년까지 서울 인구 추이", (2010, 2020), (2010, 2020)),
        ("서울 인구 10년간 추이", (2015, 2024), (2016, None)),
    ],
)
def test_trend_period_not_said(question: str, llm_span, expected) -> None:
    start, end = llm_span
    p = qa.correct(
        question,
        parsed(type="trend", indicator="총인구", regions=["서울"], start_year=start, end_year=end),
        TODAY,
    )
    assert (p.get("start_year"), p.get("end_year")) == expected


@pytest.mark.parametrize(
    "question, kind, split, expected_kind",
    [
        ("서울 남녀 인구 비교", "compare", True, "value"),
        ("서울 남자랑 여자 인구", "value", True, "value"),
        ("서울이랑 부산 남녀 인구", "compare", True, "compare"),
        ("서울 남녀 인구 추이", "trend", True, "trend"),
        ("서울 남녀 합계 인구", "value", None, "value"),
        ("서울 성별 구분 없이 인구", "value", None, "value"),
        ("서울 남녀 인구 구성", "composition", None, "composition"),
        ("서울 인구", "value", None, "value"),
    ],
)
def test_sex_split_asked(question: str, kind: str, split, expected_kind: str) -> None:
    regions = ["서울", "부산"] if "부산" in question else ["서울"]
    p = qa.correct(question, parsed(type=kind, indicator="총인구", regions=regions), TODAY)
    assert p.get("split_sex") is split
    assert p["type"] == expected_kind


def test_sex_split_computes_men_and_women(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qa, "_compute", lambda q: {"status": "ok", "indicator": "총인구", "sex": q["sex"]}
    )
    q = {
        "type": "value",
        "indicator": "총인구",
        "regions": ["서울"],
        "sex": "계",
        "split_sex": True,
    }
    r = qa.compute(q)
    assert [p["sex"] for p in r["split"]] == ["남", "여"]
    for name in ("성비", "세대당 인구", "합계출산율"):
        assert "split" not in qa.compute({**q, "indicator": name})


def test_sex_split_sentence_compares_sexes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "summary", lambda p: "")
    part = {"status": "ok", "indicator": "총인구", "unit": "명", "kind": "count"}
    r = {"split": [{**part, "sex": "남", "value": 100}, {**part, "sex": "여", "value": 130}]}
    assert qa._split_text(r).strip() == "여성이 30명 더 많습니다 (1.3배)."


@pytest.fixture
def data_end_2025(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))


def trend(**kw) -> dict:
    return parsed(type="trend", **kw)


@pytest.mark.parametrize(
    "question, llm, want",
    [
        ("최근 5년 인천 사망자 수 변화", (2020, 2025), (2021, None)),
        ("지난 3년간 인천 사망자 수 변화", (2022, 2024), (2023, None)),
        ("부산 조출생률 7년 동안 어떻게 됐어?", (2017, 2024), (2019, None)),
        ("8년 전보다 고령인구 비율 얼마나 올랐어?", (2018, 2026), (2017, None)),
        ("울산 인구 1995년부터 8년 동안 얼마나 늘었어?", (1995, None), (1995, 2003)),
        ("2016년부터 지금까지 서울 인구 추이", (2016, 2026), (2016, None)),
        ("2018년이랑 작년 출생아 수 얼마나 달라졌어?", (2018, 2024), (2018, 2025)),
    ],
)
def test_relative_period_by_rule(data_end_2025, question, llm, want) -> None:
    p = qa.correct(question, trend(start_year=llm[0], end_year=llm[1]), TODAY)
    assert (p["start_year"], p["end_year"]) == want


def test_years_later_is_counted_from_today(data_end_2025) -> None:
    q = "6년 뒤 대구 인구 몇 명이야?"
    p = qa.correct(q, parsed(indicator="총인구", regions=["대구"]), TODAY)
    assert p["year"] == 2032


def test_llm_year_without_number_is_dropped(data_end_2025) -> None:
    p = qa.correct("외국인 작년에 얼마나 늘었어?", trend(start_year=2024, end_year=2024), TODAY)
    assert (p["start_year"], p["end_year"]) == (None, None)


def test_year_arithmetic_reads_as_null() -> None:
    raw = (
        '{"type": "trend", "year": 2025, "start_year": 2025 - 8, "end_year": 2025,'
        ' "time": [{"expr": "8년 전"}]}'
    )
    objs = [o for o in qa._json_objects(qa.YEAR_ARITH.sub(": null", raw)) if "type" in o]
    assert objs and objs[0]["start_year"] is None and objs[0]["type"] == "trend"


def series(*years) -> list[dict]:
    return [{"year": y, "value": 1} for y in years]


NOTE_DAY = date(2026, 10, 24)


@pytest.mark.parametrize(
    "q, years, want",
    [
        (
            {"asked": {"recent": 5}},
            range(2021, 2026),
            "가장 최근 자료가 2025년까지라 2021~2025년(5개 연도)으로 계산했습니다",
        ),
        (
            {"asked": {"recent": 5}},
            range(2020, 2025),
            "가장 최근 자료가 2024년까지라 2020~2024년(5개 연도)으로 계산했습니다",
        ),
        (
            {"asked": {"recent": 5}},
            range(2023, 2026),
            "자료가 2023~2025년만 있어 3개 연도로 계산했습니다",
        ),
        (
            {"time_refs": [{"kind": "compare_ago"}]},
            range(2015, 2026),
            "가장 최근 자료가 2025년까지라 2015년과 2025년을 비교했습니다",
        ),
        (
            {"time_refs": [{"kind": "span", "n": 7}]},
            range(2019, 2026),
            "가장 최근 자료가 2025년까지라 2019~2025년(7개 연도)으로 계산했습니다",
        ),
        (
            {"time_refs": [{"kind": "until_now"}]},
            range(2015, 2026),
            "가장 최근 자료가 2025년까지라 2015~2025년으로 계산했습니다",
        ),
        ({"asked": {"recent": 5}}, range(2022, 2027), None),
        ({"time_refs": [{"kind": "span_from"}]}, range(1995, 2004), None),
        ({"time_refs": [{"kind": "word"}]}, range(2020, 2026), None),
        ({}, range(2020, 2024), None),
    ],
)
def test_data_end_note(q, years, want) -> None:
    result = {"status": "ok", "series": series(*years), "notes": []}
    qa._add_data_end_note(q, result, NOTE_DAY)
    assert result["notes"] == ([want] if want else [])


def test_n_years_end_at_this_year_when_it_has_data(monkeypatch) -> None:
    calls = []

    def fake_trend(ind, reg, a, b, *rest):
        calls.append((a, b))
        return {"status": "ok", "series": [{"year": y, "value": 1} for y in range(a, b + 1)]}

    monkeypatch.setattr(indicators, "trend", fake_trend)
    monkeypatch.setattr(qa, "_with_this_year", lambda ind, region, sex, ages, end: 2026)
    span = {"type": "trend", "start_year": 2019, "time_refs": [{"kind": "span", "n": 7}]}
    qa._compute_population(span, "총인구", "trend", ["대구광역시"], 2025, "계", None, 2025)
    recent = {"type": "trend", "asked": {"recent": 5}}
    qa._compute_population(recent, "총인구", "trend", ["대구광역시"], 2025, "계", None, 2025)
    these_days = {"type": "trend", "asked": {"these_days": True, "latest": True}}
    qa._compute_population(these_days, "총인구", "trend", ["대구광역시"], 2025, "계", None, 2025)
    assert calls == [(2020, 2026), (2022, 2026), (2021, 2026)]


def test_n_years_end_at_data_end_without_this_year(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    monkeypatch.setattr(qa, "_with_this_year", lambda ind, region, sex, ages, end: end)
    span = {"type": "trend", "start_year": 2019, "time_refs": [{"kind": "span", "n": 7}]}
    qa._compute_population(span, "총인구", "trend", ["대구광역시"], 2025, "계", None, 2025)
    assert calls == [(2019, 2025)]


def test_guessed_year_of_period_trend_is_dropped(data_end_2025) -> None:
    p = qa.correct("대구 출생아 수 7년 동안 어떻게 변했어?", trend(year=2022), TODAY)
    assert (p["start_year"], p.get("end_year"), p["year"]) == (2019, None, None)


@pytest.mark.parametrize(
    "question, llm, want",
    [
        ("2019년이랑 2023년 인천 사망자 수 비교해줘", (2019, 2022), (2019, 2023)),
        ("2023년과 2019년 인천 사망자 수 얼마나 달라?", (2023, 2019), (2019, 2023)),
    ],
)
def test_two_years_with_any_particle(data_end_2025, question, llm, want) -> None:
    p = qa.correct(question, trend(start_year=llm[0], end_year=llm[1]), TODAY)
    assert (p["start_year"], p["end_year"]) == want


def test_year_not_in_question_is_dropped(data_end_2025) -> None:
    p = qa.correct("2019년 이후 인천 사망자 수 추이", trend(start_year=2019, end_year=2023), TODAY)
    assert (p["start_year"], p.get("end_year")) == (2019, None)


def test_year_said_in_words_is_kept(data_end_2025) -> None:
    p = qa.correct("코로나 첫 해 인천 사망자 수", parsed(indicator="사망자 수", year=2020), TODAY)
    assert p["year"] == 2020


def test_n_years_fall_back_when_this_year_value_is_missing(monkeypatch) -> None:
    calls = []

    def fake_trend(ind, reg, a, b, *rest):
        calls.append((a, b))
        years = range(a, min(b, 2025) + 1)
        return {"status": "ok", "series": [{"year": y, "value": 1} for y in years]}

    monkeypatch.setattr(indicators, "trend", fake_trend)
    monkeypatch.setattr(qa, "_with_this_year", lambda ind, region, sex, ages, end: 2026)
    recent = {"type": "trend", "asked": {"recent": 5}}
    qa._compute_population(recent, "세대당 인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2022, 2026), (2021, 2025)]


@pytest.mark.parametrize(
    "question",
    [
        "수영구가 연제구보다 사람이 많아?",
        "대구에서 사람 제일 많이 사는 구 다섯 곳",
        "춘천시 주민 몇 명이야?",
        "작년 인천 사람 얼마나 돼?",
    ],
)
def test_plain_people_question_is_population(question: str) -> None:
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["indicator"] == "총인구" and p["grounded"] is True


@pytest.mark.parametrize(
    "question",
    [
        "버스 타고 일하러 가는 사람 몇 명이야?",
        "대전에서 반려동물 키우는 사람 몇 명이야?",
        "울산에 귀촌한 사람 얼마나 돼?",
        "창원에서 혼자 사는 사람 몇 명이야?",
    ],
)
def test_people_of_another_statistic_is_not_grounded(question: str) -> None:
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["grounded"] is False


@pytest.mark.parametrize("kind", ["value", "compare", "rank", "composition", "causal", "other"])
def test_guessed_year_is_dropped_for_every_type(kind: str) -> None:
    p = qa.correct("버스로 일터에 가는 사람 수", parsed(type=kind, year=2025), TODAY)
    assert p["year"] is None


@pytest.mark.parametrize("question", ["2019년 서울 인구 구성", "코로나 첫 해 서울 인구 구성"])
def test_said_year_is_kept_for_composition(question: str) -> None:
    year = 2019 if "2019" in question else 2020
    p = qa.correct(question, parsed(type="composition", indicator="총인구", year=year), TODAY)
    assert p["year"] == year


@pytest.mark.parametrize(
    "given, want",
    [
        ({"year": 0}, {"year": None}),
        ({"year": ""}, {"year": None}),
        ({"start_year": 0, "end_year": 0}, {"start_year": None, "end_year": None}),
        ({"year": "2019"}, {"year": 2019}),
        ({"sex": ""}, {"sex": "계"}),
        ({"year": 2019, "sex": "여"}, {"year": 2019, "sex": "여"}),
    ],
)
def test_blank_values_are_cleaned(given: dict, want: dict) -> None:
    p = parsed(**given)
    qa._blank_to_none(p, lambda key, value, reason: p.__setitem__(key, value))
    assert {k: p[k] for k in want} == want


def test_trend_end_note_only_when_end_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "_lookup_span", lambda sp, region, sex: (2015, 2024))
    monkeypatch.setattr(qa.lookup, "trend", lambda *a: {"status": "ok", "notes": []})
    sp = {"name": "외국인주민 수", "sources": []}
    q = {"type": "trend", "indicator": "외국인주민 수", "regions": ["서울"]}
    assert qa._lookup_trend(q, sp, "서울", 2025, "계", None)["notes"] == []
    asked = qa._lookup_trend({**q, "end_year": 2025}, sp, "서울", 2025, "계", None)
    assert asked["notes"] == ["외국인주민 수 자료가 2024년까지 있어 2024년까지 봤습니다"]


@pytest.mark.parametrize(
    "q, leaf, expected",
    [
        (
            {"type": "value"},
            {"year": 2024},
            "가장 최근 자료인 2024년 기준으로 답했습니다",
        ),
        (
            {"type": "value"},
            {"year": 2026, "as_of": "2026년 10월 말"},
            "가장 최근 자료인 2026년 10월 말 기준으로 답했습니다",
        ),
        ({"type": "value", "year": 2020}, {"year": 2020}, None),
        (
            {"type": "trend", "start_year": 2010},
            {"indicator": "외국인주민 수", "series": [{"year": 2015}, {"year": 2024}]},
            "외국인주민 수 자료는 2024년이 가장 최근입니다",
        ),
        (
            {"type": "trend", "end_year": 2020},
            {"indicator": "외국인주민 수", "series": [{"year": 2015}, {"year": 2020}]},
            None,
        ),
    ],
)
def test_latest_note(q: dict, leaf: dict, expected: str | None) -> None:
    result = {"status": "ok", **leaf}
    qa._add_latest_note(q, result)
    assert result.get("notes", []) == ([expected] if expected else [])


def test_latest_note_once_for_split() -> None:
    part = {"status": "ok", "year": 2025, "notes": []}
    result = {"status": "ok", "split": [dict(part), dict(part, notes=[])]}
    qa._add_latest_note({"type": "value"}, result)
    qa._add_latest_note({"type": "value"}, result)
    assert all(len(p["notes"]) == 1 for p in result["split"])


def test_no_year_uses_latest_for_computed_indicator() -> None:
    p = qa.correct(
        "서울 인구 몇 명이야", parsed(indicator="총인구", regions=["서울"], year=2025), TODAY
    )
    assert p.get("year") is None
    assert (p.get("asked") or {}).get("latest") is True
    p = qa.correct(
        "2025년 서울 인구", parsed(indicator="총인구", regions=["서울"], year=2025), TODAY
    )
    assert not (p.get("asked") or {}).get("latest")


def test_rank_falls_back_to_annual_when_month_drops_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monthly = {
        "status": "ok",
        "as_of": "2026년 10월 말",
        "year": 2026,
        "total_regions": 16,
        "ranking": [{"region": "서울특별시", "value": 1}],
    }
    annual = {
        "status": "ok",
        "year": 2025,
        "total_regions": 17,
        "ranking": [{"region": "서울특별시", "value": 1}],
    }
    monkeypatch.setattr(qa, "_compute", lambda q: monthly if q["asked"]["latest"] else annual)
    r = qa.compute({"type": "rank", "indicator": "총인구", "asked": {"latest": True}})
    assert r["year"] == 2025
    assert r["notes"] == [
        "가장 최근 연간 자료인 2025년 기준으로 답했습니다. "
        "최신 달(2026년 10월 말) 자료는 행정구역 개편으로 일부 지역이 달라 쓰지 않았습니다"
    ]


def test_empty_month_falls_back_to_annual(monkeypatch: pytest.MonkeyPatch) -> None:
    monthly = {"status": "ok", "as_of": "2026년 10월 말", "year": 2026, "value": None}
    annual = {"status": "ok", "year": 2025, "value": 2.07}
    monkeypatch.setattr(qa, "_compute", lambda q: monthly if q["asked"]["latest"] else annual)
    r = qa.compute({"type": "value", "indicator": "세대당 인구", "asked": {"latest": True}})
    assert r["value"] == 2.07


def test_indicators_aligned_to_common_year_and_summed(monkeypatch: pytest.MonkeyPatch) -> None:
    asked_years = []

    def fake(q):
        asked_years.append((q["indicator"], q.get("year")))
        year = q.get("year")
        if q["indicator"] == "총인구":
            if year:
                return {
                    "status": "ok",
                    "indicator": "총인구",
                    "unit": "명",
                    "kind": "count",
                    "region": "춘천시",
                    "year": year,
                    "value": 285234,
                }
            return {
                "status": "ok",
                "indicator": "총인구",
                "unit": "명",
                "kind": "count",
                "region": "춘천시",
                "year": 2026,
                "as_of": "2026년 10월 말",
                "value": 284762,
            }
        return {"status": "ok", "indicator": "등록외국인 수", "unit": "명", "kind": "count",
                "region": "춘천시", "year": 2025, "value": 5637}  # fmt: skip

    monkeypatch.setattr(qa, "compute", fake)
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    q = {"type": "value", "indicators": ["총인구", "등록외국인 수"], "aggregate": "sum",
         "regions": ["춘천"]}  # fmt: skip
    r = qa._compute(q)
    assert ("총인구", 2025) in asked_years
    assert r["value"] == 290871 and r["year"] == 2025
    assert [m["value"] for m in r["members"]] == [285234, 5637]


def _count(name: str, value: int, year: int, as_of: str | None = None) -> dict:
    out = {"status": "ok", "indicator": name, "unit": "명", "kind": "count", "region": "가시"}
    return {**out, "year": year, "value": value, **({"as_of": as_of} if as_of else {})}


def test_sum_needs_every_asked_indicator(monkeypatch: pytest.MonkeyPatch) -> None:

    def fake(q):
        if q["indicator"] == "총인구":
            return {"status": "missing_data", "reason": "자료 없음"}
        return _count("등록외국인 수", 7, 2025)

    monkeypatch.setattr(qa, "compute", fake)
    q = {"type": "value", "indicators": ["총인구", "등록외국인 수"], "aggregate": "sum",
         "regions": ["가시"]}  # fmt: skip
    r = qa._compute(q)
    assert "members" not in r and " + " not in (r.get("indicator") or "")
    notes = [n for leaf in qa._leaves(r) for n in leaf.get("notes", [])]
    assert any("총인구" in n and "뺐습니다" in n for n in notes)


def test_common_year_keeps_values_of_the_same_month() -> None:
    month = "2026년 10월 말"
    same = [_count("총인구", 10, 2026, month), _count("고령인구", 2, 2026, month)]
    assert qa._common_year(same) is None
    assert qa._common_year([_count("가", 1, 2025), _count("나", 2, 2025)]) is None
    assert qa._common_year([_count("가", 1, 2025), _count("나", 2, 2024)]) == 2024


def test_common_year_for_a_month_value_and_an_annual_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    mixed = [_count("총인구", 10, 2026, "2026년 10월 말"), _count("등록외국인 수", 2, 2025)]
    assert qa._common_year(mixed) == 2025
    months = [_count("가", 1, 2026, "2026년 5월 말"), _count("나", 2, 2026, "2026년 10월 말")]
    assert qa._common_year(months) == 2025


def test_default_value_next_to_alt_is_of_the_same_year(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fake(q):
        asked = q.get("asked") or {}
        if asked.get("survey"):
            return {"status": "ok", "value": 55.2, "unit": "세", "year": 2025, "notes": []}
        seen.append((asked.get("latest"), q.get("year")))
        return {"status": "ok", "value": 56.8, "unit": "세", "year": 2025}

    monkeypatch.setattr(qa, "_compute", fake)
    q = {"type": "value", "indicator": "중위연령"}
    qa.compute({**q, "asked": {"survey": "인구총조사", "latest": True}})
    assert seen == [(None, 2025)]


def test_sum_indicators_refuses_mixed_periods_or_rates() -> None:
    one = {"status": "ok", "unit": "명", "kind": "count", "region": "춘천시", "value": 1}
    assert qa._sum_indicators([{**one, "year": 2025}, {**one, "year": 2024}], {}) is None
    assert qa._sum_indicators([{**one, "year": 2025}, {**one, "year": 2025, "unit": "%",
                                                       "kind": "rate"}], {}) is None  # fmt: skip


def test_two_word_time_refs_become_start_and_end(monkeypatch) -> None:
    # "작년이랑 재작년": 낱말 시점 둘을 다 풀어 놓고도 쓰지 않아 HCX의 year 하나만 남았다 (11/24)
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    q = "작년이랑 재작년 혼인 건수 비교해 줘"
    p = qa.correct(q, parsed(type="trend", indicator="총인구", year=2024), TODAY)
    assert (p["start_year"], p["end_year"]) == (2024, 2025)
    p = qa.correct(q, parsed(type="value", indicator="총인구", year=2024), TODAY)
    assert p["type"] == "trend" and (p["start_year"], p["end_year"]) == (2024, 2025)
    # 변화 낱말 없이 "비교하면"만 있어도 '작년에' 한 시점 규칙에 걸려 값으로 바뀌지 않는다
    q = "작년에 혼인 많이 했어? 재작년이랑 비교하면?"
    p = qa.correct(q, parsed(type="trend", indicator="총인구", year=2024), TODAY)
    assert p["type"] == "trend" and (p["start_year"], p["end_year"]) == (2024, 2025)


def test_month_range_in_question_becomes_months(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    p = qa.correct(
        "작년 4월부터 6월까지 부산 이혼 몇 건", parsed(indicator="이혼 건수", year=2025), TODAY
    )
    assert p["months"] == [4, 6]
    p = qa.correct("작년 부산 이혼 몇 건", parsed(indicator="이혼 건수", year=2025), TODAY)
    assert "months" not in p


def test_lookup_span_skips_sources_without_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qa.lookup, "_years", lambda s: (None, None) if s["table"] == "X" else (1990, 2010)
    )
    monkeypatch.setattr(qa.indicators, "resolve_region", lambda r: {"status": "ok", "kind": "시도"})
    sp = {"sources": [{"table": "X", "level": "시도"}, {"table": "Y", "level": "시도"}]}
    assert qa._lookup_span(sp, "서울", "계") == (1990, 2010)
    assert qa._lookup_span({"sources": [sp["sources"][0]]}, "서울", "계") == (0, 9999)


def test_lookup_pick_skips_sources_without_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa.lookup, "_years", lambda s: (None, None))
    spec = {"name": "인구", "sources": [{"table": "X", "level": "시도"}]}
    src, err = qa.lookup._pick(spec, "시도", 1990, 2007, "계")
    assert src is None and err["status"] == "missing_data"


@pytest.mark.parametrize(
    ("question", "llm", "expected"),
    [
        ("화성시는 작년에 이사 들어온 사람이 나간 사람보다 많아?", "순이동", "순이동"),
        (
            "서초구랑 해남군 엄마들 평균 출산 나이 몇 살 차이야?",
            "모의 평균 출산연령",
            "모의 평균 출산연령",
        ),
        (
            "첫째 아이 낳는 엄마 나이가 제일 어린 시도 3곳은?",
            "모의 평균 출산연령",
            "첫째아 평균 출산연령",
        ),
        ("작년에 이사 들어온 사람 몇 명이야?", "총인구", "전입자 수"),
        ("경북 구미시 작년에 이사 나간 사람이 들어온 사람보다 많아?", "순이동", "순이동"),
        ("작년에 서울에서 이사 나간 사람들 제일 많이 간 시도 3곳", "순이동", "전출자 수"),
    ],
)
def test_hcx_indicator_kept_with_real_aliases(
    monkeypatch, question: str, llm, expected: str
) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["indicator"] == expected


def test_single_relative_year_read_as_trend_becomes_value() -> None:
    q = "화성시는 작년에 이사 들어온 사람이 나간 사람보다 많아?"
    h = parsed(type="trend", indicator="순이동", regions=["화성시"], year=2024)
    h.update(start_year=2023, end_year=2024)
    p = qa.correct(q, h, TODAY)
    assert p["type"] == "value" and p["year"] == TODAY.year - 1
    p = qa.correct(
        "작년 서울 인구 추이", parsed(type="trend", indicator="총인구", year=2024), TODAY
    )
    assert p["type"] == "trend"


@pytest.mark.parametrize(
    ("question", "said"),
    [
        ("해운대구는 일할 나이 100명이 노인 몇 명 부양해?", False),
        ("김해 사는 등록외국인 몇 명이야?", False),
        ("요즘 사람들 왜 결혼을 안 해?", False),
        ("여자 아기 100명 태어날 때 남자 아기는 몇 명 태어나?", False),
        ("세종시에 미성년 자녀가 딱 셋인 집 몇 가구야?", False),
        ("코로나 첫 해 출생아 수", True),
        ("그해 서울 인구", True),
        ("월드컵 때 태어난 아기 수", True),
        ("이천이십이년 서울 인구", True),
    ],
)
def test_year_words_skip_verb_endings(question: str, said: bool) -> None:
    assert qa._says_year(question) is said


def test_both_sexes_people_question_is_population(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    q = "경기도는 남자가 많아 여자가 많아? 얼마나 차이 나?"
    p = qa.correct(q, parsed(type="compare", indicator="성비", regions=["경기도"]), TODAY)
    assert p["indicator"] == "총인구" and p.get("split_sex") and p["grounded"] is True


def test_student_age_band_is_population() -> None:
    q = "강원도 초등학생 나이(6~11살) 애들 최근 5년 동안 얼마나 줄었어?"
    p = qa.correct(q, parsed(type="trend", indicator="초등학생 인구", regions=["강원도"]), TODAY)
    assert p["indicator"] == "총인구" and p["ages"] == [[6, 11]]


def test_age_of_unlisted_measure_stays_with_it() -> None:
    q = "2020년 서울 출생아 수랑 고령자 가구 비율 알려줘"
    p = qa.correct(q, parsed(indicator="출생아 수", regions=["서울"], year=2020), TODAY)
    assert p["also_asked"] == "고령자 가구 비율"
    assert not p.get("ages") and not p.get("age_group")
    assert p["also_asked_ages"]["ages"] == [[65, None]]
    q = "2020년 서울 65세 이상 인구와 1인 가구 수"
    p = qa.correct(q, parsed(indicator="총인구", regions=["서울"], year=2020), TODAY)
    assert p["ages"] == [[65, None]] and not p.get("also_asked_ages")
    assert qa.measure_question(q, "1인 가구 수") == "2020년 서울 1인 가구 수"


def test_age_note_follows_the_unlisted_measure() -> None:
    question = "2020년 서울 출생아 수랑 20대 후반 1인 가구 수 알려줘"
    p = qa.correct(question, parsed(indicator="출생아 수", regions=["서울"], year=2020), TODAY)
    assert p["also_asked"] == "20대 후반 1인 가구 수"
    assert not p.get("ages") and not p.get("age_note")
    other = p["also_asked_ages"]
    assert other["ages"] == [[27, 29]] and "27~29세" in other["age_note"]


def test_one_region_two_years_compare_is_a_trend(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    monkeypatch.setattr(indicators, "compare", lambda *a: pytest.fail("지역 비교로 계산함"))
    q = {"type": "compare", "asked": {}, "start_year": 2010, "end_year": 2025}
    qa._compute_population(q, "총인구", "compare", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2010, 2025)]


def test_decade_part_age_is_explained() -> None:
    note = "'20대 후반'은 27~29세로 계산했습니다 (초반 0~3세 · 중반 4~6세 · 후반 7~9세)"
    assert qa.decade_part_note("서울 20대 후반 여자 인구") == note
    assert qa.decade_part_note("서울 20대 여자 인구") is None


def test_one_region_two_years_compare_reads_as_trend() -> None:
    asked = parsed(type="compare", indicator="총인구", regions=["서울"], year=2025)
    p = qa.correct(
        "2010년과 2025년 서울 인구수 비교해줘",
        {**asked, "start_year": 2010, "end_year": 2025},
        TODAY,
    )
    assert p["type"] == "trend" and (p["start_year"], p["end_year"]) == (2010, 2025)
    two = parsed(type="compare", indicator="총인구", regions=["서울", "부산"], year=2025)
    p = qa.correct("2025년 서울과 부산 인구 비교해줘", two, TODAY)
    assert p["type"] == "compare"


@pytest.mark.parametrize(
    "question",
    [
        "외국인은 몇 명인가요?",
        "경기도에 외국인 얼마나 살아?",
        "외국인 제일 많이 사는 데 세 곳 어디야?",
    ],
)
def test_bare_foreigner_is_foreign_resident(monkeypatch, question: str) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    p = qa.correct(question, parsed(type="value", indicator=""), TODAY)
    assert p["indicator"] == "외국인주민 수" and p["grounded"] is True


@pytest.mark.parametrize(
    "question",
    [
        "2010년 인구조사 때 서울이랑 경기도 중에 외국인 어디가 더 많이 살았어?",
        "제주에 등록된 외국인 몇 명이야?",
        "불법체류 외국인 중에 태국 사람 몇 명이야?",
    ],
)
def test_other_foreigner_questions_are_not_foreign_residents(monkeypatch, question: str) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    p = qa.correct(question, parsed(type="value", indicator=""), TODAY)
    assert p["indicator"] != "외국인주민 수"


def test_children_before_elders_is_aging_index(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    q = "의성군은 애들보다 노인이 몇 배나 많아?"
    p = qa.correct(q, parsed(type="compare", indicator="노령화지수", regions=["의성군"]), TODAY)
    assert p["indicator"] == "노령화지수" and p["grounded"] is True


def test_month_range_compares_regions_with_monthly_sources(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    q = "작년 상반기에 서울이랑 부산 중에 결혼 어디가 더 많이 했어?"
    p = qa.correct(
        q, parsed(type="compare", indicator="혼인 건수", regions=["서울", "부산"]), TODAY
    )
    r = qa.compute(p)
    assert r["status"] == "ok" and all(v["value"] for v in r["values"])
    assert "1~6월" in qa.summary(r)


def test_this_year_months_use_monthly_coverage(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    q = "올해 3월 서울이랑 부산 출생아 비교"
    p = qa.correct(
        q, parsed(type="compare", indicator="출생아 수", regions=["서울", "부산"]), TODAY
    )
    p["year"] = 2026
    r = qa.compute(p)
    assert r["status"] == "ok" and "2026년 3월" in qa.summary(r)


@pytest.mark.parametrize(
    ("question", "llm_type", "months_each", "want"),
    [
        ("작년 1월이랑 7월 서울이랑 부산 출생아 비교해줘", "compare", [[1, 1], [7, 7]], "compare"),
        ("작년 1월부터 3월까지 서울이랑 부산 출생아 몇 명이야?", "value", [[1, 3]], "compare"),
        ("작년 3월 서울이랑 부산 출생아 몇 명이야?", "value", [[3, 3]], "compare"),
    ],
)
def test_months_with_many_regions_compare_every_region(
    monkeypatch, question, llm_type, months_each, want
) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    p = qa.correct(
        question,
        parsed(type=llm_type, indicator="출생아 수", regions=["서울", "부산"], year=2025),
        TODAY,
    )
    assert p["months_each"] == months_each and p["type"] == want and not p.get("months")
    text = qa.summary(qa.compute(p))
    assert "서울특별시" in text and "부산광역시" in text


def test_month_sum_answer_says_the_months(monkeypatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", REAL_ALIASES)
    q = "작년 1월부터 3월까지 돌아가신 분 다 합치면 몇 명이야?"
    p = qa.correct(q, parsed(type="value", indicator="사망자 수", year=2025), TODAY)
    assert "1~3월" in qa.summary(qa.compute(p)).split("\n")[0]
