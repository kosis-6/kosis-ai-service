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

# (표현들, 시작, 끝(None=이상), 근거). 긴 표현부터 맞춘다
GROUPS = [
    (
        ("생산가능인구", "생산연령인구", "생산가능 인구", "일하는 나이대", "일할 나이"),
        15,
        64,
        "생산가능인구 15~64세 (인구총조사 기준)",
    ),
    (("학령인구", "학령 인구", "학령기"), 6, 21, "학령인구 6~21세 (교육기본통계 기준)"),
    (("영유아",), 0, 5, "영유아 0~5세 (영유아보육법)"),
    (("유소년", "유소년인구"), 0, 14, "유소년인구 0~14세 (인구총조사 기준)"),
    (("어린이", "아동", "애들"), 0, 14, "유소년 0~14세로 봄 (아동복지법은 18세 미만)"),
    (("청소년",), 9, 24, "청소년기본법 9~24세"),
    # 학교급 = 학령 구간 (국제결혼 가정 자녀 통계의 만 7~12세 · 13~15세 · 16~18세 구분)
    (("초등학생", "초등학교 다니는", "초딩"), 7, 12, "초등학생 7~12세 (학령 기준)"),
    (("중학생", "중딩"), 13, 15, "중학생 13~15세 (학령 기준)"),
    (("고등학생", "고딩"), 16, 18, "고등학생 16~18세 (학령 기준)"),
    (
        ("청년", "젊은 사람", "젊은이", "젊은층", "젊은 층"),
        19,
        34,
        "청년기본법 19~34세 (고용 통계는 15~29세)",
    ),
    (("중장년", "중년"), 40, 64, "중장년 40~64세 (고용노동부 기준)"),
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
        ),
        65,
        None,
        "고령자 65세 이상 (노인복지법·고령자 통계)",
    ),
    (("미성년", "미성년자"), 0, 18, "민법 19세 미만"),
]
# 이 말은 질문에 시설 이름이 있으면 연령 집단으로 보지 않는다
# ("어린이집 다니는 애들"은 0~14세 전체가 아님)
FACILITY = re.compile(r"어린이집|유치원|학교|학원")
FACILITY_SENSITIVE = {"애들"}
# 숫자 구간이 있으면 이름보다 숫자가 우선
NUMERIC_AGE = re.compile(r"\d+\s*세|\d+\s*대(?!\s*(광역시|도시))|\d+\s*살")


@dataclass
class AgeRef:
    expr: str
    lo: int
    hi: int | None  # None = 이상
    basis: str

    def label(self) -> str:
        return f"{self.lo}세 이상" if self.hi is None else f"{self.lo}~{self.hi}세"


def _find(question: str, word: str) -> int:
    """질문에서 집단 이름의 위치. 시설 이름("어린이집"·"청소년관")이거나
    시설과 함께 나온 말이면 -1"""
    pos = question.find(word)
    if pos < 0 or question[pos + len(word) : pos + len(word) + 1] in ("집", "관"):
        return -1
    if word in FACILITY_SENSITIVE and FACILITY.search(question):
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


AGE_SPANS = [
    (re.compile(r"(\d+)\s*대\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*대"), lambda m: (int(m[1]), int(m[2]) + 9)),
    # "20대 초반" = 20~23세, "중반" = 24~26세, "후반" = 27~29세
    (re.compile(r"(\d+)\s*대\s*초반"), lambda m: (int(m[1]), int(m[1]) + 3)),
    (re.compile(r"(\d+)\s*대\s*중반"), lambda m: (int(m[1]) + 4, int(m[1]) + 6)),
    (re.compile(r"(\d+)\s*대\s*후반"), lambda m: (int(m[1]) + 7, int(m[1]) + 9)),
    (re.compile(r"(\d+)\s*대(?!\s*(광역시|도시))"), lambda m: (int(m[1]), int(m[1]) + 9)),
    (re.compile(r"(\d+)\s*세\s*이상"), lambda m: (int(m[1]), None)),
    (re.compile(r"(\d+)\s*[~\-]\s*(\d+)\s*세"), lambda m: (int(m[1]), int(m[2]))),
    # 미만은 그 나이를 빼고(15세 미만 = 0~14세), 이하는 넣는다(15세 이하 = 0~15세)
    (re.compile(r"(\d+)\s*세\s*미만"), lambda m: (0, int(m[1]) - 1)),
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


def native_to_digits(text: str) -> str:
    """'서른 살' → '30세', '스물다섯 살' → '25세'. 고유어 수가 없으면('몇 살') 그대로"""

    def repl(m: re.Match) -> str:
        if not (m[1] or m[2]):
            return m[0]
        return f"{NATIVE_TENS.get(m[1], 0) + NATIVE_UNITS.get(m[2], 0)}세"

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
    """'20대', '30대 이상', '15~19세', '청소년' 같은 연령 표현 하나 → 구간. 연령 표현이 아니면 None"""
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
