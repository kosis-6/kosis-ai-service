"""받침에 맞춘 조사 (josa.py)"""

import pytest

from app.services.josa import josa, ro


@pytest.mark.parametrize(
    ("word", "pair", "expected"),
    [
        ("사망자 수", "은는", "사망자 수는"),
        ("출산율", "은는", "출산율은"),
        ("'혼인율'", "은는", "'혼인율'은"),  # 따옴표 안의 말로 본다
        ("인구(명)", "은는", "인구(명)는"),  # 끝의 괄호 설명은 빼고 본다
        ("2025", "은는", "2025는"),  # 이천이십오
        ("2021", "이가", "2021이"),  # 이천이십일
        ("부산광역시", "이가", "부산광역시가"),
        ("'외국인주민 수'", "과와", "'외국인주민 수'와"),
        ("TFR", "은는", "TFR은(는)"),  # 알 수 없으면 둘 다
    ],
)
def test_josa(word: str, pair: str, expected: str) -> None:
    assert josa(word, pair) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [("합계출산율", "합계출산율로"), ("총인구", "총인구로"), ("고령인구 비율", "고령인구 비율로"),
     ("2021", "2021로"), ("2023", "2023으로"), ("24.5%", "24.5%로"), ("TFR", "TFR(으)로")],
)  # fmt: skip
def test_ro(word: str, expected: str) -> None:
    assert ro(word) == expected
