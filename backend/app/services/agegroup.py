import re
import sys
from dataclasses import dataclass

GROUPS = [
    (
        ("생산가능인구", "생산연령인구", "생산가능 인구", "일하는 나이대", "일할 나이"),
        15,
        64,
        "생산가능인구 15~64세 (인구총조사 기준)",
    ),
    (("학령인구", "학령 인구", "학령기"), 6, 21, "학령인구 6~21세 (교육기본통계 기준)"),
    (
        (
            "핵심생산연령인구",
            "핵심 생산연령인구",
            "핵심생산인구",
            "핵심 생산인구",
            "핵심 경제활동 연령",
            "핵심 노동인구",
        ),
        25,
        49,
        "핵심생산연령인구 25~49세 (통계청 장래인구추계)",
    ),
    (("영아", "갓난아기", "갓난아이", "아기"), 0, 0, "영아 0세 (만 1세 미만)"),
    (("영유아",), 0, 5, "영유아 0~5세 (영유아보육법)"),
    (
        ("미취학 아동", "미취학", "취학 전", "취학전"),
        0,
        6,
        "취학 전 0~6세 (초등학생을 7세부터로 봄)",
    ),
    (("유치원생", "유치원 다니는"), 3, 5, "유치원생 3~5세 (유아교육법 만 3세~취학 전)"),
    (("유아", "유아기"), 3, 5, "유아 3~5세 (유아교육법 만 3세~취학 전)"),
    (("유소년", "유소년인구"), 0, 14, "유소년인구 0~14세 (인구총조사 기준)"),
    (
        ("어린이", "아동", "애들", "아이들", "어린아이", "아이"),
        0,
        14,
        "유소년 0~14세로 봄 (아동복지법은 18세 미만)",
    ),
    (("청소년",), 9, 24, "청소년기본법 9~24세"),
    (("초등학생", "초등생", "초등학교 다니는", "초딩"), 7, 12, "초등학생 7~12세 (학령 기준)"),
    (("중학생", "중학교 다니는", "중딩"), 13, 15, "중학생 13~15세 (학령 기준)"),
    (("고등학생", "고교생", "고등학교 다니는", "고딩"), 16, 18, "고등학생 16~18세 (학령 기준)"),
    (
        ("중고등학생", "중·고등학생", "중고생", "중고교생"),
        13,
        18,
        "중·고등학생 13~18세 (학령 기준)",
    ),
    (
        ("초중고생", "초중고등학생", "초·중·고등학생", "초중고 학생"),
        7,
        18,
        "초·중·고등학생 7~18세 (학령 기준)",
    ),
    (("대학생", "대학 다니는"), 18, 21, "대학생 18~21세 (학령 기준)"),
    (
        ("청년", "젊은 사람", "젊은이", "젊은층", "젊은 층"),
        19,
        34,
        "청년기본법 19~34세 (고용 통계는 15~29세)",
    ),
    (("2030세대", "2030 세대"), 20, 39, "2030세대 20~39세"),
    (("4050세대", "4050 세대"), 40, 59, "4050세대 40~59세"),
    (("준고령자", "준고령층"), 50, 54, "준고령자 50~54세 (고령자고용법 시행령)"),
    (("신중년",), 50, 64, "신중년 50~64세 (고용노동부 기준)"),
    (("중장년", "중년"), 40, 64, "중장년 40~64세 (고용노동부 기준)"),
    (
        ("후기 고령인구", "후기고령인구", "후기 고령자", "후기고령자", "후기 노인"),
        75,
        None,
        "후기 고령인구 75세 이상",
    ),
    (
        ("초고령자", "초고령 인구", "초고령인구", "초고령"),
        85,
        None,
        "초고령 85세 이상 (고령자 통계 구분)",
    ),
    (
        (
            "고령자",
            "고령층",
            "고령 인구",
            "고령인구",
            "노인",
            "노년층",
            "노년",
            "어르신",
            "노령층",
            "노령 인구",
            "노령인구",
            "할머니",
            "할아버지",
            "시니어",
            "실버",
        ),
        65,
        None,
        "고령자 65세 이상 (노인복지법·고령자 통계)",
    ),
    (("미성년", "미성년자"), 0, 18, "민법 19세 미만"),
    (("성인", "어른", "성년"), 19, None, "성인 19세 이상 (민법)"),
    (
        ("가임기 여성", "가임 여성", "가임여성", "가임기", "가임 연령"),
        15,
        49,
        "가임기 15~49세 (합계출산율 계산 기준)",
    ),
]
FACILITY = re.compile(r"어린이집|유치원|학교|학원")
FACILITY_SENSITIVE = {"애들", "아이들", "아이"}
BIRTH = re.compile(r"낳|태어|출생|출산")
INFANT_DEATH = re.compile(r"영아\s*사망")
BIRTH_SENSITIVE = {"아기", "갓난아기", "갓난아이", "영아", "아이", "아이들", "애들"}
NOT_BEFORE = {"초고령": ("사회",), "성인": ("병",)}
NUMERIC_AGE = re.compile(r"\d+\s*세(?!대)|\d+\s*대(?!\s*(광역시|도시))|\d+\s*살")


