import logging
import time
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.prompts import recell as prompt
from app.services import binding, llm_calls, llm_json, search_llm
from app.services import dynamic_indicator as di
from app.services.binding import Binding, Bound, Unbound

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_TOKENS = 700
MAX_CELLS = 80
NOTE = "뜻으로 이음"


@dataclass
class Fix:
    binding: Binding
    notice: str | None


def conditions(b: Binding, dims: list[di.Dim], topic: str | None) -> list[Unbound]:
    names = {d.name for d in dims}
    out = [u for u in b.unbound if u.value]
    if not out and topic and any(u.cond in names for u in b.unbound):
        out = [Unbound("topic", topic, "지표 이름과 맞는 칸이 없음")]
    return out


def user_text(
    question: str,
    tbl_nm: str | None,
    b: Binding,
    conds: list[Unbound],
    dims: list[di.Dim],
    region_dim: int | None,
) -> str:
    bound = [
        f"{x.cond}={' · '.join(x.names) or x.value}"
        for x in b.bound
        if x.by not in ("period", "region")
    ]
    lines = [
        f"질문: {question}",
        f"통계표: {b.tbl_id} {tbl_nm or ''}".rstrip(),
        f"이미 정한 칸: {'; '.join(bound) or '-'}",
        "못 이은 조건:",
        *(f"{i}. {u.cond}='{u.value}' ({u.why})" for i, u in enumerate(conds, start=1)),
        "분류별 칸 (코드: 이름):",
    ]
    for d in dims:
        if d.no == region_dim:
            continue
        cells = list(d.codes.items())
        cut = f" (칸 {len(cells)}개 중 앞 {MAX_CELLS}개)" if len(cells) > MAX_CELLS else ""
        shown = ", ".join(f"{c}: {n}" for c, n in cells[:MAX_CELLS])
        lines.append(f"[{d.no}] {d.name}{cut}: {shown}")
    return "\n".join(lines)


@llm_calls.staged("recell")
async def judge(text: str) -> tuple[dict | None, str]:
    def request(llm: str):
        messages = [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": text}]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": MAX_TOKENS,
            "temperature": 0,
            "responseFormat": {"type": "json", "schema": prompt.schema()},
        }

    result, llm = await search_llm.ask(MODEL, request)
    raw, _ = llm_json.loads(result.content)
    return raw, llm


def _names(d: di.Dim, codes: list[str]) -> str:
    return " · ".join(d.codes[c] for c in codes)


def apply(
    b: Binding,
    conds: list[Unbound],
    raw: dict | None,
    dims: list[di.Dim],
    region_dim: int | None,
    tbl_nm: str | None = None,
) -> Fix | None:
    picks = (raw or {}).get("picks")
    if not isinstance(picks, list):
        return None
    by_no = {d.no: d for d in dims if d.no != region_dim}
    bound = list(b.bound)
    left = list(b.unbound)
    filled: set[int] = set()
    covered = False
    read_as: list[tuple[str, str]] = []
    broad: list[tuple[str, str]] = []
    for p in picks:
        if not isinstance(p, dict):
            continue
        no, how = p.get("no"), p.get("how")
        if not isinstance(no, int) or not 1 <= no <= len(conds) or how == prompt.NONE:
            continue
        u = conds[no - 1]
        if how == prompt.TABLE:
            new = Bound(u.cond, u.value, "table", note=NOTE)
            shown = tbl_nm or "표 전체"
        elif how in (prompt.CELLS, prompt.CLASS):
            d = by_no.get(p.get("dim")) if isinstance(p.get("dim"), int) else None
            if d is None:
                return None
            if how == prompt.CLASS:
                new = Bound("class", u.value, "dim", d.no, note=d.name)
                shown = d.name
            else:
                codes = [str(c) for c in p.get("codes") or []]
                if not codes or any(c not in d.codes for c in codes):
                    return None
                same = [x for x in bound if x.dim == d.no and x.by in ("value", "forced")]
                if u.cond == "item" and same and all(x.cond == "item" for x in same):
                    for x in same:
                        bound.remove(x)
                    codes = list(dict.fromkeys([c for x in same for c in x.codes] + codes))
                elif any(set(x.codes) != set(codes) for x in same):
                    if not p.get("broader"):
                        return None
                    bound.append(Bound(u.cond, u.value, "table", note=NOTE))
                    covered = True
                    if u in left:
                        left.remove(u)
                    broad.append((u.value, _names(d, codes)))
                    continue
                names = [d.codes[c] for c in codes]
                new = Bound(u.cond, u.value, "value", d.no, codes, names, note=NOTE)
                shown = _names(d, [str(c) for c in p.get("codes") or []])
                if not p.get("broader"):
                    read_as.append((u.value, shown))
            bound = [x for x in bound if not (x.dim == d.no and x.by in binding.QUIET)]
            filled.add(d.no)
        else:
            continue
        covered = covered or how != prompt.CLASS
        bound.append(new)
        if u in left:
            left.remove(u)
        if p.get("broader"):
            broad.append((u.value, shown))
    names = {by_no[n].name for n in filled}
    left = [u for u in left if not (not u.value and u.cond in names)]
    if covered:
        left = [u for u in left if u.cond != "topic"]
    if left or not (filled or covered):
        return None
    out = Binding(b.tbl_id, bound, [], list(b.skipped))
    lines = [f"질문의 '{a}'을(를) 표의 '{c}'(으)로 보고 계산했습니다" for a, c in read_as]
    lines += [f"질문의 '{a}'은(는) 표에 따로 없어 '{c}' 기준으로 답했습니다" for a, c in broad]
    return Fix(out, ". ".join(lines) or None)


async def fix(
    question: str, tbl_nm: str | None, b: Binding, topic: str | None, step: dict[str, Any]
) -> Fix | None:
    dims, region_dim, _ = await run_in_threadpool(di.schema, b.tbl_id)
    conds = conditions(b, dims, topic)
    if not conds:
        return None
    log: dict[str, Any] = {"conds": [f"{u.cond}={u.value}" for u in conds]}
    step["recell"] = log
    start = time.perf_counter()
    try:
        raw, llm = await judge(user_text(question, tbl_nm, b, conds, dims, region_dim))
    except Exception as e:
        logger.warning("recell LLM 실패: %s: %s", type(e).__name__, e)
        log["error"] = f"{type(e).__name__}: {e}"[:200]
        return None
    log.update(llm=llm, ms=round((time.perf_counter() - start) * 1000), raw=raw)
    out = apply(b, conds, raw, dims, region_dim, tbl_nm)
    log["result"] = out.binding.to_dict() if out else "잇지 못함"
    return out
