"""연령 집단 명칭 해석: "노인", "유소년", "청년", "생산가능인구" → 연령 구간 (검색 F-03·QA 공용)

정의가 법령·통계 기준으로 정해져 있으므로 LLM 추측이 아니라 사전으로 푼다. 숫자 구간("65세 이상", "20대")이
질문에 있으면 그쪽이 우선이고(qa.extract_ages / 검색은 items), 이 모듈은 이름으로만 말한 경우를 다룬다.
"청년"처럼 기준이 여럿인 말은 basis 에 적어 답변에서 "19~34세 기준"으로 고지한다.

실행: python -m app.services.agegroup "노인 인구가 많은 동네"
"""

import re
import sys
from dataclasses import dataclass

# (표현들, 시작, 끝(None=이상), 근거). 긴 표현부터 맞춘다
GROUPS = [
    (("생산가능인구", "생산연령인구", "생산가능 인구", "일하는 나이대", "일할 나이"), 15, 64, "생산가능인구 15~64세 (인구총조사 기준)"),
    (("학령인구", "학령 인구"), 6, 21, "학령인구 6~21세 (교육기본통계 기준)"),
    (("영유아",), 0, 5, "영유아 0~5세 (영유아보육법)"),
    (("유소년", "유소년인구"), 0, 14, "유소년인구 0~14세 (인구총조사 기준)"),
    (("어린이", "아동"), 0, 14, "유소년 0~14세로 봄 (아동복지법은 18세 미만)"),
    (("청소년",), 9, 24, "청소년기본법 9~24세"),
    (("청년", "젊은 사람", "젊은이", "젊은층", "젊은 층"), 19, 34, "청년기본법 19~34세 (고용 통계는 15~29세)"),
    (("중장년", "중년"), 40, 64, "중장년 40~64세 (고용노동부 기준)"),
    (("고령자", "고령층", "고령 인구", "고령인구", "노인", "노년층", "노년", "어르신", "노령층", "노령 인구", "노령인구"), 65, None, "고령자 65세 이상 (노인복지법·고령자 통계)"),
    (("미성년", "미성년자"), 0, 18, "민법 19세 미만"),
]
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


def resolve(question: str) -> AgeRef | None:
    """질문에 연령 집단 이름이 있고 숫자 구간이 없으면 그 집단의 구간. 없으면 None"""
    if NUMERIC_AGE.search(question):
        return None
    best = None
    for words, lo, hi, basis in GROUPS:
        for w in words:
            pos = question.find(w)
            if pos >= 0 and question[pos + len(w) : pos + len(w) + 1] in ("집", "관"):  # 어린이집·청소년관은 시설 이름
                continue
            if pos >= 0 and (best is None or pos < best[0] or (pos == best[0] and len(w) > len(best[1].expr))):
                best = (pos, AgeRef(w, lo, hi, basis))
    return best[1] if best else None


if __name__ == "__main__":
    for q in sys.argv[1:] or ["노인 인구가 많은 동네", "청년들이 많이 떠나는 지역", "65세 이상 노인 수"]:
        print(q, "→", resolve(q))
