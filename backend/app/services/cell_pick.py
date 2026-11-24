import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.prompts import cell_pick as prompt
from app.services import llm_calls, llm_json, search_llm

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_TOKENS = 500
MAX_CELLS = 80
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총"}
CELL_FAILURES = re.compile(r"^(질문의 말과 맞는 칸이 없음|.+에서 칸을 고르지 못함)$")
SPACES = re.compile(r"[\s·,.()\[\]{}<>\"'‘’“”_/-]")


def _compact(text: Any) -> str:
    return SPACES.sub("", str(text or ""))


def needs_llm(why: Any) -> bool:
    return isinstance(why, str) and bool(CELL_FAILURES.match(why))


@dataclass
class Group:
    no: int
    name: str
    cells: list[tuple[str, str]]
    total: int = 0


@dataclass
class Picked:
    forced: dict[int, str] = field(default_factory=dict)
    unsure: list[Group] = field(default_factory=list)
    read_as: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""

    def notice(self) -> str | None:
        if not self.read_as:
            return None
        pairs = ", ".join(f"'{a}'을(를) 표의 '{b}'(으)로" for a, b in self.read_as)
        return f"질문의 {pairs} 보고 계산했습니다"


def narrow(question: str, cells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(cells) <= MAX_CELLS:
        return cells
    q = _compact(question)
    grams = {q[i : i + 2] for i in range(len(q) - 1)}
    totals = [c for c in cells if _compact(c[1]) in TOTAL_NAMES]
    near = [
        c
        for c in cells
        if c not in totals
        and any(_compact(c[1])[i : i + 2] in grams for i in range(len(_compact(c[1])) - 1))
    ]
    return (totals + near)[:MAX_CELLS]


def keys_of(groups: list[Group]) -> list[str]:
    keys: list[str] = []
    for g in groups:
        key = g.name if g.name not in keys else f"{g.name} ({g.no})"
        keys.append(key)
    return keys


def user_text(question: str, understanding: dict, tbl_nm: str | None, groups: list[Group]) -> str:
    u = understanding or {}
    items = ", ".join((u.get("conditions") or {}).get("items") or []) or "없음"
    lines = [
        f"질문: {question}",
        f"질문 해석: 지표 {u.get('topic') or '-'}, 대상 {u.get('target') or '없음'}, 조건 {items}",
        f"통계표: {tbl_nm or '-'}",
        "분류별 칸:",
    ]
    for key, g in zip(keys_of(groups), groups, strict=True):
        cells = "  ".join(f"{i}. {name}" for i, (_, name) in enumerate(g.cells, start=1))
        cut = f"  (칸 {g.total}개 중 질문과 글자가 겹치는 것만)" if g.total > len(g.cells) else ""
        lines.append(f"[{key}] {cells}{cut}")
    return "\n".join(lines)


def parse(raw: str | None) -> dict | None:
    return llm_json.loads(raw)[0]


def decide(raw: dict | None, groups: list[Group]) -> Picked | None:
    if not raw or raw.get("fits") != prompt.YES:
        return None
    picks = raw.get("picks")
    if not isinstance(picks, dict):
        if not raw.get("_salvaged"):
            return None
        picks = {}
    out = Picked(reason=str(raw.get("reason") or "")[:200])
    for key, g in zip(keys_of(groups), groups, strict=True):
        value = str(picks.get(key) or "").strip()
        if value.isdigit() and 1 <= int(value) <= len(g.cells):
            out.forced[g.no] = g.cells[int(value) - 1][0]
        else:
            out.unsure.append(g)
    chosen = [
        _compact(name) for g in groups for code, name in g.cells if out.forced.get(g.no) == code
    ]
    for pair in raw.get("read_as") or []:
        if not isinstance(pair, dict):
            continue
        a, b = str(pair.get("질문") or "").strip(), str(pair.get("표") or "").strip()
        if a and b and _compact(a) != _compact(b) and any(_compact(b) in n for n in chosen):
            out.read_as.append((a, b))
    return out


@llm_calls.staged("cell_pick")
async def judge(
    question: str, understanding: dict, tbl_nm: str | None, groups: list[Group]
) -> tuple[dict | None, str]:
    user = user_text(question, understanding, tbl_nm, groups)
    shape = prompt.schema({k: len(g.cells) for k, g in zip(keys_of(groups), groups, strict=True)})

    def request(llm: str):
        messages = [{"role": "system", "content": prompt.SYSTEM}, {"role": "user", "content": user}]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": MAX_TOKENS,
            **({"temperature": 0} if llm == "gemma" else {}),
            "responseFormat": {"type": "json", "schema": shape},
        }

    result, llm = await search_llm.ask(MODEL, request)
    raw, salvaged = llm_json.loads(result.content)
    if salvaged and raw is not None:
        raw["_salvaged"] = True
    return raw, llm


async def choose(
    question: str,
    understanding: dict,
    tbl_nm: str | None,
    groups: list[Group],
    step: dict,
) -> Picked | None:
    if not get_settings().handoff_cell_llm or not groups:
        return None
    log: dict[str, Any] = {}
    step["cell_llm"] = log
    start = time.perf_counter()
    try:
        raw, llm = await judge(question, understanding, tbl_nm, groups)
    except Exception as e:
        logger.warning("칸 고르기 LLM 실패: %s: %s", type(e).__name__, e)
        log["error"] = f"{type(e).__name__}: {e}"[:200]
        log["result"] = "호출 오류"
        return None
    log.update(llm=llm, ms=round((time.perf_counter() - start) * 1000), raw=raw)
    picked = decide(raw, groups)
    log["result"] = result_label(raw, picked)
    return picked


def result_label(raw: dict | None, picked: Picked | None):
    if picked is not None:
        if picked.unsure:
            cut = raw is not None and not isinstance(raw.get("picks"), dict)
            return "정할 수 없음 (응답 잘림)" if cut else "정할 수 없음"
        return {str(k): v for k, v in picked.forced.items()}
    if raw and raw.get("fits") == prompt.NO:
        return "표에 없음"
    return "응답 해석 실패"
