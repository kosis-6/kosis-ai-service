"""받침에 맞춰 조사를 붙인다 ("총인구는", "고령인구 비율은", "출산율로")

답변 문장을 만드는 모듈(qa, lookup, indicators 등)이 함께 쓴다. qa를 불러오지 않는 작은 모듈이라
lookup·indicators에서도 서로 불러오는 고리 없이 쓸 수 있다.
받침을 알 수 없는 말(영문 등)은 두 조사를 함께 쓴다 ("TFR은(는)").
"""

import re

# 숫자로 끝나는 말 (2025, 3.2): 마지막 숫자를 읽은 소리의 받침
DIGIT_FINAL = {"0": True, "1": True, "2": False, "3": True, "4": False,
               "5": False, "6": True, "7": True, "8": True, "9": False}  # fmt: skip
# 답변에 나오는 단위 기호를 읽은 소리의 받침 (퍼센트, 제곱킬로미터, 킬로그램)
SYMBOL_FINAL = {"%": False, "㎢": False, "㎡": False, "㎏": True}
TRAILING = re.compile(r"[\s'\"’”)\]]+$")


def _last(word: str) -> str:
    """받침을 볼 마지막 글자. 끝의 따옴표·괄호 설명은 빼고 본다 ('출산율', "인구(명)")"""
    base = re.sub(r"\([^()]*\)$", "", word) if word.endswith(")") else word
    base = TRAILING.sub("", base)
    return base[-1] if base else ""


def has_final(word: str) -> bool | None:
    """받침이 있는가. 알 수 없으면 None"""
    last = _last(word)
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28 != 0
    if last in DIGIT_FINAL:
        return DIGIT_FINAL[last]
    return SYMBOL_FINAL.get(last)


def josa(word: str, pair: str) -> str:
    """받침이 있으면 pair[0], 없으면 pair[1]을 붙인다. pair 예: "은는", "이가", "을를", "과와" """
    final = has_final(word)
    if final is None:
        return f"{word}{pair[0]}({pair[1]})"
    return word + (pair[0] if final else pair[1])


def ro(word: str) -> str:
    """(으)로: 받침이 있으면(ㄹ 받침 제외) '으로'"""
    last = _last(word)
    if "가" <= last <= "힣":
        jong = (ord(last) - ord("가")) % 28
        return word + ("으로" if jong not in (0, 8) else "로")
    if last in DIGIT_FINAL:  # 1·7·8은 ㄹ 받침(일·칠·팔)
        return word + ("으로" if DIGIT_FINAL[last] and last not in "178" else "로")
    if last in SYMBOL_FINAL:
        return word + ("으로" if SYMBOL_FINAL[last] else "로")
    return word + "(으)로"
