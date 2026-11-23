"""짧은 별칭이 다른 지표 표현 안에 우연히 걸리던 문제 (fix/short-aliases, 2026-11-23)

지표 찾기는 띄어쓰기를 지운 문장에서 별칭을 글자로 찾는다. 그래서
"대전 출생아"의 "대전출생아" 안 "전출"이 전출자 수로 잡혔다.
"""

import pytest

from app.services import indicators


def names(question):
    return sorted({n for *_, n in indicators.find_in_text(question)})


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # 고친 것
        ("대전 출생아", ["출생아 수"]),
        ("2020년 이전 출생아", ["출생아 수"]),
        ("국제결혼 몇 건", ["다문화 혼인 건수"]),
        ("국제결혼 몇 쌍", ["다문화 혼인 건수"]),
        ("다문화 가정에서 태어난 아기", ["다문화 출생아 수"]),
        ("다문화 가정에서 태어난 아이", ["다문화 출생아 수"]),
        ("다문화 가정 아이", ["다문화 출생아 수"]),
        ("다문화 가정 출생아", ["다문화 출생아 수"]),
        ("다문화 출생아 비중", ["다문화 출생 비중"]),
        # 그대로여야 하는 것
        ("대전 전출자", ["전출자 수"]),
        ("결혼 몇 건", ["혼인 건수"]),
        ("태어난 아기", ["출생아 수"]),
        ("서울 인구", ["총인구"]),
        ("셋째 이상 출생아 수", ["셋째 이상 출생아 수"]),
        ("인구 천명당 출생아 수", ["조출생률"]),
    ],
)
def test_short_alias_picks_the_right_indicator(question, expected):
    assert names(question) == expected


@pytest.mark.parametrize(
    ("indicator", "question"),
    [
        # not_in은 qa.correct에서 "다른 통계" 안내에도 쓰인다 (문장에 있기만 하면).
        # 원래 그 지표를 묻는 질문이 막히면 안 된다
        ("전출자 수", "대전 전출자 수"),
        ("전출자 수", "대전 출신 중 다른 시도로 나간 사람"),
        ("사망자 수", "서울 사망자 수"),
        ("혼인 건수", "부산 결혼 몇 건"),
        ("출생아 수", "대전 출생아"),
    ],
)
def test_exclusion_does_not_block_the_plain_question(indicator, question):
    assert indicators.excluded_phrase(indicator, question) is None
