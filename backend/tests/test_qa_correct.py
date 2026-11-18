from datetime import date

import pytest

from app.services import indicators, qa

# config/catalog_notes.toml 지표 사전 일부 (DB 없이 테스트)
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


@pytest.fixture(autouse=True)
def fixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", lambda: ALIASES)


def parsed(**kw) -> dict:
    base = {"type": "value", "indicator": None, "regions": [], "year": None, "sex": "계"}
    return {**base, **kw}


def test_indicator_alias_overrides_llm() -> None:
    # HCX가 "고령화율"을 비슷한 이름의 노령화지수로 해석한 경우
    p = qa.correct(
        "최근 5년간 부산 고령화율 변화는?",
        parsed(type="trend", indicator="노령화지수", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert "corrections" in p


def test_indicator_phrase_with_change_word() -> None:
    # "인구 증감률"을 통째로 지표 이름으로 해석한 경우
    p = qa.correct(
        "2015~2025년 대구 인구 증감률은?",
        parsed(type="trend", indicator="인구 증감률", regions=["대구"]),
        TODAY,
    )
    assert p["indicator"] == "총인구"


def test_generic_population_word_keeps_llm_indicator() -> None:
    # "인구"만 별칭으로 잡혀도, HCX가 고른 다른 지원 지표를 총인구로 바꾸지 않는다
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
    ],
)
def test_composition_by_sex(question: str, by: str | None) -> None:
    # HCX가 by를 비워도 남녀를 묻는 구성비 질문은 성별 구성으로
    p = qa.correct(question, parsed(type="composition", regions=["서울"]), TODAY)
    assert p.get("by") == by


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
    # "유소년인구 비율" 안의 "인구"는 총인구로 세지 않는다
    assert {n for *_, n in indicators.find_in_text("세종 유소년인구 비율 비교")} == {
        "유소년인구 비율"
    }
    # 조회 지표 별칭 안의 "인구"도 마찬가지
    assert {n for *_, n in indicators.find_in_text("서울 인구 순유입은?")} == {"순이동"}


