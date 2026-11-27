"""행동 고르기 (N9): 답이 틀렸거나(판정 '다시') 값을 못 냈을 때 다음 행동을 코드 규칙으로 정한다

설계: docs/agent_redesign_2026-11-26.md 2-2 · 2-4 · 3-3. 행동은 **다시 하는 단계** 기준 넷이고,
좁은 것부터 쓴다 (repick 칸 → retable 표 → replan 계획 · 검색어 → give_up).

  행동      다시 하는 단계   하는 일 (ask_session)                               LLM
  repick    칸               같은 표(사전 표 포함)에서 칸 잇기 → 지적 넘긴 recell  recell 1회(따로)
  retable   표               쓴 표를 빼고 다른 표로 (_retable). 꾸러미가 있으면    없음
                             남은 후보, 없으면 원래 질문으로 찾음. 검색어는 그대로
  replan    계획 · 검색어    Gemma 가 계획을 고쳐 새로 찾고 계산 (_replan_compute)  Gemma 1회
  give_up   —                _loop_end                                            —

옛 행동 reroute 는 retable 에, requery 는 replan 에 합쳤다.

규칙 (3-3). '칸 있음'은 검사(table_check)의 differs · cells_exist, '맞음'은 kind_ok · topic_ok:

  판정 이유 · 실패                    검사                     행동      안 되면
  조건이 빠짐                         칸 있음                  repick    retable
  조건이 빠짐                         칸 없음                  retable   replan
  대상이 다름                         칸 있음 · 값 종류 · 주제  repick    retable
  대상이 다름                         그 밖                    retable   replan
  계산이 빠짐 · 형태가 다름 · 기간이 다름  —                    replan    retable
  값 없음: 칸 일부만 이어짐            칸 있음                  repick    retable
  값 없음: 칸 일부만 이어짐            칸 없음                  retable   replan
  값 없음: 후보 모두 조건 ✘            —                        replan    give_up
  답할 수 없는 질문 · 한도 초과        —                        give_up   —

같은 행동은 두 번 쓰지 않는다. 앞 행동이 다 쓰였으면 사다리의 남은 행동으로 간다
(retable 뒤에도 대상이 다름 → replan). 이 모듈은 행동 이름과 근거만 돌려준다 (LLM 호출 없음).
"""

from dataclasses import dataclass, field
from typing import Any

REPICK, RETABLE, REPLAN, GIVE_UP = "repick", "retable", "replan", "give_up"
ACTIONS = (REPICK, RETABLE, REPLAN, GIVE_UP)
LADDER = (REPICK, RETABLE, REPLAN)  # 좁은 것부터

# 판정 이유 (app/prompts/answer_judge.py REASONS)
MISSING, TARGET, UNANSWERABLE = "조건이 빠짐", "대상이 다름", "답할 수 없는 질문"
PLAN_REASONS = ("계산이 빠짐", "형태가 다름", "기간이 다름")
# 값 없는 실패 (판정 없이 코드가 실패 기록으로 낸 이유, ask_session.failed_retryable)
NO_VALUE_CELLS = "값 없음: 칸 일부만 이어짐"
NO_VALUE_CHECKS = "값 없음: 후보 모두 조건 ✘"
NO_VALUE = (NO_VALUE_CELLS, NO_VALUE_CHECKS)
# 이 이유일 때만 '칸 있음'을 본다 (검사를 돌린다)
REPICK_REASONS = (MISSING, TARGET, NO_VALUE_CELLS)

MAX_RETRIES = 2  # 질문당 다시 계산 상한 (B 측정: 한 번에 15 ~ 17초). 대체 행동은 같은 회에 든다
MAX_LLM_RETRIES = 1  # 그중 replan(Gemma 호출)은 1회
# repick 의 recell(HCX)은 위 한도와 따로 센다: repick 은 질문당 한 번이라 recell 도 1회.
# 표가 틀린 답에 repick 이 recell 을 써도 replan 기회가 남는다 (11/27 결정)
MAX_SECONDS = 60  # 판정 시작부터 (설계서 4-5). 넘으면 지금까지 얻은 것으로 끝낸다
LLM_ACTIONS = (REPLAN,)


@dataclass
class Action:
    name: str
    why: str
    fallback: str | None = None  # 이 행동이 다시 계산하지 못하면 같은 회에 이어 할 행동
    detail: dict[str, Any] = field(default_factory=dict)

    def as_trace(self) -> dict[str, Any]:
        return {"action": self.name, "why": self.why, "fallback": self.fallback, **self.detail}