NATIVE_COUNT = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}  # fmt: skip
COUNT_ALONE = re.compile(
    r"(?<![가-힣])(하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)"
    r"(?=\s|$|[?.!,~]|(?:이|을|를|은|는|인)(?![가-힣]))"
)
COUNT_BEFORE_UNIT = re.compile(
    r"(?<![가-힣0-9])(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?=명|가구|자녀|아이)"
)
CHILD_WORD = r"(?:자녀|아이|(?<![가-힣])애|자식)"
AGE_UNIT = r"\s*(?:살|세|개월|대)"
CHILD_COUNT = re.compile(
    rf"{CHILD_WORD}\s*(?:가|를|이)?\s*(\d+)(?![0-9]|{AGE_UNIT})(?:\s*명)?"
    rf"|(?<![0-9])(\d+)\s*(?:명\s*의?\s*)?{CHILD_WORD}"
)
COUNT_SENSITIVE = {"아이", "아이들", "애들"}
COUNT_AFTER = re.compile(
    r"^\s*(?:가|를|이|들이|들을)?\s*"
    rf"(?:(?:하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)(?![가-힣]*째|{AGE_UNIT})"
    r"|(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+)\s*명)"
)
COUNT_BEFORE = re.compile(
    r"(?:(?<![가-힣0-9])(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)|\d+\s*명\s*의?)\s*$"
)


def count_digits(text: str) -> str:
    text = COUNT_ALONE.sub(lambda m: str(NATIVE_COUNT[m[1]]), text)
    text = COUNT_BEFORE_UNIT.sub(lambda m: str(NATIVE_COUNT[m[1]]), text)
    return CHILD_COUNT.sub(lambda m: f"{m[1] or m[2]}자녀", text)


COUNT_UNIT = re.compile(r"(?<![0-9])(\d+)\s*(?:자녀|명|인|세대)")


def count_words(item: str, question: str = "") -> list[str]:
    counted = count_digits(item)
    if counted == item:
        return []
    words = [w for w in (re.sub(r"[^0-9A-Za-z가-힣]", "", w) for w in counted.split()) if w]
    out = []
    for w in words:
        if w.isdigit():
            unit = next((m for m in COUNT_UNIT.finditer(count_digits(question)) if m[1] == w), None)
            if unit is None:
                return []
            w = re.sub(r"\s", "", unit[0])
        out.append(w)
    return out


@dataclass
class AgeRef:
    expr: str
    lo: int
    hi: int | None
    basis: str

    def label(self) -> str:
        if self.hi is None:
            return f"{self.lo}세 이상"
        return f"{self.lo}세" if self.lo == self.hi else f"{self.lo}~{self.hi}세"


def _find(question: str, word: str) -> int:
    pos = question.find(word)
    if pos < 0:
        return -1
    after = question[pos + len(word) :]
    if after[:1] in ("집", "관"):
        return -1
    if after.lstrip().startswith(NOT_BEFORE.get(word, ())):
        return -1
    if word in FACILITY_SENSITIVE and FACILITY.search(question):
        return -1
    if word in BIRTH_SENSITIVE and (BIRTH.search(question) or INFANT_DEATH.search(question)):
        return -1
    counted = COUNT_AFTER.match(after) or COUNT_BEFORE.search(question[:pos])
    if word in COUNT_SENSITIVE and counted:
        return -1
    return pos


def resolve(question: str) -> AgeRef | None:
    if NUMERIC_AGE.search(question):
        return None
    best = None
    for words, lo, hi, basis in GROUPS:
        for w in words:
            pos = _find(question, w)
            if pos >= 0 and (
                best is None or pos < best[0] or (pos == best[0] and len(w) > len(best[1].expr))
            ):
                best = (pos, AgeRef(w, lo, hi, basis))
    return best[1] if best else None


def spans(question: str) -> list[tuple[int, int, AgeRef]]:
    found: list[tuple[int, int, AgeRef]] = []
    for words, lo, hi, basis in GROUPS:
        for w in words:
            pos = _find(question, w)
            if pos >= 0:
                found.append((pos, pos + len(w), AgeRef(w, lo, hi, basis)))
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    out: list[tuple[int, int, AgeRef]] = []
    for f in found:
        if not any(a < f[1] and f[0] < b for a, b, _ in out):
            out.append(f)
    return out


