"""상대시점 해석: "작년", "재작년", "2년 전", "3년 전부터", "올해" → 오늘 날짜 기준 연도 (검색 F-03·QA 공용)

LLM은 오늘이 언제인지 모르고 산수도 틀리므로, 시점 표현은 LLM이 아니라 코드가 푼다.
- 절대시점("2020년")은 slots.correct / qa.correct 의 YEAR 정규식이 먼저 잡고, 그 경우 상대시점은 보지 않는다
- "작년 대비", "전년보다"처럼 비교 기준으로 쓰인 표현은 시점이 아니다 (QA 의 PREV_YEAR 가 따로 다룬다)
- '10년 전 대비', '20년간', '90년생'은 연도가 아니다 (slots.SHORT_YEAR 와 같은 원칙)

실행: python -m app.services.timeref "작년 출생아 수"
"""

import re
import sys
from dataclasses import dataclass
from datetime import date

# 긴 표현부터 찾는다 ("재작년" 안에 "작년", "내후년" 안에 "후년"이 있음)
WORDS = [
    ("재작년", -2),
    ("지지난해", -2),
    ("지난해", -1),
    ("작년", -1),
    ("전년도", -1),
    ("올해", 0),
    ("금년", 0),
    ("이번 해", 0),
    ("내후년", 2),
    ("내년", 1),
]
# "2년 전", "3년전부터", "5년 전에는". "몇 년 전"은 숫자가 없어 무시
N_YEARS_AGO = re.compile(r"(?<![\d~])(\d{1,2})\s*년\s*전(?!\s*(?:보다|대비|에 비해))(\s*부터)?")
# "작년보다", "전년 대비": 비교 기준이지 시점이 아니다
COMPARE_BASE = re.compile(
    r"(재작년|작년|전년|지난해|올해|금년|\d{1,2}\s*년\s*전)\s*(이랑|하고|랑|과|와)?\s*(보다|대비|에 비해|에비해|비교)"
)
# 이어지는 기간: "작년부터", "2년 전부터" → 시작만 정하고 끝은 최신
FROM = re.compile(r"(부터|이후|이래)")


@dataclass
class TimeRef:
    expr: str  # 질문에 쓰인 표현 ("작년", "2년 전부터")
    year: int  # 오늘 날짜로 푼 연도
    open_end: bool = False  # True면 그 해부터 최신까지 ("3년 전부터")

    def describe(self, today: date) -> str:
        return f"'{self.expr}' = {self.year}년 ({today.year}년 기준)"


def resolve(question: str, today: date | None = None) -> TimeRef | None:
    """질문의 상대시점 표현 하나를 오늘 기준 연도로 푼다. 없거나 비교 기준으로만 쓰였으면 None"""
    today = today or date.today()
    # "올해 유학생이 작년보다 늘었는지": 비교 기준("작년보다")만 지우고 남은 시점("올해")을 찾는다
    question = COMPARE_BASE.sub(" ", question)
    if m := N_YEARS_AGO.search(question):
        expr = m.group(0).strip()
        return TimeRef(expr, today.year - int(m.group(1)), open_end=bool(m.group(2)))
    for word, offset in WORDS:
        if (pos := question.find(word)) >= 0:
            tail = question[pos + len(word) : pos + len(word) + 3]
            return TimeRef(word, today.year + offset, open_end=bool(FROM.match(tail.strip())))
    return None


if __name__ == "__main__":
    for q in sys.argv[1:] or ["작년 출생아 수", "3년 전부터 인구 변화", "작년보다 인구가 늘었어?"]:
        print(q, "→", resolve(q))
