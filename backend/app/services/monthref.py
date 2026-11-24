"""달 범위 읽기: "1월부터 3월까지", "1~3월", "상반기", "2분기" → (시작 달, 끝 달)

연도(timeref)와 같이 LLM이 아니라 코드가 읽는다. 달 하나("10월 인구")는 아직 읽지 않는다:
주민등록인구의 '최신 달' 답(indicators._month_values)과 섞이고, "10월 말 기준"처럼 시점을 말하는
경우와 구분이 어렵다. 묶음(기간)으로 말한 달만 본다.

실행: python -m app.services.monthref "작년 1월부터 3월까지 사망자"
"""

import re
import sys

M = r"(1[0-2]|[1-9])\s*월"
RANGE = re.compile(M + r"\s*(?:부터|에서|~|-|–|에서부터)\s*" + M + r"\s*(?:까지|사이|동안)?")
SHORT = re.compile(r"(1[0-2]|[1-9])\s*[~\-–]\s*(1[0-2]|[1-9])\s*월")
HALF = re.compile(r"(상|하)반기")
QUARTER = re.compile(r"([1-4])\s*분기")


def find(question: str) -> dict | None:
    """{"m0": 시작 달, "m1": 끝 달, "expr": 질문의 표현} 또는 None. 시작이 끝보다 뒤면 안 읽는다"""
    for pattern in (RANGE, SHORT):
        if m := pattern.search(question):
            m0, m1 = int(m.group(1)), int(m.group(2))
            if m0 <= m1:
                return {"m0": m0, "m1": m1, "expr": m.group(0).strip()}
    if m := HALF.search(question):
        return {
            "m0": 1 if m.group(1) == "상" else 7,
            "m1": 6 if m.group(1) == "상" else 12,
            "expr": m.group(0),
        }
    if m := QUARTER.search(question):
        q = int(m.group(1))
        return {"m0": 3 * q - 2, "m1": 3 * q, "expr": m.group(0)}
    return None


def label(m0: int, m1: int) -> str:
    return f"{m0}월" if m0 == m1 else f"{m0}~{m1}월"


if __name__ == "__main__":
    print(find(" ".join(sys.argv[1:])))