def resolve_all(question: str) -> list[AgeRef]:
    found: list[tuple[int, AgeRef]] = []
    for words, lo, hi, basis in GROUPS:
        hits = []
        for w in words:
            pos = _find(question, w)
            if pos >= 0:
                hits.append((pos, AgeRef(w, lo, hi, basis)))
        if hits:
            found.append(min(hits, key=lambda h: (h[0], -len(h[1].expr))))
    out: list[AgeRef] = []
    for _, ref in sorted(found, key=lambda h: h[0]):
        if all((r.lo, r.hi) != (ref.lo, ref.hi) for r in out):
            out.append(ref)
    return out


AGE_LOWER_BOUND = r"이상|초과|넘(?:은|는)"
AGE_OVER = re.compile(rf"(?:만\s*)?(\d+)\s*(?:세|살)\s*({AGE_LOWER_BOUND})")
AGE_OVER_UNDER = re.compile(
    rf"(?:만\s*)?(\d{{1,3}})\s*(?:세|살)\s*({AGE_LOWER_BOUND})\s*"
    r"(?:만\s*)?(\d{1,3})\s*(?:세|살)\s*(미만|이하)"
)


def lower_age(age: str, bound: str) -> int:
    return int(age) + (bound == "초과")


AGE_SPANS = [
    (AGE_OVER_UNDER, lambda m: (lower_age(m[1], m[2]), int(m[3]) - (m[4] == "미만"))),
    (re.compile(r"(\d+)\s*대\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*대"), lambda m: (int(m[1]), int(m[2]) + 9)),
    (re.compile(r"(\d+)\s*대\s*초반"), lambda m: (int(m[1]), int(m[1]) + 3)),
    (re.compile(r"(\d+)\s*대\s*중반"), lambda m: (int(m[1]) + 4, int(m[1]) + 6)),
    (re.compile(r"(\d+)\s*대\s*후반"), lambda m: (int(m[1]) + 7, int(m[1]) + 9)),
    (re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))"), lambda m: (int(m[1]), int(m[1]) + 9)),
    (AGE_OVER, lambda m: (lower_age(m[1], m[2]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*세"), lambda m: (int(m[1]), int(m[2]))),
    (re.compile(r"(\d+)\s*세\s*미만"), lambda m: (0, int(m[1]) - 1)),
    (re.compile(r"(\d+)\s*세\s*이하"), lambda m: (0, int(m[1]))),
]


NATIVE_TENS = {
    "열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50,
    "예순": 60, "일흔": 70, "여든": 80, "아흔": 90,
}  # fmt: skip
NATIVE_UNITS = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "석": 3, "넷": 4, "네": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9,
}  # fmt: skip
NATIVE_AGE = re.compile(
    rf"(?<![가-힣])({'|'.join(sorted(NATIVE_TENS, key=len, reverse=True))})?\s*"
    rf"({'|'.join(sorted(NATIVE_UNITS, key=len, reverse=True))})?\s*살(?![림고던며면았아기])"
)
SINGLE_AGE = re.compile(
    r"(?<![\d~\-])(\d{1,3})\s*(?:세|살)"
    r"(?!\s*(?:이상|이하|미만|초과|넘|부터|까지|이후|이전|전후|안팎|단위|간격|별|[~\-]))"
)


SINO_DIGIT = {"이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
SINO_DECADE = re.compile(r"(?<![가-힣])([이삼사오육칠팔구])?십\s*대(?![가-힣]*(?:학|표|회))")
SINO_HUNDRED = re.compile(r"(?<![가-힣])백\s*(?:살|세)")


def native_to_digits(text: str) -> str:

    def repl(m: re.Match) -> str:
        if not (m[1] or m[2]):
            return m[0]
        return f"{NATIVE_TENS.get(m[1], 0) + NATIVE_UNITS.get(m[2], 0)}세"

    text = SINO_HUNDRED.sub("100세", text)
    text = SINO_DECADE.sub(lambda m: f"{SINO_DIGIT.get(m[1], 1)}0대", text)
    return NATIVE_AGE.sub(repl, text)


def said(text: str) -> bool:
    t = native_to_digits(text)
    return bool(NUMERIC_AGE.search(t)) or span(t) is not None


def single_age(text: str) -> str | None:
    t = re.sub(r"([~\-])\s*만\s*(?=\d)", r"\1", native_to_digits(text))
    ages = {f"{int(m[1])}세" for m in SINGLE_AGE.finditer(t)}
    return ages.pop() if len(ages) == 1 else None


def span(text: str) -> AgeRef | None:
    for pat, fn in AGE_SPANS:
        if m := pat.search(text):
            lo, hi = fn(m)
            return AgeRef(m[0], lo, hi, "숫자 구간")
    return resolve(text)


if __name__ == "__main__":
    for q in sys.argv[1:] or [
        "노인 인구가 많은 동네",
        "청년들이 많이 떠나는 지역",
        "65세 이상 노인 수",
    ]:
        print(q, "→", resolve(q))
