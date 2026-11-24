import re

DIGIT_FINAL = {"0": True, "1": True, "2": False, "3": True, "4": False,
               "5": False, "6": True, "7": True, "8": True, "9": False}  # fmt: skip
SYMBOL_FINAL = {"%": False, "㎢": False, "㎡": False, "㎏": True}
TRAILING = re.compile(r"[\s'\"’”)\]]+$")


def _last(word: str) -> str:
    base = re.sub(r"\([^()]*\)$", "", word) if word.endswith(")") else word
    base = TRAILING.sub("", base)
    return base[-1] if base else ""


def has_final(word: str) -> bool | None:
    last = _last(word)
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28 != 0
    if last in DIGIT_FINAL:
        return DIGIT_FINAL[last]
    return SYMBOL_FINAL.get(last)


def josa(word: str, pair: str) -> str:
    final = has_final(word)
    if final is None:
        return f"{word}{pair[0]}({pair[1]})"
    return word + (pair[0] if final else pair[1])


def ro(word: str) -> str:
    last = _last(word)
    if "가" <= last <= "힣":
        jong = (ord(last) - ord("가")) % 28
        return word + ("으로" if jong not in (0, 8) else "로")
    if last in DIGIT_FINAL:
        return word + ("으로" if DIGIT_FINAL[last] and last not in "178" else "로")
    if last in SYMBOL_FINAL:
        return word + ("으로" if SYMBOL_FINAL[last] else "로")
    return word + "(으)로"
