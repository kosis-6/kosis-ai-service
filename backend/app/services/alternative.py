import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.prompts import alternative as prompt
from app.services import llm_calls, llm_json, search_llm

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_BUILT = 5
MAX_OFFERS = 3
MAX_TOKENS = 400
NUMBERS = "①②③"
YEAR_KEYS = ("year", "period")

Run = Callable[[dict, dict], Awaitable[dict | None]]


def year_change(entry: dict, q: dict) -> tuple[dict, str] | None:
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


VALUE_PERIOD = "연간(1년 전체)"
SHORT_PERIODS = (
    re.compile(r"상반기|하반기|반기"),
    re.compile(r"\d\s*분기|분기\s*별"),
    re.compile(r"지난\s*달|이번\s*달|\d{1,2}\s*월(?!\s*생)|월\s*별|최근\s*\d+\s*개월|\d+\s*개월"),
)


def asked_period(question: str) -> str | None:
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
    return llm_json.loads(raw)[0]


@llm_calls.staged("alternative")
async def judge(question: str, failures: list[str], alts: list[dict]) -> tuple[dict | None, str]:
    ids = [a["id"] for a in alts]
    user = user_text(question, failures, alts)

    def request(llm: str):
        messages = [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": user}]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": MAX_TOKENS,
            "temperature": 0,
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
    except Exception as e:
        logger.warning("대안 판단 실패 → 표 안내: %s: %s", type(e).__name__, e)
        log["error"] = f"{type(e).__name__}: {e}"[:200]
        log["result"] = "호출 오류"
        return None
    log.update(llm=llm, ms=round((time.perf_counter() - start) * 1000), raw=decided)
    log["result"] = (decided or {}).get("action") or "응답 해석 실패"
    if not decided or decided.get("action") != "offer":
        return None
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
    return attempt.get("skip") or (attempt.get("res") or {}).get("status") or "계산 안 됨"


def answer(question: str, q: dict, chosen: list[dict]) -> dict:
    labels: list[str] = []
    for c in chosen:
        label = c["label"]
        if label in labels:
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
