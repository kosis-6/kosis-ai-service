"""일상어 지표 별칭 (일상어 사전 확장, 2026-11-18 + 리뷰 반영)

indicators.find_in_text 는 띄어쓰기를 뺀 문장에 별칭 글자가 들어 있는지로 찾는다.
짧은 별칭이 다른 말 안에 들어 있으면 잘못 잡히므로, 뜻이 갈리는 문장을 함께 확인한다.
"""

import pytest

from app.services import indicators, qa


def names(question):
    return {name for _, _, name in indicators.find_in_text(question)}


@pytest.mark.parametrize(
    "q, expected",
    [
        # 합계출산율 = 여자 1명이 평생 낳는 아이 수 / 출생아 수 = 그해 태어난 아이 수
        ("여자 한 명이 평생 애 몇 명 낳아?", "합계출산율"),
        ("서울 여자 한 명이 애 몇 명 낳아", "합계출산율"),
        ("작년 여자 한 명이 애 몇 명 낳았어", "합계출산율"),
        ("작년 서울에서 애 몇 명 낳았어?", "출생아 수"),
        ("작년에 서울에서 아기 몇 명 태어났어", "출생아 수"),
        ("2024년 국내출생 다문화가구 자녀 수는?", "다문화가구 국내출생 자녀 수"),
        ("부산 다문화가구 자녀 중 국내에서 태어난 자녀는 몇 명?", "다문화가구 국내출생 자녀 수"),
        (
            "2024년 부산 다문화가구 자녀 중 국내에서 태어난 자녀 비율은?",
            "다문화가구 국내출생 자녀 비율",
        ),
        ("2024년 전국 다문화가구 국내출생 자녀는 몇 퍼센트야?", "다문화가구 국내출생 자녀 비율"),
        ("2024년 부산 다문화 출생아 수는?", "다문화 출생아 수"),
        ("작년 서울 결혼 몇 건이야", "혼인 건수"),
        ("작년 서울 돌아가신 분 몇 명이야", "사망자 수"),
        ("서울 노인 대 아이 비율", "노령화지수"),
        ("서울 외국인 몇 명이야", "외국인주민 수"),
        ("등록된 외국인 몇 명", "등록외국인 수"),
        ("서울 한 세대에 몇 명", "세대당 인구"),
        ("서울 남녀 비율", "성비"),
        ("서울 성비 알려줘", "성비"),
        ("2020년 서울성비는?", "성비"),  # 붙여 쓴 짧은 이름은 그대로 맞춘다
        ("서울인구 몇 명이야", "총인구"),
        # 다자녀 = 셋째 이상 (출생·출산이 붙을 때만 지표로 본다)
        ("작년 다자녀 출산 몇 명이야", "셋째 이상 출생아 수"),
        ("다자녀 출산 비중 높은 지역", "셋째 이상 출생아 비중"),
    ],
)
def test_alias_found(q, expected):
    assert names(q) == {expected}, (q, names(q))


@pytest.mark.parametrize(
    "q, wrong",
    [
        ("2020년 서울 1인 가구 중 여성 비율은?", "성비"),  # "여성비율" 안의 "성비"
        # 두 글자 이름이 띄어쓰기를 넘어 걸리면 다른 낱말이다 (제외 표현을 하나씩 넣지 않아도)
        ("2020년 서울 1인 가구 중 여성 비중은?", "성비"),
        ("남성 비중이 높은 시군구", "성비"),
        ("여성 비가 오는 날", "성비"),
        ("결혼한 부부 중 몇 %가 이혼해?", "혼인 건수"),  # S-35
        ("서울은 한 집에 몇 명 살아?", "세대당 인구"),  # 집 = 가구에 가까움, 세대와 다른 통계
        ("등록된 외국인 몇 명", "외국인주민 수"),
        ("다자녀 가구 몇 가구야", "셋째 이상 출생아 수"),  # 미성년 자녀 가구 통계
    ],
)
def test_alias_not_found(q, wrong):
    assert wrong not in names(q), (q, names(q))


@pytest.mark.needs_data
@pytest.mark.parametrize(
    ("question", "indicator", "expected_table", "expected_value", "expected_unit"),
    [
        (
            "부산 2024년 국내출생 다문화가구 자녀 수는?",
            "다문화가구 국내출생 자녀 수",
            "DT_110025_A045_A",
            13084,
            "명",
        ),
        (
            "2024년 부산 다문화가구 자녀 중 국내에서 태어난 자녀 비율은?",
            "다문화가구 국내출생 자녀 비율",
            "DT_110025_A045_A",
            96.26,
            "%",
        ),
        ("2024년 부산 다문화 출생아 수는?", "다문화 출생아 수", "DT_1BB0006", 596, "명"),
    ],
)
def test_domestic_born_children_and_births_use_different_tables(
    question, indicator, expected_table, expected_value, expected_unit
):
    parsed = qa.correct(
        question,
        {
            "type": "value",
            "indicator": "다문화 출생아 수",
            "regions": ["부산"],
            "year": 2024,
            "sex": "계",
        },
    )
    assert parsed["indicator"] == indicator and parsed["grounded"]
    result = qa.compute(parsed)
    assert result["status"] == "ok"
    assert (result["tbl_id"], result["value"], result["unit"]) == (
        expected_table,
        expected_value,
        expected_unit,
    )
