"""보고서 경로 나누기 (명세서 7.2): 이 말을 보고서 에이전트로 보낼지 코드 규칙으로 정한다

LLM을 부르지 않는다. 통계 질문을 잘못 가로채면 답이 사라지므로, 확실한 말만 보고서로 보낸다.

  1. 에이전트가 되물은 뒤의 답 (state.report_agent.pending)
  2. 제안 칩을 받은 직후의 수락 · 거절 (state.report_agent.offer)
  3. "보고서"라는 말
  4. "담다" (담아 · 담기 · 담을 …): 통계 질문에는 거의 쓰지 않는다
  5. "그거 · 이것도 · 2015년 것도 넣어": 가리키는 말 + 넣다 ("서울도 넣어서 비교"는 통계 질문)
  6. 바로 앞 말이 보고서 경로였고 보고서를 고치는 말
     (빼 · 넣 · 남겨 · 만들 · 아니 · 순서 · 짧게 · N번째 …)
"""

import re

REPORT_WORD = re.compile(r"보고서|리포트")
PUT_IN = re.compile(r"담(아|기|을|고|는|았|게|겠)")
POINTED_PUT = re.compile(
    r"(그거|그것|이거|이것|저거|저것|(?:[가-힣\d]+\s*)?(?:것|거))\s*(도|을|를|만|까지)?\s*(같이\s*)?넣"
)
EDIT = re.compile(
    r"빼|넣|남겨|남기|만들|^아니|순서|맨\s*(앞|뒤|위|아래)|짧게|길게|간단히|결론|다시\s*(써|정리)"
    r"|써\s*줘|\d+\s*번째|첫\s*번째|두\s*번째|세\s*번째|마지막\s*(거|것)"
)
YES = re.compile(r"^(응|네|예|그래|좋아|만들어|ㅇㅇ|ok|오케이)")
NO = re.compile(r"(괜찮|됐어|안\s*해|아니|싫|나중에|필요\s*없)")
STAT_REQUEST = re.compile(r"알려|구해|조회|계산|보여|몇|얼마|[?？]")


def agent_state(state: dict | None) -> dict:
    return (state or {}).get("report_agent") or {}


def wants(question: str, state: dict | None, *, standalone_stat: bool = False) -> bool:
    q = " ".join(question.split())
    mine = agent_state(state)
    turn_no = (state or {}).get("turn_no") or 0
    if REPORT_WORD.search(q) or PUT_IN.search(q) or POINTED_PUT.search(q):
        return True
    if standalone_stat:
        return False
    if mine.get("pending"):
        return True
    if mine.get("offer_turn") == turn_no and (YES.search(q) or NO.search(q)):
        return True
    return mine.get("last_turn") == turn_no and bool(EDIT.search(q))