def test_unsupported_lookup_indicator_is_named() -> None:
    # 조회 지표는 아직 답하지 못하지만, 엉뚱한 계산 지표로 바뀌지 않고 그 이름으로 안내된다
    p = qa.correct("2024년 서울 출산율은?", parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["indicator"] == "합계출산율"


@pytest.mark.parametrize(
    "question",
    [
        "서울 고령화율과 노령화지수를 비교해줘",  # 지표가 둘이면 HCX 해석을 따른다
        "저출산 정책이 효과가 있었나요?",  # 지표 표현이 없으면 HCX 해석을 따른다
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
    # 추이 질문의 기간은 건드리지 않는다
    p = qa.correct("작년까지 5년간 변화", parsed(type="trend", indicator="총인구"), TODAY)
    assert p["year"] is None


def test_age_band_population_count() -> None:
    # 연령 구간 인구 수는 총인구 + 연령 구간 (HCX가 비슷한 비율 지표로 바꿔도 되돌린다)
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
    # "몇 명"을 물었는데 비율 지표로 답하지 않는다 (0회차 s08 같은 바꿔치기)
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
    # "구별 20대 인구"는 구성비가 아니라 서울 안 구별 목록
    p = qa.correct(
        "2023년 서울 지역의 구별 20대(20~29세) 인구는 몇 명인가요?",
        parsed(type="composition", indicator="총인구", regions=["서울"]),
        TODAY,
    )
    assert (p["type"], p["level"], p["sido"], p["regions"]) == ("rank", "시군구", "서울", [])


def test_more_specific_indicator_wins() -> None:
    # 별칭이 여럿 잡히면 '인구' 말고 다른 쪽, 비율을 물었으면 비율 지표
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
    # 사유를 묻는 구성비 질문은 원인 질문이 아니다
    q = "부산으로 이사 온 사람 중 일자리 때문에 온 비율은?"
    p = qa.correct(q, parsed(type="composition"), TODAY)
    assert p["type"] != "causal"


def test_round2_misreads() -> None:
    # HCX가 실행마다 다르게 해석한 경우 (2회차): 원문에서 바로잡는다
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
    # HCX가 "광주광역시"를 "광주"로 줄이면 경기 광주시와 겹쳐 되묻게 된다
    q = "서울 광주광역시 2018년부터 2024년까지 인구 추이"
    p = qa.correct(q, parsed(type="trend", indicator="총인구", regions=["서울", "광주"]), TODAY)
    assert p["regions"] == ["서울", "광주광역시"]
    # 질문에 "광주"라고만 썼으면 그대로 둔다 (어느 광주인지 되묻는 게 맞다)
    p = qa.correct("광주 인구 추이", parsed(type="trend", regions=["광주"]), TODAY)
    assert p["regions"] == ["광주"]


def test_latest_without_change_is_latest_value() -> None:
    # HCX가 "최근"을 최근 몇 년 추이로 읽어도, 변화를 묻지 않았으면 최신 값 하나
    def hcx() -> dict:
        return parsed(
            type="trend", indicator="고령인구 비율", regions=["부산"], year=2025,
            start_year=2020, end_year=2025,
        )  # fmt: skip

    p = qa.correct("최근 부산 고령인구 비율", hcx(), TODAY)
    assert (p["type"], p.get("start_year"), p.get("end_year")) == ("value", None, None)
    assert p["asked"]["latest"] and p.get("year") is None  # 연도는 표마다 마지막 해
    p = qa.correct(
        "최근 서울 출생아 수", parsed(indicator="출생아 수", regions=["서울"], year=2024), TODAY
    )
    assert p.get("year") is None
    for q in ["최근 부산 고령인구 비율 추이", "요즘 부산 고령인구 비율이 늘었어?",
              "최근 5년 부산 고령인구 비율"]:  # fmt: skip
        assert qa.correct(q, hcx(), TODAY)["type"] == "trend", q
    # 최근 추이: HCX가 넣은 기간을 지워 자료 마지막 해까지 본다 ("최근 5년"은 따로 N개년)
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
    # HCX가 질문에 없는 구 목록을 지어내도 순위 범위는 질문의 시도
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
    # "전국 ~ 중 서울 비중"을 HCX가 전국·서울 비교로 읽은 경우 (골든셋 s44, 10회차)
    p = qa.correct(
        "2025년 전국 출생아 중 서울 아이들 비중은 얼마예요?",
        parsed(type="compare", indicator="출생아 수", regions=["전국", "서울"], year=2025),
        TODAY,
    )
    assert p["type"] == "value"
    assert p["regions"] == ["서울"]
    assert p["share_of"] == "전국"


def test_compare_two_regions_with_nation_stays_compare() -> None:
    # 전국 말고 지역이 둘이면 비중이 아니라 비교로 둔다
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
    # "과거부터 지금까지" → 되묻지 않고 수록 기간 전체 추이 (HCX가 넣은 기간은 지운다)
    p = qa.correct(
        "과거부터 지금까지 전국 합계출산율 추이",
        parsed(type="value", indicator="합계출산율", regions=["전국"], start_year=2015),
        TODAY,
    )
    assert p["type"] == "trend" and p["asked"]["from_start"] is True
    assert p.get("start_year") is None


def test_from_past_keeps_end_year_in_question() -> None:
    # "과거부터 2010년까지" → 시작만 수록 첫해, 끝은 질문의 2010년
    p = qa.correct(
        "과거부터 2010년까지 전국 합계출산율 추이",
        parsed(type="trend", indicator="합계출산율", regions=["전국"], start_year=1970),
        TODAY,
    )
    assert p["asked"]["from_start"] is True
    assert p.get("start_year") is None and p["end_year"] == 2010


def test_from_past_drops_guessed_end_year() -> None:
    # 질문에 끝 연도가 없으면 HCX가 넣은 끝 연도를 지우고 최신까지
    p = qa.correct(
        "과거부터 지금까지 전국 합계출산율 추이",
        parsed(type="trend", indicator="합계출산율", regions=["전국"], end_year=2020),
        TODAY,
    )
    assert p.get("end_year") is None


def test_from_past_computed_indicator_starts_at_first_year(monkeypatch) -> None:
    # 계산 지표(총인구 등)도 "과거부터"면 주민등록인구 수록 첫해부터 (전에는 최근 6년만)
    calls = []
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    q = {"type": "trend", "asked": {"from_start": True}, "start_year": None, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    q = {"type": "trend", "asked": {}, "start_year": None, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2008, 2025), (2020, 2025)]


def test_recent_n_years_computed_indicator(monkeypatch) -> None:
    # 계산 지표도 "최근 5년"은 마지막 연도 기준 5개년 (전에는 기본값 6개년, HCX 기간을 그대로 씀)
    calls = []
    monkeypatch.setattr(indicators, "trend", lambda ind, reg, a, b, *rest: calls.append((a, b)))
    q = {"type": "trend", "asked": {"recent": 5}, "start_year": 2019, "end_year": None}
    qa._compute_population(q, "총인구", "trend", ["서울특별시"], 2025, "계", None, 2025)
    assert calls == [(2021, 2025)]


@pytest.mark.parametrize(
    ("question", "llm", "grounded"),
    [
        ("최근 5년 부산 고령화율", "고령인구 비율", True),  # 별칭이 질문에 있다
        ("서울 1인 가구 중 여성 비율", "총인구", False),  # HCX가 비슷한 지표를 고름
    ],
)
def test_grounded_marks_indicator_named_in_question(question, llm, grounded) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["grounded"] is grounded


def test_grounded_is_none_without_indicator() -> None:
    # 원인 질문처럼 지표가 없으면 근거를 따지지 않는다 (바로 계산을 시도하지 않음)
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
    # 공백을 빼면 "등록외국인수" 안에 별칭 "외국인수"가 있지만 외국인주민 수가 아니다
    assert indicators.find_in_text("2023년 경기 등록외국인 수") == []
    assert [n for *_, n in indicators.find_in_text("2023년 경기 외국인 수")] == ["외국인주민 수"]


@pytest.mark.usefixtures("foreign_aliases")
@pytest.mark.parametrize(
    "question",
    ["2023년 경기 등록외국인 수", "서울 체류외국인 수는?", "2023년 경기 등록외국인 비율"],
)
def test_other_foreigner_statistics_are_unsupported(question: str) -> None:
    # HCX가 비슷한 이름의 외국인주민 지표를 골라도 답하지 않고 표 안내로 넘긴다
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
    # "세" 대신 "살", "미만" 대신 "안 된"
    assert qa.extract_ages("서울청년 19~34살 몇명임?") == [[19, 34]]
    assert qa.extract_ages("울산에 100살 이상 어르신") == [[100, None]]
    assert qa.extract_ages("15살 안 된 애들") == [[0, 14]]


@pytest.mark.parametrize(
    ("question", "ages"),
    [
        # 개발셋 D-139, D-140: 말로 이은 구간을 읽지 못해 연령 없이 답했다
        ("해운대구에서 작년에 돌아가신 분 중 62살에서 67살 사이 몇 명이야?", [[62, 67]]),
        ("등록 외국인 중에 33살부터 38살까지 몇 명이야?", [[33, 38]]),
        ("33세부터 38세까지 인구", [[33, 38]]),
        ("62세와 67세 사이 인구", [[62, 67]]),
        ("62에서 67살", [[62, 67]]),
        ("65세 이상 70세 미만 인구", [[65, 69]]),
        ("65살 이상 69살 이하", [[65, 69]]),
        ("20대부터 30대까지 여자", [[20, 39]]),
        # 연도·다른 단위의 "부터·에서"는 연령이 아니다
        ("2020년부터 2025년까지 인구", []),
        ("2015년에서 2020년 사이 30대", [[30, 39]]),
        ("3명에서 5명 사이", []),
    ],
)
def test_extract_ages_spoken_range(question: str, ages: list) -> None:
    assert qa.extract_ages(question) == ages


def test_age_group_grounds_age_defined_indicator() -> None:
    # 지표 이름이 없어도 연령 집단 + 묻는 말이 있으면 HCX가 고른 계산 지표를 쓴다
    p = qa.correct(
        "우리나라 사람 중에 노인이 몇 퍼센트예요?",
        parsed(indicator="고령인구 비율 (%)", regions=["전국"], term="노인이 몇 퍼센트"),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율" and not p.get("unsupported")
    p = qa.correct(
        "의성군은 애들보다 노인이 몇 배나 많아?",
        parsed(indicator="노령화지수", regions=["의성군"], term="노령화지수"),
        TODAY,
    )
    assert p["indicator"] == "노령화지수" and not p.get("unsupported")
    # 한 집단만 있으면 노령화지수의 근거가 아니다
    p = qa.correct(
        "의성군 노인 몇 배야?", parsed(indicator="노령화지수", regions=["의성군"]), TODAY
    )
    assert p.get("unsupported") is True


def test_age_group_count_without_population_word() -> None:
    # "인구"라는 말이 없어도 연령 집단의 사람 수를 물으면 연령 구간 인구
    p = qa.correct(
        "광주랑 전남 노인 다 합치면 몇 명이야",
        parsed(indicator="고령인구 비율", regions=["광주광역시", "전라남도"]),
        TODAY,
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[65, None]]
    p = qa.correct(
        "요 몇 년 새 부산 어르신들 얼마나 늘었어?",
        parsed(type="trend", indicator="고령인구 비율", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[65, None]]


def test_unknown_label_that_is_age_population() -> None:
    # HCX가 사전에 없는 "연령 구간 인구" 이름을 주거나 지표를 비워도 총인구
    p = qa.correct(
        "서울청년 19~34살 몇명임?", parsed(indicator="19~34세 인구 수", regions=["서울"]), TODAY
    )
    assert p["indicator"] == "총인구" and p["ages"] == [[19, 34]]
    p = qa.correct("애들 몇 명이야?", parsed(indicator="유소년인구 수", term="애들"), TODAY)
    assert p["indicator"] == "총인구" and p["ages"] == [[0, 14]]
    p = qa.correct(
        "울산에 100살 이상 어르신 몇 분 계셔?",
        parsed(type="other", indicator="", term="100살 이상 어르신", regions=["울산"]),
        TODAY,
    )
    assert (p["type"], p["indicator"], p["ages"]) == ("value", "총인구", [[100, None]])


def test_age_population_needs_people_only() -> None:
    # 연령이 있어도 다른 대상(취업자·가구)을 세면 인구로 바꾸지 않는다
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
    # HCX가 엉뚱한 지표를 주거나 비워도, 연령 집단이 아닌 대상(취업자)을 세면 총인구로 바꾸지 않는다
    p = qa.correct(question, parsed(indicator=llm), TODAY)
    assert p["indicator"] != "총인구"


@pytest.mark.parametrize(
    ("question", "regions"),
    [
        ("광주랑 전남 노인 다 합치면 몇 명이야", ["광주광역시", "전라남도"]),
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
        ("노인이랑 애들 몇 배 차이야", True),  # 나누는 쪽을 말하지 않으면 노령화지수
        ("노인보다 애들이 몇 배 많아?", False),  # 유소년 ÷ 노인은 노령화지수가 아니다
        ("노인 대비 유소년 몇 배야", False),
    ],
)
def test_aging_index_direction(question: str, ok: bool) -> None:
    p = qa.correct(question, parsed(indicator="노령화지수"), TODAY)
    assert (p["indicator"] == "노령화지수" and not p.get("unsupported")) is ok


def test_births_versus_deaths_is_natural_increase() -> None:
    # HCX가 지표를 비워도 "태어난 vs 돌아가신"은 자연증가 건수
    p = qa.correct("작년에 태어난 사람이 많아 돌아가신 사람이 많아?", {"type": "other"}, TODAY)
    assert (p["type"], p["indicator"]) == ("value", "자연증가 건수")
    assert not p.get("unsupported")


# ---- 원인·예측 질문, 지원하지 않는 조건 -------------------------------------------------


@pytest.fixture
def data_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))


@pytest.mark.usefixtures("data_years")
@pytest.mark.parametrize(
    ("question", "year", "prediction"),
    [
        ("앞으로 출산율 다시 오를까?", None, True),
        ("서울 인구는 계속 줄어들까?", None, True),
        ("향후 고령인구 비율 전망", None, True),
        ("2040년 서울 인구는 몇 명이 될까요?", 2040, False),  # 미래 연도: 장래추계로 답한다
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
    # 지표 사전 표는 체중으로 나뉘어 있지 않다: 전체 출생아 수로 답하지 않고 맞는 구간 없음
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
        ("만 6~11세 초등학생 나이 인구는?", None),  # '나이'의 '이'는 조사가 아니다
        ("작년에 인구가 줄었나", None),
        ("주민등록인구", None),
    ],
)
def test_population_modifier(monkeypatch, question: str, word) -> None:
    regions = {"서울", "서울의"}
    monkeypatch.setattr(indicators, "find_regions", lambda t: [t] if t in regions else [])
    assert qa.population_modifier(question) == word


def test_total_population_with_other_statistic_word_is_not_grounded(monkeypatch) -> None:
    # "통학 인구"를 별칭 '인구'만 보고 총인구로 답하지 않는다 → 바로 계산을 시도한다
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
        # '비율' 없이 몇 프로를 물음 → 같은 이름의 비율 지표
        ("마포구 주민 가운데 등록된 외국인은 몇 프로 정도야?", "등록외국인 비율"),
        ("인천 등록외국인 몇 %야", "등록외국인 비율"),
        # 몇 명을 물음 → 그대로
        ("마포구 등록된 외국인 몇 명이야?", "등록외국인 수"),
        # 증감률을 물음 → 그대로
        ("마포구 등록외국인 작년보다 몇 퍼센트 늘었어?", "등록외국인 수"),
        # 전국 중 비중은 share_of가 맡는다 → 그대로
        ("전국 등록외국인 중 마포구는 몇 프로야?", "등록외국인 수"),
    ],
)
def test_percent_question_uses_the_ratio_sibling(registered_aliases, question, expected) -> None:
    for llm in ("등록외국인 수", "몇 프로"):  # HCX가 지표 칸에 '몇 프로'를 넣는 경우도
        p = qa.correct(question, parsed(indicator=llm, term="몇 프로", regions=["마포구"]), TODAY)
        assert p["indicator"] == expected
        # 바꾼 비율 지표는 질문에 근거가 있는 지표다 (근거 없는 지표 검사가 되돌리지 않는다)
        assert not p.get("unsupported")


@pytest.mark.parametrize(
    ("question", "llm", "expected"),
    [
        # 그해 태어난 아이 수: HCX가 출생아 수로 바르게 읽었으면 덮어쓰지 않는다
        ("2024년 서울에서 아기 몇 명 낳았어?", "출생아 수", "출생아 수"),
        ("2024년 서울에서 아기 몇 명 낳았어?", "합계출산율", "출생아 수"),
        # 여자 한 명이 평균 몇 명 낳는지: 합계출산율
        ("2024년 서울 여자 한 명이 평균 아이 몇 명 낳아?", "출생아 수", "합계출산율"),
        ("여성 1명당 아기 몇 명 낳아?", None, "합계출산율"),
    ],
)
def test_babies_born_or_per_woman(question: str, llm, expected: str) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["indicator"] == expected


@pytest.mark.parametrize("question", ["서울 2030세대 인구는?", "서울 2030 세대 인구는?"])
def test_generation_population_is_total_population(question: str) -> None:
    # "2030세대"는 연령 조건 (20~39세).
    # '세대 인구'를 다른 통계로 보지 않고, 2030년으로도 읽지 않는다
    p = qa.correct(question, parsed(indicator="총인구", regions=["서울"], year=2030), TODAY)
    assert p["indicator"] == "총인구" and p["grounded"] is not False
    assert p["ages"] == [[20, 39]] and p["year"] is None


# ---- 변형셋 1차: 지표 이름 없이 풀어 말한 질문 (2026-11-18) ---------------------------------


@pytest.mark.parametrize(
    ("question", "llm", "expected"),
    [
        ("해운대구 일할 나이 인구 100명당 노인 몇 명이야?", "총인구", "노년부양비"),
        ("의성군 어르신 수가 아이들 수의 몇 배야?", "고령인구 비율", "노령화지수"),
        ("영도구 주민 나이 순으로 줄 세우면 가운데가 몇 살이야?", None, "중위연령"),
        ("세종이랑 전남, 인구에 비해 아기 태어나는 정도 얼마나 차이 나?", "출생아 수", "조출생률"),
        ("작년에 이사 온 사람에서 떠난 사람 빼면 제일 많이 남는 시도 3곳", "순이동", "순이동"),
        ("어르신 비율이 가장 높은 지역 세 군데 알려줘", "고령인구 비율%", "고령인구 비율"),
        ("대구랑 부산 중에 여자 1명이 평생 낳는 아이 수 많은 곳은?", "합계출산율", "합계출산율"),
        ("요새 사람들 애 몇 명씩 낳아?", "출생아 수", "합계출산율"),
        ("노원구가 강남구보다 사람이 많아 적어?", "총인구", "총인구"),
        ("서울에서 사람 제일 많이 사는 구 다섯 곳", "총인구", "총인구"),
        ("수성구에 이십대 얼마나 살아?", "총인구", "총인구"),
        ("광주하고 전라남도 어르신 수 합쳐줘", "고령인구 수", "총인구"),
    ],
)
def test_spelled_out_indicators(question: str, llm, expected: str) -> None:
    p = qa.correct(question, parsed(indicator=llm, regions=["서울"]), TODAY)
    assert p["indicator"] == expected
    # 근거 없는 지표로 보고 통계표 찾기로 넘기지 않는다 (이 파일은 별칭을 줄여 둬서 None일 수 있다)
    assert p["grounded"] is not False


@pytest.mark.parametrize(
    "question",
    [
        "제주도에 외국인 등록한 사람 몇 명 있어?",  # 등록외국인이지 총인구가 아니다
        "2010년 기준으로 전철로 통근이나 통학한 사람 몇 명이었어?",
        "작년 사망자 중에 일흔 살 이상인 분 몇 명이야?",
    ],
)
def test_other_people_are_not_total_population(question: str) -> None:
    p = qa.correct(question, parsed(indicator=None, regions=["서울"]), TODAY)
    assert p["indicator"] != "총인구"


@pytest.mark.parametrize(
    ("question", "ages"),
    [
        ("수성구에 이십대 얼마나 살아?", [[20, 29]]),
        ("울산에 백 살 이상 되신 분 얼마나 계세요?", [[100, None]]),
        ("작년 사망자 중에 일흔 살 이상인 분", [[70, None]]),
        ("오십 대 남자", [[50, 59]]),
    ],
)
def test_korean_number_ages(question: str, ages) -> None:
    assert qa.extract_ages(question) == ages


def test_korea_population_is_nationwide() -> None:
    p = qa.correct("한국 인구 중 어르신 비중 몇 %야?", parsed(indicator="고령인구 비율"), TODAY)
    assert p["regions"] in ([], ["전국"]) and p["indicator"] == "고령인구 비율"


def test_merging_words_before_population_are_plain() -> None:
    # "합쳐서 인구", "시군 중 인구"의 앞말은 다른 통계가 아니다
    assert qa.population_modifier("부산 울산 경남 합쳐서 인구 몇 명이야?") is None
    assert qa.population_modifier("경기도 시군 중 인구 많은 순서로 세 곳") is None
    assert qa.population_modifier("2020년 통학 인구") == "통학"


@pytest.mark.parametrize(
    "question",
    [
        "여성 평균 초혼연령 높은 시도 3곳",
        "경기도 시군 중 인구 많은 순서로 세 곳",
        "작년 출생아 수 많은 시군구 상위 3곳",
    ],
)
def test_high_rank_overrides_ascending(question) -> None:
    # 추론을 끈 질문 해석이 "높은"을 낮은 순으로 읽은 경우 (변형셋 v1 P-081)
    p = qa.correct(question, parsed(type="rank", indicator="출생아 수", ascending=True), TODAY)
    assert p["ascending"] is False


@pytest.mark.parametrize(
    "question",
    [
        "작년 한 해 인구 감소 폭 큰 시도 top3",  # 감소 순위는 낮은 순이 맞다
        "합계출산율 가장 낮은 시도 3곳",
    ],
)
def test_low_or_decrease_rank_keeps_ascending(question) -> None:
    p = qa.correct(question, parsed(type="rank", indicator="출생아 수", ascending=True), TODAY)
    assert p["ascending"] is True


def test_baby_words_are_not_an_age_for_birth_indicators(monkeypatch) -> None:
    # 출생 지표의 "아기 · 아이"는 태어난 아이: 0세 연령 조건을 붙이지 않는다
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n)
    q0 = "다문화 가정 아기 최근 5년 동안 어떻게 변했어?"
    p = qa.correct(q0, parsed(type="trend", indicator="다문화 출생아 수"), TODAY)
    assert not p.get("ages") and not p.get("age_group")
    q = qa.correct("서울 아이들 수 얼마나 돼?", parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert q["ages"] == [[0, 14]]  # 출생 지표가 아니면 지금처럼 연령 집단


def test_counting_words_before_population_are_plain() -> None:
    assert qa.population_modifier("경기도에서 제일 큰 도시 3개 인구로") is None


def test_change_rank_question(monkeypatch) -> None:
    # "인구 제일 많이 줄어든 시도": 그해 값이 아니라 전년 대비 변화량 순위
    # (HCX가 고른 자연증가 건수를 총인구로 고친다)
    p = qa.correct("작년에 인구 제일 많이 줄어든 시도 세 군데",
                   parsed(type="rank", indicator="자연증가 건수"), TODAY)  # fmt: skip
    assert p["rank_by"] == "decrease" and p["indicator"] == "총인구" and p["type"] == "rank"
    q = qa.correct("인구 증가 폭이 가장 큰 구는?", parsed(type="rank", indicator="총인구"), TODAY)
    assert q["rank_by"] == "increase"
    # 해를 묻는 질문은 아니다
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
    # HCX가 비율 질문을 원인 질문으로 읽어도 원인 말이 없으면 원인 질문이 아니다
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
