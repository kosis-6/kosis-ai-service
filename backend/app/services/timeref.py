"""상대시점 해석: "작년", "재작년", "2년 전", "3년 전부터", "올해" → 오늘 날짜 기준 연도
(검색 F-03·QA 공용)

LLM은 오늘이 언제인지 모르고 산수도 틀리므로, 시점 표현은 LLM이 아니라 코드가 푼다.
- 절대시점("2020년")은 slots.correct / qa.correct 의 YEAR 정규식이 먼저 잡고,
  그 경우 상대시점은 보지 않는다
- "작년 대비", "전년보다"처럼 비교 기준으로 쓰인 표현은 시점이 아니다
  (QA 의 PREV_YEAR 가 따로 다룬다)
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
# "N년 전"의 '전'. 지역·낱말의 첫 글자는 빼고 본다 ("최근 5년 전국", "5년 전라남도", "5년 전체")
AGO = r"전(?![국라남북주체])"
# "2년 전", "3년전부터", "5년 전에는". "몇 년 전"은 숫자가 없어 무시
N_YEARS_AGO = re.compile(
    r"(?<![\d~])(\d{1,2})\s*년\s*" + AGO + r"(?!\s*(?:보다|대비|에 비해))(\s*부터)?"
)
# "작년보다", "전년 대비": 비교 기준이지 시점이 아니다
COMPARE_BASE = re.compile(
    r"(재작년|작년|전년|지난해|올해|금년|\d{1,2}\s*년\s*" + AGO + r")\s*(이랑|하고|랑|과|와)?"
    r"\s*(보다|대비|에 비해|에비해|비교)"
)
# 이어지는 기간: "작년부터", "2년 전부터" → 시작만 정하고 끝은 최신
FROM = re.compile(r"(부터|이후|이래)")
# "최근 5년", "지난 10년간", "과거 10년 동안", "최근 3개년 동안"
# → 자료 마지막 해를 포함한 N개 해 (끝이 열린 기간. recent_period).
# 두 자리 연도 규칙이 '최근 10년'을 2010년으로 읽던 문제도 이 표현을 먼저 걷어 내 막는다
# (slots.short_years)
RECENT_N_YEARS = re.compile(
    r"(최근|최신|지난|요즘|과거)\s*(\d{1,2})\s*(?:개년|년)(?!\s*" + AGO + r")"
    r"(?:\s*(?:간|동안|사이))?"
)


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


def data_end(today: date | None = None) -> int:
    """자료(연간)의 마지막 해. 읽지 못하면 작년"""
    try:
        from app.services import indicators  # 순환 가져오기를 피한다

        return indicators._data_years()[1]
    except Exception:
        return (today or date.today()).year - 1


def recent_period(
    question: str, today: date | None = None, end: int | None = None
) -> TimeRef | None:
    """'최근 5년' → 자료 마지막 해를 포함한 5개 해의 첫해부터 최신까지 (2025년까지면 2021년부터).
    질문 이해 · 통계표 찾기 · 계획이 같은 기준으로 센다 (period 와 같은 규칙).
    전에는 여기만 '올해 − N'이라 자료 마지막 해가 작년이 아니면 계산 단계와 한 해 어긋났다"""
    if m := RECENT_N_YEARS.search(question):
        end = end if end is not None else data_end(today)
        return TimeRef(m.group(0).strip(), end - int(m.group(2)) + 1, open_end=True)
    return None


# ---- 상대 기간 규칙표: 표현 찾기(find) · 종류 → 연도(period). LLM 이 센 연도는 쓰지 않는다 ----
# 기준은 팀 골든셋 (11/22 세진 확인):
#   word         작년 · 재작년 · 올해 · 내년     오늘 기준 한 해
#   ago          N년 전 (부터)                  오늘 − N (부터면 그 해부터 최신까지)
#   later        N년 뒤 · 후                    오늘 + N
#   recent       최근 · 지난 N년                자료 마지막 해를 포함한 N개 해
#   span         N년 동안 · N년간               최근 N년과 같다 ('10년 동안' = 2016~2025)
#   compare_ago  N년 전보다 · N년 전과 비교     자료 마지막 해 − N 과 마지막 해
#   span_from    2010년부터 N년 동안            그 해부터 그 해 + N
#   until_now    지금까지 · 현재까지            끝 = 자료 마지막 해
# '자료 마지막 해까지'는 end 를 None 으로 둔다: 계산 단계가 표마다 그 표의 마지막 해를 쓴다
KINDS = ("word", "ago", "later", "recent", "span", "compare_ago", "span_from", "until_now")
N_KINDS = ("ago", "later", "recent", "span", "compare_ago", "span_from")
_N = r"(?<![\d~])(\d{1,2})\s*"
SPAN_FROM = re.compile(r"((?:19|20)\d{2})\s*년\s*부터\s*(\d{1,2})\s*(?:개년|년)\s*(?:간|동안)")
COMPARE_AGO = re.compile(
    _N + r"년\s*" + AGO + r"\s*(?:이랑|하고|랑|과|와)?\s*(?:보다|대비|에\s*비해|비교)"
)
SPAN_N = re.compile(_N + r"(?:개년|년)\s*(?:간|동안)")
LATER_N = re.compile(_N + r"년\s*(?:뒤|후)(?![반기])")
UNTIL_NOW = re.compile(r"(?:지금|현재|요즘|최근)\s*까지")
# 낱말 뒤에 붙으면 비교 기준 ("작년보다", "전년 대비"): 시점이 아니다 (QA 의 PREV_YEAR 가 다룬다)
WORD_AS_BASE = re.compile(r"\s*(?:보다|대비|에\s*비해)")


def find(question: str) -> list[dict]:
    """질문의 상대 기간 표현 [{expr, kind, n, anchor?, offset?, open?}]
    (긴 모양부터, 겹치지 않게)"""
    text, out = question, []

    def take(pattern: re.Pattern, kind: str, n_group: int | None, **extra) -> None:
        nonlocal text
        for m in pattern.finditer(text):
            ref = {"expr": m.group(0).strip(), "kind": kind}
            ref["n"] = int(m.group(n_group)) if n_group else None
            ref.update({k: (v(m) if callable(v) else v) for k, v in extra.items()})
            out.append(ref)
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)

    take(SPAN_FROM, "span_from", 2, anchor=lambda m: int(m.group(1)))
    take(RECENT_N_YEARS, "recent", 2)
    take(COMPARE_AGO, "compare_ago", 1)
    take(SPAN_N, "span", 1)
    take(LATER_N, "later", 1)
    take(N_YEARS_AGO, "ago", 1, open=lambda m: bool(m.group(2)))
    take(UNTIL_NOW, "until_now", None)
    for word, offset in WORDS:
        pos = text.find(word)
        if pos < 0:
            continue
        tail = text[pos + len(word) :]
        text = text.replace(word, " " * len(word))
        if WORD_AS_BASE.match(tail):
            continue
        out.append(
            {
                "expr": word,
                "kind": "word",
                "n": None,
                "offset": offset,
                "open": bool(FROM.match(tail[:3].strip())),
            }
        )
    return out


def period(ref: dict, today: date, end: int) -> dict:
    """상대 기간 표현 하나 → {year, start, end} (해당 없는 칸은 없다). end: 자료 마지막 해.
    end 가 None 이면 '자료 마지막 해까지'"""
    kind, n = ref["kind"], ref.get("n") or 0
    if kind == "word":
        year = today.year + ref.get("offset", 0)
        return {"start": year, "end": None} if ref.get("open") else {"year": year}
    if kind == "ago":
        year = today.year - n
        return {"start": year, "end": None} if ref.get("open") else {"year": year}
    if kind == "later":
        return {"year": today.year + n}
    if kind in ("recent", "span"):
        return {"start": end - n + 1, "end": None}
    if kind == "compare_ago":
        return {"start": end - n, "end": None}
    if kind == "span_from":
        return {"start": ref["anchor"], "end": ref["anchor"] + n}
    if kind == "until_now":
        return {"end": None}
    return {}


if __name__ == "__main__":
    for q in sys.argv[1:] or ["작년 출생아 수", "3년 전부터 인구 변화", "작년보다 인구가 늘었어?"]:
        print(q, "→", resolve(q))
