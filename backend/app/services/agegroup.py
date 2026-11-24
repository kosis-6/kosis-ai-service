"""연령 집단 명칭 해석: "노인", "유소년", "청년", "생산가능인구" → 연령 구간 (검색 F-03·QA 공용)

정의가 법령·통계 기준으로 정해져 있으므로 LLM 추측이 아니라 사전으로 푼다.
숫자 구간("65세 이상", "20대")이
질문에 있으면 그쪽이 우선이고(qa.extract_ages / 검색은 items), 이 모듈은 이름으로만 말한 경우를
다룬다.
"청년"처럼 기준이 여럿인 말은 basis 에 적어 답변에서 "19~34세 기준"으로 고지한다.

실행: python -m app.services.agegroup "노인 인구가 많은 동네"
"""

import re
import sys
from dataclasses import dataclass

# (표현들, 시작, 끝(None=이상), 근거).
# 질문에서 먼저 나온 표현을 쓰고, 같은 위치면 긴 표현을 쓴다.
# 그래서 "신중년"은 한 글자 뒤의 "중년"보다 먼저 찾혀 신중년으로 읽힌다
# (준고령자·후기 고령인구·초고령도 같음)
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
    # 학교급 = 학령 구간 (국제결혼 가정 자녀 통계의 만 7~12세 · 13~15세 · 16~18세 구분)
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
# 이 말은 질문에 시설 이름이 있으면 연령 집단으로 보지 않는다
# ("어린이집 다니는 애들"은 0~14세 전체가 아님)
FACILITY = re.compile(r"어린이집|유치원|학교|학원")
FACILITY_SENSITIVE = {"애들", "아이들", "아이"}
# 출생·사망을 묻는 말 안의 아기·아이는 연령 집단이 아니다
# ("아기 몇 명 태어났어" = 출생아 수, "영아 사망률" = 지표 이름)
BIRTH = re.compile(r"낳|태어|출생|출산")
INFANT_DEATH = re.compile(r"영아\s*사망")
BIRTH_SENSITIVE = {"아기", "갓난아기", "갓난아이", "영아", "아이", "아이들", "애들"}
# 바로 뒤에 이 말이 오면 연령 집단이 아니다 ("초고령사회" = 65세 이상 비율이 20%를 넘는 사회)
NOT_BEFORE = {"초고령": ("사회",), "성인": ("병",)}
# 숫자 구간이 있으면 이름보다 숫자가 우선 ("2030세대"의 "세"는 나이가 아님)
NUMERIC_AGE = re.compile(r"\d+\s*세(?!대)|\d+\s*대(?!\s*(광역시|도시))|\d+\s*살")


NATIVE_COUNT = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}  # fmt: skip
# 혼자 쓴 고유어 수 ("자녀 넷", "아이 셋이"). '셋째'(출산순위) 같은 서수와 "하나도 없는"
# 같은 관용 표현('도')은 아니다
COUNT_ALONE = re.compile(
    r"(?<![가-힣])(하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉)"
    r"(?=\s|$|[?.!,~]|(?:이|을|를|은|는|인)(?![가-힣]))"
)
# 단위 앞의 수 ("자녀 네 명", "두 자녀"). '30세'의 '세'는 앞에 숫자가 있어 아니다
COUNT_BEFORE_UNIT = re.compile(
    r"(?<![가-힣0-9])(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?=명|가구|자녀|아이)"
)
# 자녀 수를 표의 칸 꼴로 ("자녀 4", "아이 4명", "4명의 자녀", "두 아이" → "4자녀").
# '애'는 낱말 하나일 때만 ("장애 2급"의 '애'가 아니다)
CHILD_WORD = r"(?:자녀|아이|(?<![가-힣])애|자식)"
# 수 뒤의 나이 단위 ("아이 다섯 살"은 다섯 살 아이다)
AGE_UNIT = r"\s*(?:살|세|개월|대)"
CHILD_COUNT = re.compile(
    rf"{CHILD_WORD}\s*(?:가|를|이)?\s*(\d+)(?![0-9]|{AGE_UNIT})(?:\s*명)?"
    rf"|(?<![0-9])(\d+)\s*(?:명\s*의?\s*)?{CHILD_WORD}"
)
# 아이 수를 말하는 말 ("아이 넷", "아이가 셋", "아이 2명", "두 아이")은 연령 집단(0~14세)이 아니라
# 자녀 수다. 연령으로 읽으면 연령 분류가 있는 인구 표가 자녀수별 가구 표를 이겼다
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
    """고유어 수를 숫자로, 자녀 수를 표의 칸 꼴로 ("미성년 자녀 넷" → "미성년 4자녀").
    표 칸은 '4자녀', '3명이상'처럼 숫자로 적어 "자녀 넷"이 어느 칸에도 맞지 않았다"""
    text = COUNT_ALONE.sub(lambda m: str(NATIVE_COUNT[m[1]]), text)
    text = COUNT_BEFORE_UNIT.sub(lambda m: str(NATIVE_COUNT[m[1]]), text)
    return CHILD_COUNT.sub(lambda m: f"{m[1] or m[2]}자녀", text)


