"""재계획 (replan): 답 판정이 '다시'를 내고 행동 고르기가 replan 을 골랐을 때, 처음 계획과
판정의 지적을 Gemma 에 주고 계산 계획만 고쳐 받는다 (설계서 4-3). 조건(지역 · 연도 · 성별 · 연령)은
LLM 이 못 바꾸고 계산 · 연도 · 숫자는 코드가 한다.

  first_plan_of  처음 계획: 꾸러미(handoff)의 plan, 사전 경로로 답했으면 해석(parsed)으로 만든
                 최소 계획
  call           Gemma 1회 → 새 계획 (처음 계획 프롬프트와 같은 parse · correct 보정)
                 → num/den 번호를 span 으로 → 종류 잠금 · 같은 계획이면 None
  runnable       코드에 아직 없는 계산 종류(sum · not_computable)면 그 이유 문자열, 되면 None

프롬프트: app/prompts/replan.py (실험실 P6 초안 11/23 승인 + 11/24 수정 3곳 + num/den 번호).
"""

import copy
import json
import logging
from typing import Any

from app.agent import plan as planner
from app.prompts import replan as prompt
from app.services import llm_calls, search_llm
from app.services.hcx_client import HcxError

logger = logging.getLogger(__name__)

MODEL = "HCX-007"  # GemmaClient 는 GEMMA_MODEL 로 바꿔 보낸다
MAX_TOKENS = 600
# 판정 이유 → LLM 이 고쳐도 되는 칸. 대상이 다르면 다른 통계를 쓴 것이라 search 도 열어 준다
EDITABLE = {
    "형태가 다름": "combine, indicators",
    "조건이 빠짐": "combine, indicators",
    "계산이 빠짐": "combine, indicators",
    "기간이 다름": "combine, indicators",
}
EDITABLE_DEFAULT = "indicators[].search, combine"
# 아직 코드가 없는 것: 월 단위 합 (lookup 은 연 단위 자료만 읽는다). 계산하지 않고 다음 행동으로
NOT_YET_MONTH = "월 단위 합계(sum over month)"
# 계산하지 않는 종류: 숫자 대신 답변 불가 사유로 (refusal.not_collected_item, 설계서 B-1 예외)
NOT_COMPUTABLE = "not_computable"
REFUSAL_CODE = "not_collected_item"


def first_plan_of(found: dict | None, parsed: dict) -> dict:
    """처음 계획. 꾸러미에 계획이 있으면 그것, 없으면(사전 경로) 해석에서 지표 하나짜리 계획"""
    h = (found or {}).get("handoff") or {}
    p = h.get("plan") or {}
    if p.get("indicators"):
        return {
            "shared": p.get("shared") or {},
            "indicators": [
                {k: i.get(k) for k in planner.IND_KEYS if i.get(k) is not None}
                for i in p["indicators"]
            ],
            "combine": {
                k: v for k, v in (p.get("combine") or {}).items() if v not in (None, "", [])
            }
            or {"op": "none"},
        }
    name = parsed.get("indicator") or ""
    shared: dict[str, Any] = {}
    if parsed.get("year"):
        shared["year_from"] = shared["year_to"] = parsed["year"]
    if parsed.get("sex") and parsed["sex"] != "계":
        shared["sex"] = parsed["sex"]
    if isinstance(parsed.get("age"), dict) and parsed["age"].get("expr"):
        shared["age"] = parsed["age"]["expr"]
    return {
        "shared": shared,
        "indicators": [
            {"span": name, "search": name, "dict": name, "scope": "in", "role": "measure"}
        ],
        "combine": {"op": "none"},
    }


def kind_of(parsed: dict, first: dict) -> str:
    """질문이 묻는 값의 종류 (LLM 에 참고로 주고, 종류 잠금에 쓴다)"""
    if parsed.get("type") == "composition":
        return "ratio"
    spec = parsed.get("dynamic_spec") or {}
    if spec.get("kind") == "ratio" or (first.get("combine") or {}).get("op") == "ratio":
        return "ratio"
    # 사전 지표로 답했으면 그 지표의 종류 (등록외국인 비율 → ratio). 11/24 손 확인: 이걸 안 봐서
    # 서울 안 구 순위의 맞는 재계획을 "종류가 바뀜"으로 버렸다
    from app.services import indicators

    sp = indicators.spec(parsed.get("indicator") or "") or {}
    if sp.get("kind") in ("ratio", "composition") or sp.get("unit") == "%":
        return "ratio"
    return "count"


def user_text(question: str, first: dict, verdict: dict, kind: str, tried: list[str]) -> str:
    reason = str(verdict.get("reason") or "")
    lines = [
        f"질문: {question}",
        f"묻는 값의 종류: {kind}",
        f"고쳐도 되는 칸: {EDITABLE.get(reason, EDITABLE_DEFAULT)}",
        "처음 계획:",
        json.dumps(first, ensure_ascii=False),
        f"검토자의 지적: {reason} — {verdict.get('missing') or ''}",
    ]
    if tried:
        lines.append("시도한 표: " + ", ".join(tried))
    return "\n".join(lines)


