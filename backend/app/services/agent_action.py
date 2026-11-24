"""행동 고르기 (N9): 답 판정이 '다시'를 냈을 때 다음에 무엇을 할지 코드 규칙으로 정한다

판정(answer_judge)은 답이 틀렸는지와 그 이유만 낸다. 틀렸을 때 무엇을 해야 고쳐지는지는 이유와
답이 어디서 왔는지에 따라 다르다. 11/22 실험에서 이유를 보지 않고 모두 '새 검색어로 다시 찾기'를
했더니 정답 표 1위가 42 → 29로 줄었다: 깨뜨린 19건 중 13건은 표는 이미 맞고 칸 · 계산이 막힌
답이었다. 그래서 행동을 여섯으로 나누고 코드가 고른다 (LLM 호출 없음).

  행동      돌아가는 단계     언제
  replan    계산 계획        이유가 형태 · 조건 · 계산 (표는 맞는데 계산 방법이 틀림)
  requery   표 찾기          이유가 대상이고 사전 경로 (사전이 엉뚱한 지표로 받음, 후보가 없음)
  retable   표 고르기        표 찾기 경로, 1위에 ✘가 있거나 검토가 exact가 아님 → 다음 후보
  recell    칸 고르기        표 찾기 경로, 1위 조건 모두 ✔ + 검토 exact인데 칸 · 계산이 막힘
                             (지연님 연결 단계가 들어오면 거기로. 그 전에는 replan으로 떨어짐)
  reroute   경로             사전 경로인데 표 찾기를 아직 안 거침
                             (지금 _recompute의 '통계표 찾기로')
  give_up   —                위 어느 것도 아니거나 상한 초과 → 처음 답 유지 또는 답변 불가 사유

이 모듈은 행동 이름과 근거만 돌려준다. 실제 호출은 ask_session._judged가 한다. 지금 돌아가는
행동은 reroute · retable · give_up이고, replan · requery · recell은 이름만 내고 호출부가
비어 있어 retable로 떨어진다 (재계획 프롬프트 · 연결 단계가 들어오면 채운다).
설계: 인계_2026-11-17/A_설계서.md 4절.
"""

from dataclasses import dataclass, field
from typing import Any

REPLAN, REQUERY, RETABLE, RECELL, REROUTE, GIVE_UP = (
    "replan", "requery", "retable", "recell", "reroute", "give_up",
)  # fmt: skip
ACTIONS = (REPLAN, REQUERY, RETABLE, RECELL, REROUTE, GIVE_UP)
# 판정 이유 → 계산 방법 문제 (표를 바꿔도 안 풀린다)
PLAN_REASONS = ("형태가 다름", "조건이 빠짐", "계산이 빠짐")
MAX_RETRIES = 2  # 질문당 다시 계산 상한 (B 측정: 한 번에 15 ~ 17초)
MAX_LLM_RETRIES = 1  # 그중 replan · requery(Gemma 호출)는 합쳐 1회


@dataclass
class Action:
    name: str
    why: str
    fallback: str | None = None  # 호출부가 없거나 실패하면 떨어질 행동
    detail: dict[str, Any] = field(default_factory=dict)

    def as_trace(self) -> dict[str, Any]:
        return {"action": self.name, "why": self.why, "fallback": self.fallback, **self.detail}


def picked_candidate(found: dict | None) -> dict | None:
    """넘기기 꾸러미에서 탐색이 고른 1위 후보 (없으면 첫 후보)"""
    targets = ((found or {}).get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None
    cands = targets[0].get("candidates") or []
    return next((c for c in cands if c.get("picked")), cands[0] if cands else None)


def top_is_sure(cand: dict | None) -> bool:
    """1위가 '조건 모두 ✔ + 검토 exact'인가 (표는 맞다고 봐도 되는 근거)"""
    if not cand:
        return False
    checks = cand.get("checks") or {}
    return (
        bool(checks) and all(checks.values()) and (cand.get("review") or {}).get("match") == "exact"
    )


def failed_at_cell(trace: dict | None) -> bool:
    """바로 계산 기록에 '칸을 못 고름' · '계산 방법 없음' 종류의 실패가 있는가"""
    for step in (trace or {}).get("dynamic") or []:
        skip = str(step.get("skip") or "")
        if "칸" in skip or "계산" in skip or "맞는" in skip:
            return True
    return False


def choose(
    verdict: dict[str, Any],
    res: dict[str, Any],
    found: dict | None,
    tried: int = 0,
    llm_tried: int = 0,
) -> Action:
    """판정 결과 · 답 · 꾸러미 → 다음 행동.

    tried: 지금까지 다시 계산한 횟수, llm_tried: 그중 LLM 행동 횟수
    """
    if tried >= MAX_RETRIES:
        return Action(GIVE_UP, f"다시 계산 상한({MAX_RETRIES}회)")
    reason = str(verdict.get("reason") or "")
    parsed = res.get("parsed") or {}
    via_search = bool(parsed.get("dynamic_spec"))
    cand = picked_candidate(found)
    llm_ok = llm_tried < MAX_LLM_RETRIES

    if reason in PLAN_REASONS:
        if llm_ok:
            return Action(
                REPLAN,
                f"판정 이유 '{reason}': 계산 계획 문제",
                fallback=RETABLE if via_search else REROUTE,
            )
        return Action(
            RETABLE if via_search else REROUTE, f"'{reason}'이지만 LLM 행동 상한 → 표 · 경로만"
        )

    if reason == "기간이 다름":
        # 연도는 코드 규칙표(timeref)가 세므로 LLM 계획 전에 그 규칙을 다시 태운다.
        # 여기서는 replan으로 넘기되 호출부가 먼저 timeref를 다시 돌리게 표시한다
        if llm_ok:
            return Action(
                REPLAN,
                "판정 이유 '기간이 다름': 연도 규칙 다시 → 안 되면 계획",
                fallback=RETABLE if via_search else REROUTE,
                detail={"rerun_timeref": True},
            )
        return Action(GIVE_UP, "'기간이 다름'인데 LLM 행동 상한")

    if reason == "대상이 다름":
        if not via_search:
            # 사전이 엉뚱한 지표로 받은 경우. 표 찾기를 안 거쳤으면 경로부터 바꾼다
            # (지금 _recompute). 표 찾기까지 거쳤는데도 틀리면 후보에 정답이 없는 것
            # → 검색어 다시 (게이트 통과)
            if found is None:
                return Action(REROUTE, "사전 경로, 표 찾기 안 거침 → 통계표 찾기로")
            if llm_ok:
                return Action(
                    REQUERY, "사전 경로, 표 찾기 후보에도 없음 → 검색어 다시", fallback=RETABLE
                )
            return Action(RETABLE, "'대상이 다름'이지만 LLM 행동 상한 → 다음 후보")
        if top_is_sure(cand) and failed_at_cell(res.get("trace")):
            # 표는 맞는데 칸 · 계산이 막힘. 11/22 실험에서 이 경우에 재검색하면 깨졌다
            return Action(
                RECELL,
                "1위 조건 모두 ✔ · 검토 exact인데 칸 · 계산 실패 → 같은 표에서 칸 다시",
                fallback=REPLAN if llm_ok else RETABLE,
            )
        return Action(RETABLE, "표 찾기 경로, 1위가 확실하지 않음 → 다음 후보")

    if reason == "답할 수 없는 질문":
        return Action(GIVE_UP, "판정 refuse: 답변 불가 사유로")

    return Action(RETABLE if via_search else REROUTE, f"이유 없음('{reason or '-'}') → 기본 행동")