# 수 뒤에 붙어 무엇을 센 것인지 말하는 단위 ('4자녀', '3명', '1인', '2세대')
COUNT_UNIT = re.compile(r"(?<![0-9])(\d+)\s*(?:자녀|명|인|세대)")


def count_words(item: str, question: str = "") -> list[str]:
    """수를 말로 쓴 조건 낱말을 표 칸 꼴의 낱말로 ("미성년 자녀 넷" → ['미성년', '4자녀']).
    수를 말로 쓰지 않았으면 []. 조건이 수뿐이면("넷", HCX가 '아이'를 떼고 뽑는다) 질문에서
    그 수가 붙은 말을 찾는다 ("아이 넷" → '4자녀'). 못 찾으면 []
    (수 하나로는 '4'가 든 아무 칸에나 맞는다)"""
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
    hi: int | None  # None = 이상
    basis: str

    def label(self) -> str:
        if self.hi is None:
            return f"{self.lo}세 이상"
        return f"{self.lo}세" if self.lo == self.hi else f"{self.lo}~{self.hi}세"


def _find(question: str, word: str) -> int:
    """질문에서 집단 이름의 위치. 시설 이름("어린이집"·"청소년관"), 시설과 함께 나온 말,
    출생·사망 질문 안의 아기·아이, 아이 수("아이 넷"), "초고령사회" 같은 말이면 -1"""
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
    """질문에 연령 집단 이름이 있고 숫자 구간이 없으면 그 집단의 구간. 없으면 None"""
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
    """질문에 나온 연령 집단 이름마다 (시작 위치, 끝 위치, 구간). 같은 자리에서는 긴 이름"""
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
    """질문에 나온 연령 집단 이름 전부 (나온 순서, 같은 구간은 하나).
    "애들보다 노인이 몇 배"처럼 두 집단을 함께 물은 경우를 알아보는 데 쓴다"""
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


AGE_NOT_YET = re.compile(r"(\d+)\s*(?:세|살)\s*(?:가|이)?\s*(?:안|못)\s*(?:된|됐|되는|돼)")


def lower_age(age: str, bound: str) -> int:
    """평가 기준의 '15살 넘은'은 15세 이상, 명시적인 '초과'만 다음 나이부터다."""
    return int(age) + (bound == "초과")


