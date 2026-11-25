import pytest

from app.services import indicators


def names(q):
    return {name for *_, name in indicators.find_in_text(q)}


@pytest.mark.parametrize(
    "q, name",
    [
        ("작년 서울 이혼 몇 건이야", "이혼 건수"),
        ("서울 이혼 몇 쌍이야", "이혼 건수"),
        ("작년 서울로 이사 온 사람 몇 명이야", "전입자 수"),
        ("서울 엄마들 몇 살에 아이 낳아", "모의 평균 출산연령"),
        ("부산 애 낳는 나이 알려줘", "모의 평균 출산연령"),
        ("서울 첫 아이 낳는 나이", "첫째아 평균 출산연령"),
        ("전남은 노인 한 명을 몇 명이 부양해", "노인 1명당 생산연령인구"),
        ("서울 아이 비율", "유소년인구 비율"),
        ("부산 아이들 비중", "유소년인구 비율"),
        ("서울 노인 대 아이 비율", "노령화지수"),
        ("이사 온 이유 중 일자리 때문인 비중은?", "전입 사유 구성"),
        ("결혼한 부부 중 몇 %가 이혼해?", "혼인 대비 이혼 건수 비율"),
        # 1인 가구 (11/25, 사전 추가: 인구총조사 가구원수별 가구 표)
        ("대전 혼자 사는 집 몇 개야", "1인 가구 수"),
        ("강릉 나홀로 가구 수", "1인 가구 수"),
        ("경기도 독거 가구 2015년부터 얼마나 늘었어", "1인 가구 수"),
        ("가구 전체에서 혼자 사는 집 비율은?", "1인 가구 비율"),
        ("부산 1인가구 비중 알려줘", "1인 가구 비율"),
        ("대전 1인 가구 몇 퍼센트야", "1인 가구 비율"),
    ],
)
def test_everyday_indicator_aliases_2(q, name):
    assert name in names(q), (q, names(q))


@pytest.mark.parametrize(
    "q, not_name",
    [
        ("서울 노인 대 아이 비율", "유소년인구 비율"),
        ("작년에 태어난 아기 중 첫째 아이 비율은?", "유소년인구 비율"),
        ("서울 첫 아이 낳는 나이", "모의 평균 출산연령"),
        ("결혼한 부부 중 몇 %가 이혼해?", "이혼 건수"),
        ("가구 전체에서 혼자 사는 집 비율은?", "1인 가구 수"),  # 비율만
        ("부산 1인가구 비중 알려줘", "1인 가구 수"),
        ("이사 온 이유 중 일자리 때문인 비중은?", "전입자 수"),
    ],
)
def test_everyday_indicator_aliases_2_not(q, not_name):
    assert not_name not in names(q), (q, names(q))


def _correct(q, indicator):
    import datetime

    from app.services import qa

    return qa.correct(
        q, {"type": "value", "indicator": indicator, "regions": []}, datetime.date(2026, 11, 20)
    )


@pytest.mark.needs_data
def test_count_words_inside_alias_keep_indicator():
    result = _correct("전남은 노인 한 명을 몇 명이 부양해", "노년부양비")
    assert result["indicator"] == "노인 1명당 생산연령인구"


@pytest.mark.needs_data
def test_count_question_still_becomes_population():
    assert _correct("서울 노인 몇 명이야", "고령인구 비율")["indicator"] == "총인구"
