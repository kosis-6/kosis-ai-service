"""키워드 검색어 만들기: 질문 원문 → 군더더기 말 지우기 → 일상어 사전으로 통계 용어 덧붙이기 (F-15)

HCX가 요약한 topic·target에서 단어를 뽑으면 지역·분류 단어('시군구별', '연령대별')가 빠지고,
'돌싱'·'혼자 사는 집' 같은 일상어는 표 이름에 없어 키워드가 표와 만나지 못한다.
그래서 키워드 검색어는 질문 원문에서 만들고, 사전으로 통계 용어를 질문 뒤에 덧붙인다 (치환이 아님).

  "돌싱이 몇 명인지 알려 줘" → clean → "돌싱이" → expand → "돌싱이 재혼 이혼 혼인상태 …"

사전 config/synonyms.json = {일상 표현: 통계 용어(공백으로 여러 개)}, 2,965항목.
  - 팀원 실험실(kosis-search-lab src/synonyms.py)의 주제별 사전 + 손으로 쓴 157항목,
    한 글자 키·오발화 항목 제거 (2026-10-31 최세진 전달, synonyms_all.json)
  - 손으로 쓴 항목은 개발셋·팀원셋 오답을 보고 넣은 것이라 그 두 셋 수치는 낙관적이다.
    새 질문에서 엉뚱하게 걸리는 항목은 이 파일에서 빼면 된다
  - 키는 질문의 부분 문자열로 맞춘다. 한 글자 키는 넣지 않는다 ('배' → 선박이 '배우자'에 걸림)

실행: python -m app.services.synonyms "결혼 안 하고 낳은 아기가 1년에 몇 명인지"
"""

import json
import re
import sys
from functools import cache
from pathlib import Path

SYNONYMS_PATH = Path(__file__).resolve().parents[2] / "config" / "synonyms.json"

# 검색에 방해되는 말 (실험실 정규식 그대로). 부분 문자열로 지우므로 단어 안에서도 지워진다
FILLER = re.compile(
    r"통계표|통계|자료|데이터|현황|찾아 줘|알려 줘|알고 싶어|보여 줘|볼 수 있는|있는 표|"
    r"얼마나|몇 명|몇|되나요|하나요|인지|는지|어때|궁금|사람들|사람|퍼센트|비율이|정도|"
    r"어떻게|어디|어느|어떤|제일|가장|보통|요즘|경우|우리|되나|있나|되는|있는|"
    r"다른지|많은지|늘어나는지|하는지|싶어|해마다|1년에|얼마|쯤|나라"
)


@cache
def load(path: Path = SYNONYMS_PATH) -> dict[str, str]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def clean_query(question: str) -> str:
    """군더더기 말을 공백으로 바꾸고 공백을 하나로"""
    return " ".join(FILLER.sub(" ", question).split())


def expand(q: str, table: dict[str, str] | None = None) -> str:
    """질문에 들어 있는 표현을 긴 것부터 찾아 통계 용어를 덧붙인다.
    긴 표현이 맞으면 그 자리를 비워 짧은 표현이 겹쳐 맞지 않게 한다 ('혼자 사는' 뒤 '사는')"""
    table = load() if table is None else table
    rest = q
    extra: list[str] = []
    for phrase in sorted(table, key=len, reverse=True):
        if phrase in rest:
            extra.append(table[phrase])
            rest = rest.replace(phrase, " ")
    terms = list(dict.fromkeys(tok for e in extra for tok in e.split()))
    return q + (" " + " ".join(terms) if terms else "")


def keyword_text(question: str, table: dict[str, str] | None = None) -> str:
    """키워드 검색어: 원문 정리 → 사전 확장"""
    return expand(clean_query(question), table)


if __name__ == "__main__":
    for q in sys.argv[1:] or ["결혼 안 하고 낳은 아기가 1년에 몇 명인지"]:
        print(f"{q}\n  → {keyword_text(q)}")