AGE_SPANS = [
    (AGE_OVER_UNDER, lambda m: (lower_age(m[1], m[2]), int(m[3]) - (m[4] == "미만"))),
    (re.compile(r"(\d+)\s*대\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*대"), lambda m: (int(m[1]), int(m[2]) + 9)),
    # "20대 초반" = 20~23세, "중반" = 24~26세, "후반" = 27~29세
    (re.compile(r"(\d+)\s*대\s*초반"), lambda m: (int(m[1]), int(m[1]) + 3)),
    (re.compile(r"(\d+)\s*대\s*중반"), lambda m: (int(m[1]) + 4, int(m[1]) + 6)),
    (re.compile(r"(\d+)\s*대\s*후반"), lambda m: (int(m[1]) + 7, int(m[1]) + 9)),
    (re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))"), lambda m: (int(m[1]), int(m[1]) + 9)),
    (AGE_OVER, lambda m: (lower_age(m[1], m[2]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*세"), lambda m: (int(m[1]), int(m[2]))),
    # 미만은 그 나이를 빼고(15세 미만 = 0~14세), 이하는 넣는다(15세 이하 = 0~15세)
    (re.compile(r"(\d+)\s*세\s*미만"), lambda m: (0, int(m[1]) - 1)),
    # "스무 살 안 됐는데" · "열 살이 못 된": 그 나이 미만 (11/25, 사전이 연령 없이 답하던 것)
    (AGE_NOT_YET, lambda m: (0, int(m[1]) - 1)),
    (re.compile(r"(\d+)\s*세\s*이하"), lambda m: (0, int(m[1]))),
]


# 고유어 나이 ('서른 살', '스물다섯 살', '스무 살', '한 살'). 표와 조건은 'N세'로 쓴다
NATIVE_TENS = {
    "열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50,
    "예순": 60, "일흔": 70, "여든": 80, "아흔": 90,
}  # fmt: skip
NATIVE_UNITS = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "석": 3, "넷": 4, "네": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9,
}  # fmt: skip
# '살' 뒤가 '살림 · 살고 · 살던'처럼 다른 낱말이면 나이가 아니다 ('두 살림', '한 살고')
NATIVE_AGE = re.compile(
    rf"(?<![가-힣])({'|'.join(sorted(NATIVE_TENS, key=len, reverse=True))})?\s*"
    rf"({'|'.join(sorted(NATIVE_UNITS, key=len, reverse=True))})?\s*살(?![림고던며면았아기])"
)
# 한 나이만 말한 경우. '1세 단위'(간격), '60세 넘어서도' · '65세부터'(구간)는 한 나이가 아니다
SINGLE_AGE = re.compile(
    r"(?<![\d~\-])(\d{1,3})\s*(?:세|살)"
    r"(?!\s*(?:이상|이하|미만|초과|넘|부터|까지|이후|이전|전후|안팎|단위|간격|별|[~\-]))"
)


# 한자어 나이 ('이십대', '삼십 대', '백 살', '백세'). 앞이 다른 낱말이면 아니다 ('문십대')
SINO_DIGIT = {"이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
SINO_DECADE = re.compile(r"(?<![가-힣])([이삼사오육칠팔구])?십\s*대(?![가-힣]*(?:학|표|회))")
SINO_HUNDRED = re.compile(r"(?<![가-힣])백\s*(?:살|세)")


def native_to_digits(text: str) -> str:
    """'서른 살' → '30세', '스물다섯 살' → '25세', '이십대' → '20대', '백 살' → '100세'.
    고유어 · 한자어 수가 없으면('몇 살') 그대로"""

    def repl(m: re.Match) -> str:
        if not (m[1] or m[2]):
            return m[0]
        return f"{NATIVE_TENS.get(m[1], 0) + NATIVE_UNITS.get(m[2], 0)}세"

    text = SINO_HUNDRED.sub("100세", text)
    text = SINO_DECADE.sub(lambda m: f"{SINO_DIGIT.get(m[1], 1)}0대", text)
    return NATIVE_AGE.sub(repl, text)


def said(text: str) -> bool:
    """나이를 말했는가 ('30세', '20대', '서른 살', '노인')"""
    t = native_to_digits(text)
    return bool(NUMERIC_AGE.search(t)) or span(t) is not None


def single_age(text: str) -> str | None:
    """나이 하나만 말했으면 그 나이('30세'). 구간이거나 여럿이면 None"""
    # '만 13세~만 15세'의 뒤 나이도 구간의 끝이다 ('~' 와 숫자 사이의 '만'을 지우고 본다)
    t = re.sub(r"([~\-])\s*만\s*(?=\d)", r"\1", native_to_digits(text))
    ages = {f"{int(m[1])}세" for m in SINGLE_AGE.finditer(t)}
    return ages.pop() if len(ages) == 1 else None


def span(text: str) -> AgeRef | None:
    """'20대', '30대 이상', '15~19세', '청소년' 같은 연령 표현 하나 → 구간.
    연령 표현이 아니면 None"""
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
