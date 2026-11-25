import pytest

from app.services import indicators


def names(question):
    return sorted({n for *_, n in indicators.find_in_text(question)})


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("대전 출생아", ["출생아 수"]),
        ("2020년 이전 출생아", ["출생아 수"]),
        ("국제결혼 몇 건", ["다문화 혼인 건수"]),
        ("국제결혼 몇 쌍", ["다문화 혼인 건수"]),
        ("다문화 가정에서 태어난 아기", ["다문화 출생아 수"]),
        ("다문화 가정에서 태어난 아이", ["다문화 출생아 수"]),
        ("다문화 가정 아이", ["다문화 출생아 수"]),
        ("다문화 가정 출생아", ["다문화 출생아 수"]),
        ("다문화 출생아 비중", ["다문화 출생 비중"]),
        # 11/24 재계획이 쓰는 말: 조사망률 · 다문화 혼인 비중 · 다문화 출생아 수의 별칭 추가
        ("인구 10만 명당 사망자 수", ["조사망률"]),
        ("다문화 결혼 비중", ["다문화 혼인 비중"]),
        ("다문화가정 출생아 수", ["다문화 출생아 수"]),
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
        ("전출자 수", "대전 전출자 수"),
        ("전출자 수", "대전 출신 중 다른 시도로 나간 사람"),
        ("사망자 수", "서울 사망자 수"),
        ("혼인 건수", "부산 결혼 몇 건"),
        ("출생아 수", "대전 출생아"),
    ],
)
def test_exclusion_does_not_block_the_plain_question(indicator, question):
    assert indicators.excluded_phrase(indicator, question) is None
