"""계산 계획의 steps (A_20 ②): JSON 스키마 · 코드 검증 · 지금 combine 과의 호환 변환.

계획기(LLM)가 내는 steps 는 plan_exec 의 연산 6개 순서다. 여기서는 LLM 없이
- schema(): 계획 스키마에 steps · answer 칸을 더한 것 (복합 질문일 때만 이 스키마로 부른다)
- normalize(): LLM 출력의 steps 를 정리하고 plan_exec.validate 로 검증 → (steps, 틀린 점)
- from_combine(): 지금 계획(combine.op 하나)을 같은 뜻의 steps 로 바꾼다 (호환)"""

from __future__ import annotations

import copy
import re
from typing import Any

from app.services import plan_exec as pe

YEAR_EXPR = re.compile(r"^(?:(?:19|20)\d{2}|latest(?:-\d{1,2})?)$")
NULL_OR = lambda t: {"anyOf": [{"type": "null"}, t]}  # noqa: E731

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": list(pe.OPS)},
        # get
        "indicator": NULL_OR({"type": "integer"}),
        "regions": NULL_OR(
            {
                "anyOf": [
                    {"type": "string", "enum": ["all"]},
                    {"type": "array", "items": {"type": "string"}},
                    {
                        "type": "object",
                        "properties": {"from_step": {"type": "integer"}},
                        "required": ["from_step"],
                    },
                ]
            }
        ),
        "years": NULL_OR({"type": "array", "items": {"type": "string"}, "maxItems": 2}),
        # change · rank · filter · pick
        "of": NULL_OR({"type": "integer"}),
        "from": NULL_OR({"type": "string"}),
        "to": NULL_OR({"type": "string"}),
        "how": NULL_OR({"type": "string", "enum": sorted({*pe.CHANGE_HOW, *pe.COMBINE_HOW})}),
        # combine
        "a": NULL_OR({"type": "integer"}),
        "b": NULL_OR({"type": "integer"}),
        "scale": NULL_OR({"type": "number"}),
        # rank
        "top": NULL_OR({"type": "integer"}),
        "top_pct": NULL_OR({"type": "number"}),
        "ascending": NULL_OR({"type": "boolean"}),
        # filter: 조건 하나만. 값은 숫자(ge · gt · le · lt · top_pct · bottom_pct) 또는
        # 비교 기준 지역 이름(above · below, 보통 "전국")
        "cond": NULL_OR(
            {
                "type": "object",
                "properties": {
                    k: {"anyOf": [{"type": "number"}, {"type": "string"}]} for k in pe.FILTER_KEYS
                },
            }
        ),
        "why": {"type": "string"},
    },
    "required": ["op", "why"],
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {"type": "integer"},
        "with": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["step", "with"],
}


def schema(base: dict[str, Any]) -> dict[str, Any]:
    """계획 스키마(prompts.plan.SCHEMA · replan.SCHEMA)에 steps · answer 를 더한다"""
    s = copy.deepcopy(base)
    s["properties"]["steps"] = {"type": "array", "items": STEP_SCHEMA, "maxItems": pe.MAX_STEPS}
    s["properties"]["answer"] = ANSWER_SCHEMA
    s["required"] = list(dict.fromkeys([*s.get("required", []), "steps", "answer"]))
    return s


# ---------------------------------------------------------------- 정리 · 검증
_KEYS = {
    "get": ("indicator", "regions", "years"),
    "change": ("of", "from", "to", "how"),
    "combine": ("a", "b", "how", "scale"),
    "rank": ("of", "top", "top_pct", "ascending"),
    "filter": ("of", "cond"),
    "pick": ("of", "regions"),
}


def _clean(step: dict[str, Any]) -> dict[str, Any]:
    """연산에 쓰는 칸만 남기고 null 은 뺀다 (LLM 이 스키마 때문에 모든 칸을 채워 보낸다)"""
    op = step.get("op")
    out: dict[str, Any] = {"op": op}
    for k in _KEYS.get(op, ()):
        v = step.get(k)
        if v is None:
            continue
        if k == "cond" and isinstance(v, dict):
            v = {ck: cv for ck, cv in v.items() if cv is not None}
        out[k] = v
    if step.get("why"):
        out["why"] = str(step["why"])[:120]
    if op == "get" and "regions" not in out:
        out["regions"] = "all"
    if op == "change" and "how" not in out:
        out["how"] = "diff"
    return out


