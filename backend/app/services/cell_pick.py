"""칸 고르기 LLM (2026-11-18, 세션 B)

바로 계산(dynamic_indicator)은 칸 이름이 질문에 글자 그대로 있어야 칸을 고른다. 그래서
"결혼 건수"를 물으면 항목이 "혼인"인 맞는 표를 골라 놓고도 "질문의 말과 맞는 칸이 없음"으로
계산하지 못했다 (668문항 시뮬레이션: 정답 표가 후보에 있던 483문항 중 299문항이 칸 고르기에서 막힘).

규칙이 먼저 고르고, 규칙이 실패한 표에서만 여기서 LLM 1회(Gemma 기본, 안 되면 HCX)가 고른다.
  - 지역 · 연령 · 성별 분류는 코드가 고르므로 LLM에 보내지 않는다 (dynamic_indicator.llm_groups)
  - 칸 번호는 목록에 있는 것만 받는다.
    LLM이 "이 표에 질문의 값이 없다"(fits 아니오)면 계산하지 않는다
  - 고른 칸은 되묻기에서 사용자가 고른 칸과 같은 자리(forced)로 넘겨 계산 · 검증은 그대로 한다
HANDOFF_CELL_LLM=false 로 끈다.
"""

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
MAX_CELLS = 80  # 한 분류에서 보내는 칸 수. 넘으면 질문과 글자가 겹치는 칸 + 합계만
TOTAL_NAMES = {"계", "합계", "전체", "총계", "소계", "총"}
CELL_FAILURES = re.compile(r"^(질문의 말과 맞는 칸이 없음|.+에서 칸을 고르지 못함)$")
SPACES = re.compile(r"[\s·,.()\[\]{}<>\"'‘’“”_/-]")


def _compact(text: Any) -> str:
    return SPACES.sub("", str(text or ""))


def needs_llm(why: Any) -> bool:
    """규칙이 칸을 못 골라 계산하지 못한 경우인가 (되묻기 Ask · 그 밖의 이유는 아니다)"""
    return isinstance(why, str) and bool(CELL_FAILURES.match(why))


@dataclass
class Group:
    """LLM에 보이는 분류 하나"""

    no: int  # 0 = 항목, 1~5 = c1~c5
    name: str
    cells: list[tuple[str, str]]  # (코드, 이름). 보내는 칸만
    total: int = 0  # 원래 칸 수 (줄여 보냈으면 cells보다 많다)


@dataclass
class Picked:
    forced: dict[int, str] = field(default_factory=dict)  # {분류 번호: 코드}
    unsure: list[Group] = field(default_factory=list)  # "정할 수 없음"이라 한 분류
    read_as: list[tuple[str, str]] = field(default_factory=list)  # (질문 낱말, 표 낱말)
    reason: str = ""

    def notice(self) -> str | None:
        if not self.read_as:
            return None
        pairs = ", ".join(f"'{a}'을(를) 표의 '{b}'(으)로" for a, b in self.read_as)
        return f"질문의 {pairs} 보고 계산했습니다"


def narrow(question: str, cells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """칸이 너무 많은 분류: 합계 칸과 질문과 두 글자 이상 겹치는 칸만 (최대 MAX_CELLS)"""
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
    """응답의 분류 이름 (이름이 겹치면 번호를 붙인다)"""
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
    """응답 JSON. 글자 수 한도에 잘렸어도 다 쓴 칸은 살린다 (llm_json.loads)"""
    return llm_json.loads(raw)[0]


def decide(raw: dict | None, groups: list[Group]) -> Picked | None:
    """LLM 응답 → 고른 칸. 표가 질문의 값을 담지 않는다고 했거나 응답이 이상하면 None.
    응답이 잘려 picks가 없으면(fits 예까지는 씀) 모든 분류를 "정할 수 없음"으로 채운다.
    버리는 대신 분류가 하나이고 칸이 적으면 사용자에게 되묻는다
    (11/18, 세진 제안 "필수 값을 못 채우면 None으로")"""
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
        else:  # "정할 수 없음" · 빠짐 · 목록 밖 번호
            out.unsure.append(g)
    chosen = [
        _compact(name) for g in groups for code, name in g.cells if out.forced.get(g.no) == code
    ]
    for pair in raw.get("read_as") or []:
        if not isinstance(pair, dict):
            continue
        a, b = str(pair.get("질문") or "").strip(), str(pair.get("표") or "").strip()
        # 표 낱말이 실제로 고른 칸 이름에 있을 때만 (지어낸 짝은 버린다)
        if a and b and _compact(a) != _compact(b) and any(_compact(b) in n for n in chosen):
            out.read_as.append((a, b))
    return out


@llm_calls.staged("cell_pick")
async def judge(
    question: str, understanding: dict, tbl_nm: str | None, groups: list[Group]
) -> tuple[dict | None, str]:
    """LLM 1회 → (응답, 답한 LLM)"""
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
        raw["_salvaged"] = True  # 잘린 응답에서 살림 (기록용)
    return raw, llm


async def choose(
    question: str,
    understanding: dict,
    tbl_nm: str | None,
    groups: list[Group],
    step: dict,
) -> Picked | None:
    """규칙이 못 고른 표에서 LLM이 칸을 고른다. 못 하면 None (지금처럼 계산하지 않음). 기록은
    step["cell_llm"]"""
    if not get_settings().handoff_cell_llm or not groups:
        return None
    log: dict[str, Any] = {}
    step["cell_llm"] = log
    start = time.perf_counter()
    try:
        raw, llm = await judge(question, understanding, tbl_nm, groups)
    except Exception as e:  # 판단이 실패하면 지금처럼 계산하지 않는다
        logger.warning("칸 고르기 LLM 실패: %s: %s", type(e).__name__, e)
        log["error"] = f"{type(e).__name__}: {e}"[:200]
        log["result"] = "호출 오류"
        return None
    log.update(llm=llm, ms=round((time.perf_counter() - start) * 1000), raw=raw)
    picked = decide(raw, groups)
    log["result"] = result_label(raw, picked)
    return picked


def result_label(raw: dict | None, picked: Picked | None):
    """기록에 남길 결과: 고른 칸 / 정할 수 없음 / 표에 없음(LLM이 아니오) / 응답 해석 실패.
    (11/18 전에는 해석 실패도 '표에 없음'으로 적어 LLM이 틀린 판단을 한 것처럼 보였다)"""
    if picked is not None:
        if picked.unsure:
            cut = raw is not None and not isinstance(raw.get("picks"), dict)
            return "정할 수 없음 (응답 잘림)" if cut else "정할 수 없음"
        return {str(k): v for k, v in picked.forced.items()}
    if raw and raw.get("fits") == prompt.NO:
        return "표에 없음"
    return "응답 해석 실패"
