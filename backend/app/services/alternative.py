"""조건을 모두 만족하는 값이 없을 때의 대안 (계약_초안 5-6, 2026-11-17)

넘기기 꾸러미의 후보로 모두 계산하지 못했을 때(dynamic_indicator.compute_from_handoff),
지금까지는 표 안내로 끝났다. 여기서는
  1. 코드가 대안을 만들고 값까지 계산한다 (표에 실제로 있는 것만)
       가까운 연도   연도 조건만 안 맞은 표(✘ year) · 수록
       기간 밖으로 끝난 표 → 그 표에 있는 가장 가까운 해
       다른 표       규칙으로 건너뛴 후보 → 그대로
                     (고른 표가 '모두 충족'이라서, 확인한 조건이 없어서)
  2. LLM 1회(Gemma 기본, 꺼져 있으면 HCX)가 그중 질문의 답으로 쓸 만한 것만 고른다
     (대상 · 집단을 바꾼 것, 개념이 다른 통계, 값의 뜻이 다른 것은 버린다)
  3. 고른 대안을 값과 함께 되묻는다: "① 2025년 기준: 서울 ○○명 ② …  어느 것으로 볼까요?"
     사용자가 고르면 미리 계산한 답을 그대로 준다 (ask_session의 되묻기 kind "alternative")
대안이 하나도 계산되지 않으면 LLM을 부르지 않는다. HANDOFF_ALTERNATIVES=false 로 끈다.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.prompts import alternative as prompt
from app.services import llm_json, search_llm

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_BUILT = 5  # 계산해 보는 대안 수
MAX_OFFERS = 3  # 사용자에게 보이는 대안 수
MAX_TOKENS = 400
NUMBERS = "①②③"
YEAR_KEYS = ("year", "period")

# 표 하나를 조건을 바꿔 계산해 보는 함수
# (compute_from_handoff가 넘긴다): (후보, 해석) → 답 또는 None
Run = Callable[[dict, dict], Awaitable[dict | None]]


def year_change(entry: dict, q: dict) -> tuple[dict, str] | None:
    """그 표에 있는 가장 가까운 연도로 바꾼 조건과 설명. 바꿀 것이 없으면 None"""
    lo, hi = entry.get("start_year"), entry.get("end_year")
    if lo is None or hi is None:
        return None
    if q.get("type") == "trend" and q.get("start_year"):
        s, e = q["start_year"], q.get("end_year") or hi
        ns, ne = max(s, lo), min(e, hi)
        if ns > ne or (ns, ne) == (s, e):
            return None
        return {
            "start_year": ns,
            "end_year": ne,
        }, f"기간 {s}~{e}년 → {ns}~{ne}년 (수록 {lo}~{hi}년)"
    y = q.get("year")
    if y is None or lo <= y <= hi:
        return None
    new = hi if y > hi else lo
    return {"year": new}, f"연도 {y}년 → {new}년 (수록 {lo}~{hi}년)"


def plan(attempts: list[dict], q: dict, picked_name: str | None) -> list[dict]:
    """만들 대안 목록 (우선순위 순서). attempts: 시도한 표마다 {entry, res, skip}"""
    years, others = [], []
    for a in attempts:
        entry = a["entry"]
        failed = [k for k, ok in (entry.get("checks") or {}).items() if not ok]
        ranged = (a.get("res") or {}).get("status") == "out_of_range"
        if (failed and set(failed) <= set(YEAR_KEYS)) or ranged:
            if made := year_change(entry, q):
                change, what = made
                years.append({"entry": entry, "change": change, "what": what})
        elif not failed and a.get("skip") and entry.get("kind") == "other":
            why = f"고른 표 '{picked_name}'로는 계산하지 못함" if picked_name else "다른 표"
            others.append({"entry": entry, "change": {}, "what": f"다른 표 ({why})"})
    return (years + others)[:MAX_BUILT]


# 지금 계산(lookup)은 연 단위 자료만 읽는다. 질문이
# 그보다 짧은 기간을 물었으면 대안마다 그 차이를 적어
# LLM이 보게 한다 (11/18: "2024년 상반기 혼인 건수"에 연간 값을 같은 기간으로 보고 제안한 문제)
VALUE_PERIOD = "연간(1년 전체)"
SHORT_PERIODS = (
    re.compile(r"상반기|하반기|반기"),
    re.compile(r"\d\s*분기|분기\s*별"),
    re.compile(r"지난\s*달|이번\s*달|\d{1,2}\s*월(?!\s*생)|월\s*별|최근\s*\d+\s*개월|\d+\s*개월"),
)


def asked_period(question: str) -> str | None:
    """질문이 1년보다 짧은 기간을 물었으면 그 말 ("상반기", "2분기", "지난달"). 아니면 None"""
    for pattern in SHORT_PERIODS:
        if m := pattern.search(question or ""):
            return m.group(0).strip()
    return None


def first_line(answer: str | None) -> str:
    for line in (answer or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def user_text(question: str, failures: list[str], alts: list[dict]) -> str:
    lines = [f"질문: {question}", "원래 조건으로 계산하지 못한 이유:"]
    lines += [f"- {f}" for f in failures] or ["- (기록 없음)"]
    lines.append("대안:")
    short = asked_period(question)
    for a in alts:
        what = a["what"]
        if short:
            what = f"{what}. 기간 단위: {short} → {VALUE_PERIOD}"
        lines.append(
            f"{a['id']}. 표: {a['entry'].get('tbl_nm')} / 달라진 점: {what}"
            f" / 값의 기간: {VALUE_PERIOD} / 값: {a['value']}"
        )
    return "\n".join(lines)


def parse(raw: str) -> dict | None:
    """응답 JSON. 글자 수 한도에 잘렸어도 다 쓴 칸은 살린다 (llm_json.loads, 11/18)"""
    return llm_json.loads(raw)[0]


async def judge(question: str, failures: list[str], alts: list[dict]) -> tuple[dict | None, str]:
    """LLM 1회 → (판단, 답한 LLM)"""
    ids = [a["id"] for a in alts]
    user = user_text(question, failures, alts)

    def request(llm: str):
        messages = [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": user}]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": MAX_TOKENS,
            **({"temperature": 0} if llm == "gemma" else {}),
            "responseFormat": {"type": "json", "schema": prompt.schema(ids)},
        }

    result, llm = await search_llm.ask(MODEL, request)
    return parse(result.content), llm


async def offer(
    question: str,
    q: dict,
    attempts: list[dict],
    picked_name: str | None,
    run: Run,
    trace: dict,
) -> dict | None:
    """대안을 계산하고 LLM이 고른 것을 되묻는 답 (qa.answer와 같은 모양). 없으면 None → 표 안내"""
    if not get_settings().handoff_alternatives:
        return None
    planned = plan(attempts, q, picked_name)
    log: dict[str, Any] = {"planned": len(planned)}
    trace["alternative"] = log
    if not planned:
        return None
    results = await asyncio.gather(*(run(p["entry"], {**q, **p["change"]}) for p in planned))
    alts = []
    for p, res in zip(planned, results, strict=True):
        if res is not None and res.get("status") == "ok":
            alts.append({**p, "res": res, "value": first_line(res.get("answer"))})
    log["computed"] = [{"tbl_id": a["entry"]["tbl_id"], "what": a["what"]} for a in alts]
    if not alts:
        return None
    for i, a in enumerate(alts, start=1):
        a["id"] = f"A{i}"
    failures = [f"[{a['entry'].get('tbl_nm')}] {_failure(a)}" for a in attempts]
    start = time.perf_counter()
    try:
        decided, llm = await judge(question, failures, alts)
    except Exception as e:  # 판단이 실패하면 지금처럼 표 안내
        logger.warning("대안 판단 실패 → 표 안내: %s: %s", type(e).__name__, e)
        log["error"] = f"{type(e).__name__}: {e}"[:200]
        log["result"] = "호출 오류"
        return None
    log.update(llm=llm, ms=round((time.perf_counter() - start) * 1000), raw=decided)
    log["result"] = (decided or {}).get("action") or "응답 해석 실패"
    if not decided or decided.get("action") != "offer":
        return None  # tables · clarify · 해석 실패: 표 안내 (clarify 되묻기는 다음 단계)
    by_id = {a["id"]: a for a in alts}
    chosen = []
    for o in decided.get("offers") or []:
        a = by_id.get(str(o.get("id")))
        if a is not None and a not in chosen:
            chosen.append({**a, "label": (o.get("label") or a["what"]).strip()})
        if len(chosen) >= MAX_OFFERS:
            break
    if not chosen:
        return None
    log["offered"] = [c["entry"]["tbl_id"] for c in chosen]
    return answer(question, q, chosen)


def _failure(attempt: dict) -> str:
    """시도한 표가 계산되지 않은 이유 한 줄"""
    return attempt.get("skip") or (attempt.get("res") or {}).get("status") or "계산 안 됨"


def answer(question: str, q: dict, chosen: list[dict]) -> dict:
    """대안 되묻기 응답. 고른 대안의 답(미리 계산함)은 대화 상태(context)에만 남는다"""
    labels: list[str] = []
    for c in chosen:
        label = c["label"]
        if label in labels:  # 버튼 이름이 겹치면 표 이름으로 구분
            label = f"{label} ({c['entry'].get('tbl_nm')})"
        labels.append(label)
    lines = ["요청하신 조건을 모두 만족하는 통계는 찾지 못했습니다. 대신 이런 자료가 있습니다."]
    lines += [
        f"{NUMBERS[i]} {label}: {c['value']}"
        for i, (label, c) in enumerate(zip(labels, chosen, strict=True))
    ]
    lines.append("어느 것으로 볼까요? 번호로 답해 주세요.")
    text = "\n".join(lines)
    stored = [{k: v for k, v in c["res"].items() if k != "trace"} for c in chosen]
    return {
        "status": "clarify_question",
        "answer": text,
        "parsed": q,
        "result": {"status": "clarify_question"},
        "sources": [],
        "chart": None,
        "trace": {},
        "question": question,
        "clarification": {
            "kind": "alternative",
            "prompt": text,
            "options": labels,
            "context": {
                "codes": {label: str(i) for i, label in enumerate(labels)},
                "answers": stored,
            },
        },
    }
