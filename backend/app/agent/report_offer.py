"""보고서 제안 칩 (명세서 4.3): "지금까지 본 ○○ 통계로 보고서를 만들까요?"

코드 규칙으로만 띄운다 (답마다 LLM을 부르지 않는다).
  - 로그인 사용자 · 이 답이 보고서에 담을 수 있는 답(reportable)
  - 같은 대화에서 같은 지표의 담을 수 있는 답이 MIN_SAME 개째
  - 이 대화에서 제안한 적이 없고, 거절한 적도 없음
수락 · 거절은 다음 말에서 라우터(report_router, offer_turn)가 에이전트로 보낸다.
"""

import re

from app.services import report_compare

MIN_SAME = 3
# 주제를 볼 때 떼는 것: 괄호 안 (집계 대상), 연령 · 성별 수식어
# "70세 이상 1인가구(20세이상 인구) 수" · "여성 1인가구" · "1인 가구 수"
# → 모두 "1인가구" (개발셋 R01)
PAREN = re.compile(r"\([^)]*\)")
QUALIFIER = re.compile(
    r"\d+\s*(?:~\s*\d+\s*)?세\s*(?:이상|이하|미만|초과)?|\d+대|여성|남성|여자|남자"
)


def _topic(fig: dict) -> str:
    name = QUALIFIER.sub("", PAREN.sub("", fig.get("indicator") or ""))
    return report_compare._norm(name)


def update(state: dict | None, res: dict, after: dict, user_id: int | None) -> None:
    """after["report_agent"] 에 본 지표를 쌓고, 조건이 맞으면 res["report_offer"] 를 붙인다"""
    if user_id is None or not res.get("reportable") or not res.get("figure"):
        return
    before = (state or {}).get("report_agent") or {}
    mine = {**before, **(after.get("report_agent") or {})}
    fig = res["figure"]
    seen = [
        *(mine.get("seen") or []),
        {"topic": _topic(fig), "region": (fig.get("conditions") or {}).get("region")},
    ]
    mine["seen"] = seen[-20:]
    same = [s for s in seen if s["topic"] == _topic(fig)]
    if len(same) >= MIN_SAME and not mine.get("offered") and not mine.get("declined"):
        regions = {s["region"] for s in same}
        region = next(iter(regions)) if len(regions) == 1 else None
        name = fig.get("indicator") or "같은 주제"
        res["report_offer"] = {
            "topic": f"{region} {name}" if region else name,
            "count": len(same),
        }
        mine["offered"] = True
        mine["offer_turn"] = after.get("turn_no")
        mine["offer_topic"] = res["report_offer"]["topic"]
    after["report_agent"] = mine
