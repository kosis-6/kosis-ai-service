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