def normalize(
    raw: Any, n_indicators: int, answer: Any = None
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """LLM 출력의 steps → (정리한 steps, 틀린 점). 틀린 점이 있으면 steps 는 None"""
    if not isinstance(raw, list):
        return None, ["steps 가 목록이 아님"]
    steps = [_clean(s) for s in raw if isinstance(s, dict)]
    problems = pe.validate(steps, n_indicators)
    for i, s in enumerate(steps):
        if s["op"] == "get":
            bad = [y for y in s.get("years") or [] if not YEAR_EXPR.match(str(y))]
            if bad:
                problems.append(f"step {i}: 연도는 숫자 연도 또는 latest · latest-N 만 ({bad})")
        if s["op"] == "change":
            for k in ("from", "to"):
                if k in s and not YEAR_EXPR.match(str(s[k])):
                    problems.append(f"step {i}: {k} 는 숫자 연도 또는 latest · latest-N 만")
        if s["op"] == "filter" and isinstance(s.get("cond"), dict) and s["cond"]:
            key, val = next(iter(s["cond"].items()))
            if key in ("above", "below") and not isinstance(val, str):
                problems.append(f"step {i}: {key} 의 값은 비교 기준 지역 이름")
            if key not in ("above", "below") and not isinstance(val, int | float):
                problems.append(f"step {i}: {key} 의 값은 숫자")
    if isinstance(answer, dict):
        k = answer.get("step")
        if not (isinstance(k, int) and 0 <= k < len(steps)):
            problems.append(f"answer.step {k!r} 가 없음")
        for w in answer.get("with") or []:
            if not (isinstance(w, int) and 0 <= w < len(steps)):
                problems.append(f"answer.with {w!r} 가 없음")
    return (None, problems) if problems else (steps, [])


# ---------------------------------------------------------------- 호환 변환
def _index(plan: dict[str, Any], ref: Any, live: list[int]) -> int | None:
    """combine.num · den: 처음 계획은 span(문자열), 재계획은 번호"""
    inds = plan.get("indicators") or []
    if isinstance(ref, int):
        return ref if ref in live else None
    if isinstance(ref, str):
        for i in live:
            if inds[i].get("span") == ref:
                return i
    return None


def _get(i: int, years: list[str], regions: Any = "all") -> dict[str, Any]:
    return {"op": "get", "indicator": i, "regions": regions, "years": years}


def from_combine(
    plan: dict[str, Any], years: list[str] | None = None
) -> list[dict[str, Any]] | None:
    """지금 계획(combine.op 하나)을 같은 뜻의 steps 로. 못 바꾸는 op(not_computable 등)는 None.
    years 는 질문의 시점 표현(없으면 ['latest'])"""
    c = plan.get("combine") or {}
    op = c.get("op") or "none"
    inds = plan.get("indicators") or []
    live = [i for i, ind in enumerate(inds) if ind.get("scope") != "out"]
    if not live:
        return None
    ys = list(years or ["latest"])
    if op in ("none", "compare"):
        return [_get(i, ys) for i in live]
    if op in ("diff", "ratio", "sum", "which_larger") and len(live) >= 2:
        a = _index(plan, c.get("num"), live)
        b = _index(plan, c.get("den"), live)
        if a is None or b is None or a == b:
            a, b = live[0], live[1]
        scale = c.get("scale") or (100 if op == "ratio" else 1)
        steps = [_get(a, ys), _get(b, ys)]
        steps.append({"op": "combine", "a": 0, "b": 1, "how": op, "scale": scale})
        return steps
    if op == "rank" and len(live) == 1:
        top = c.get("top") if isinstance(c.get("top"), int) else 1
        return [_get(live[0], ys), {"op": "rank", "of": 0, "top": top, "ascending": False}]
    if op == "filter_then_lookup" and len(live) >= 2:
        flt = next((i for i in live if inds[i].get("role") == "filter"), live[0])
        rest = [i for i in live if i != flt]
        steps = [_get(flt, ys), {"op": "rank", "of": 0, "top": 1, "ascending": False}]
        steps += [_get(i, ys, {"from_step": 1}) for i in rest[: pe.MAX_GETS - 1]]
        return steps
    if op == "intersect" and len(live) >= 2:
        top = c.get("top") if isinstance(c.get("top"), int) else 5
        a, b = live[0], live[1]
        return [
            _get(a, ys),
            {"op": "rank", "of": 0, "top": top, "ascending": False},
            _get(b, ys),
            {"op": "rank", "of": 2, "top": top, "ascending": False},
            {"op": "pick", "of": 3, "regions": {"from_step": 1}},
        ]
    return None
