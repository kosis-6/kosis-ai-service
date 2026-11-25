"""'A 중 B 비율'은 A의 비율 지표가 아니다 (11/25): 사전에 1인 가구 비율이 들어가자
"1인 가구 중 여성 비율"이 사전 1인 가구 비율(1인 가구 ÷ 일반가구)로 답하던 것"""

from app.services import qa


def _fixes(question, indicator):
    out = []
    qa._ratio_sibling(question, {"indicator": indicator}, lambda k, v, why: out.append((k, v)))
    return out


def test_ratio_sibling_switches_plain_ratio_question() -> None:
    assert _fixes("서울 1인 가구 비율이 몇 프로야", "1인 가구 수") == [
        ("indicator", "1인 가구 비율")
    ]


def test_ratio_sibling_sends_within_questions_to_the_table_path() -> None:
    # 사전 비율로 바꾸지 않고, 사전이 못 내는 값이라 표 경로로 (unsupported)
    unsupported = [("unsupported", True)]
    assert _fixes("2020년 서울 1인 가구 중 여성 비율은?", "1인 가구 수") == unsupported
    assert _fixes("2020년 서울 1인 가구 가운데 70세 이상 비율은?", "1인 가구 수") == unsupported
    assert qa._within_after("1인 가구 중 여성 비율", "1인 가구 수")
    assert not qa._within_after("1인 가구 비율", "1인 가구 수")