def _signature(p: dict) -> tuple:
    inds = tuple(
        (i.get("dict") or i.get("search") or "", i.get("span") or "")
        for i in p.get("indicators") or []
    )
    c = p.get("combine") or {}
    return inds, c.get("op") or "none", c.get("num"), c.get("den"), c.get("within"), c.get("axis")


def resolve(new: dict, question: str, first: dict, kind: str) -> tuple[dict | None, str]:
    """LLM 출력 → 쓸 수 있는 계획. (계획, 메모) / 못 쓰면 (None, 이유)
    - 처음 계획 프롬프트와 같은 보정 (근거 없는 조건 삭제 · 연도 규칙 · dict 채우기)
    - num/den 번호 → 지표 span (multi_indicator.ratio_order 가 span 으로 방향을 정한다)
    - 종류 잠금: 비율을 물었는데 건수 계획(또는 반대)이면 거부
    - 처음 계획과 같으면 거부 (다음 행동으로)"""
    p = planner.correct(question, planner.parse(json.dumps(new)))
    measure = new.get("measure")
    if kind == "ratio" and measure == "count" or kind == "count" and measure == "ratio":
        return None, f"묻는 값의 종류가 바뀜 ({kind} → {measure})"
    c = p["combine"]
    spans = [i.get("span") for i in p["indicators"]]
    for key in ("num", "den"):
        v = c.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            c[key] = spans[v] if 0 <= v < len(spans) else None
        elif v not in spans:
            c[key] = None
    first_filled = copy.deepcopy(first)
    planner.fill_dict(first_filled)  # 처음 계획에도 같은 dict 채우기를 적용해 같은 조건으로 견준다
    if _signature(p) == _signature(first_filled):
        return None, "처음 계획과 같음"
    p["measure"] = measure
    p["changed"] = new.get("changed")
    return p, str(new.get("changed") or "")


def runnable(p: dict) -> str | None:
    """코드로 계산할 수 있는 계획인가. 없는 종류면 그 이름"""
    c = p.get("combine") or {}
    if c.get("op") == NOT_COMPUTABLE:
        return None  # 계산하지 않고 답변 불가 사유로 (ask_session._replan_compute)
    if c.get("op") == "sum" and c.get("over") == "month":
        return NOT_YET_MONTH
    if not [i for i in p.get("indicators") or [] if i.get("scope") != "out" and i.get("search")]:
        return "검색할 지표가 없음"
    return None


def apply_to_parsed(p: dict, parsed: dict) -> dict:
    """새 계획의 combine 을 지표 하나짜리 계산(사전 경로 qa)이 읽는 해석 칸으로 옮긴다.
    지표가 둘 이상이면 multi_indicator 가 combine 을 직접 읽으므로 바꾸지 않는다
    - sum over region · year  → aggregate=sum (qa 가 지역 묶음 · 연도 묶음 합을 이미 한다)
    - rank within=시도 이름    → sido · level=시군구 (lookup.rank(sido=…))
    - rank axis=분류 이름      → rank_axis (ask_session._axis_rank)"""
    c = p.get("combine") or {}
    live = [i for i in p.get("indicators") or [] if i.get("scope") != "out"]
    out = dict(parsed)
    if len(live) != 1:
        return out
    op = c.get("op")
    if op == "sum" and c.get("over") in ("region", "year"):
        out["aggregate"] = "sum"
    if op == "rank":
        out["type"] = "rank"
        if c.get("within"):
            out["sido"], out["level"] = c["within"], "시군구"
        if c.get("axis"):
            out["rank_axis"] = c["axis"]
    return out


@llm_calls.staged("replan")
async def _ask(text: str) -> dict | None:
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        MODEL,
        [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": text}],
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        temperature=0,
        responseFormat={"type": "json", "schema": prompt.SCHEMA},
    )
    try:
        out = json.loads(result.content)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


async def call(
    question: str, first: dict, verdict: dict, parsed: dict, tried: list[str], trace: dict
) -> dict | None:
    """재계획 1회. 쓸 수 있는 새 계획이면 돌려주고, 아니면 None (이유는 trace["replan"])"""
    kind = kind_of(parsed, first)
    record: dict[str, Any] = {"first": copy.deepcopy(first), "kind": kind}
    trace["replan"] = record
    try:
        raw = await _ask(user_text(question, first, verdict, kind, tried))
    except HcxError as e:
        record["error"] = f"{type(e).__name__}: {e}"
        return None
    if raw is None:
        record["error"] = "JSON 아님"
        return None
    record["raw"] = raw
    p, note = resolve(raw, question, first, kind)
    record["note"] = note
    if p is None:
        return None
    if why := runnable(p):
        record["not_runnable"] = why
        return None
    record["plan"] = {k: p.get(k) for k in ("shared", "indicators", "combine", "measure")}
    return p