def picked_candidate(found: dict | None) -> dict | None:
    """넘기기 꾸러미에서 탐색이 고른 1위 후보 (없으면 첫 후보). 지표가 여럿이면 None"""
    targets = ((found or {}).get("handoff") or {}).get("targets") or []
    if len(targets) != 1:
        return None
    cands = targets[0].get("candidates") or []
    return next((c for c in cands if c.get("picked")), cands[0] if cands else None)


def failed_at_cell(trace: dict | None) -> bool:
    """바로 계산 기록에 '칸을 못 고름' · '계산 방법 없음' 종류의 실패가 있는가"""
    for step in (trace or {}).get("dynamic") or []:
        skip = str(step.get("skip") or "")
        if "칸" in skip or "계산" in skip or "맞는" in skip:
            return True
    return False


def _rule(reason: str, cells: bool, fits: bool) -> tuple[tuple[str, ...], str]:
    """판정 이유 · 실패 × 검사 → (행동 순서, 근거). 3-3 표"""
    if reason == MISSING:
        if cells:
            return (REPICK, RETABLE), f"'{reason}' + 그 표에 고칠 칸 있음 → 같은 표에서 칸 다시"
        return (RETABLE, REPLAN), f"'{reason}'인데 그 표에 고칠 칸 없음 → 다른 표"
    if reason == TARGET:
        if cells and fits:
            return (REPICK, RETABLE), f"'{reason}' + 칸 있음 · 값 종류 · 주제 맞음 → 칸 다시"
        return (RETABLE, REPLAN), f"'{reason}' → 다른 표"
    if reason in PLAN_REASONS:
        return (REPLAN, RETABLE), f"'{reason}': 계산 계획 문제 → 계획 다시"
    if reason == NO_VALUE_CELLS:
        if cells:
            return (REPICK, RETABLE), "값 없음 · 칸이 일부만 이어짐 + 칸 있음 → 칸 다시"
        return (RETABLE, REPLAN), "값 없음 · 칸이 일부만 이어짐 + 칸 없음 → 다른 표"
    if reason == NO_VALUE_CHECKS:
        return (REPLAN, GIVE_UP), "값 없음 · 후보 모두 조건 ✘ → 계획 · 검색어 다시"
    return (RETABLE, REPLAN), f"이유 없음('{reason or '-'}') → 다른 표"


def choose(
    verdict: dict[str, Any],
    tried: int = 0,
    llm_tried: int = 0,
    done: frozenset[str] = frozenset(),
    *,
    cells: bool = False,
    fits: bool = True,
    repick_ok: bool = True,
) -> Action:
    """판정(또는 값 없는 실패) 이유 · 검사 → 다음 행동.

    tried: 지금까지 다시 계산한 횟수, llm_tried: 그중 replan 횟수, done: 이미 쓴 행동
    cells: 다시 볼 표에 고칠 칸이 있나, fits: 값 종류 · 주제가 맞나,
    repick_ok: False 면 repick 을 고르지 않는다 (여러 지표 · 복합 답, 계산이 필요한 지적).
    셋 다 ask_session.repick_cells (검사 table_check)
    """
    reason = str(verdict.get("reason") or "")
    if reason == UNANSWERABLE:
        return Action(GIVE_UP, "판정 refuse: 답변 불가 사유로")
    if tried >= MAX_RETRIES:
        return Action(GIVE_UP, f"다시 계산 상한({MAX_RETRIES}회)")
    order, why = _rule(reason, cells and repick_ok, fits)
    if GIVE_UP not in order:
        # 규칙의 두 행동이 다 쓰였으면 사다리의 남은 행동으로 (retable 뒤에도 대상이 다름 → replan)
        order = order + tuple(a for a in (RETABLE, REPLAN) if a not in order)

    def usable(name: str) -> bool:
        if name == GIVE_UP:
            return True
        if name in done:
            return False
        if name == REPLAN and llm_tried >= MAX_LLM_RETRIES:
            return False
        return not (name == REPICK and not (cells and repick_ok))

    left = [a for a in order if usable(a)]
    if not left or left[0] == GIVE_UP:
        gone = [a for a in order if a != GIVE_UP]
        return Action(GIVE_UP, f"{why} · 쓸 행동이 없음 (이미 함 · 상한: {' · '.join(gone)})")
    name = left[0]
    if name != order[0]:
        why = f"{why} · {order[0]}은 이미 함 · 상한 → {name}"
    nxt = next((a for a in left[1:] if a != GIVE_UP), None)
    detail: dict[str, Any] = {}
    if name == REPICK:
        detail["llm"] = True
    if reason == "기간이 다름" and name == REPLAN:
        detail["rerun_timeref"] = True
    return Action(name, why, fallback=nxt, detail=detail)
