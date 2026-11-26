"""복합 질문의 계산 계획 실행기 (A_20 설계, 11/26).

계획은 연산(step)의 순서다. 모든 step의 결과는 같은 모양 — 지역 × 시점 표(Table) — 라 서로 이어진다.
LLM은 계획만 적고 숫자는 여기(코드)가 낸다. 값을 가져오는 get 은 바깥에서 받는다(사전 · 표 경로).

연산(닫힌 목록 6개)
- get     : indicator · regions · years → 표                      (바깥의 getter 가 가져옴)
- change  : 두 시점의 변화 (diff · rate · ratio)                   → 지역 × 1
- combine : 두 step 을 같은 지역 · 시점끼리 (diff · ratio · sum · which_larger)
- rank    : 상위 N (top 또는 top_pct, ascending)                  → 지역 × 1 (순서 있음)
- filter  : 조건을 만족하는 지역만 (ge · gt · le · lt · above · below · top_pct · bottom_pct)
- pick    : 앞 step 이 남긴 지역만

regions 는 "all" · 지역 이름 목록 · {"from_step": k} 셋 중 하나. years 는 숫자 연도 문자열 또는
"latest" · "latest-N" 상대 표현. 실제 연도는 getter(자료 범위)가 정한다."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

OPS = ("get", "change", "combine", "rank", "filter", "pick")
CHANGE_HOW = ("diff", "rate", "ratio")
COMBINE_HOW = ("diff", "ratio", "sum", "which_larger")
FILTER_KEYS = ("ge", "gt", "le", "lt", "above", "below", "top_pct", "bottom_pct")
MAX_STEPS = 6
MAX_GETS = 4
NATION = "전국"


class PlanError(Exception):
    """계획이 틀렸거나(검증) 어느 step 이 값을 못 냈다(실행). code 는 상태 이름, step 은 번호"""

    def __init__(self, code: str, step: int | None, why: str) -> None:
        super().__init__(why)
        self.code, self.step, self.why = code, step, why


@dataclass
class Table:
    """지역 × 시점 표. rows[지역][시점] = 값(없으면 None). order 는 순위 step 이 남긴 지역 순서"""

    rows: dict[str, dict[str, float | None]]
    indicator: str | None = None
    unit: str | None = None
    label: str | None = None
    order: list[str] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def regions(self) -> list[str]:
        return self.order if self.order is not None else list(self.rows)

    def periods(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.rows.values():
            for p in r:
                seen.setdefault(p, None)
        return sorted(seen, key=_period_key)

    def last_period(self) -> str:
        ps = self.periods()
        if not ps:
            raise PlanError("missing_data", None, "값이 없는 표")
        return ps[-1]

    def single(self, region: str) -> float | None:
        """지역의 값 하나 (시점이 여럿이면 마지막 시점)"""
        row = self.rows.get(region) or {}
        if not row:
            return None
        return row[sorted(row, key=_period_key)[-1]]


Getter = Callable[[int, Any, list[str]], Awaitable[Table]]
# get(indicator 번호, regions 지정, years 표현 목록) → Table.
# regions 지정은 "all" 또는 지역 이름 목록


def _period_key(p: str) -> tuple:
    digits = "".join(ch for ch in str(p) if ch.isdigit())
    return (int(digits) if digits else 0, str(p))


# ---------------------------------------------------------------- 검증 (실행 전, LLM 없이)
def validate(steps: list[dict], n_indicators: int) -> list[str]:
    """틀린 점 목록. 비어 있으면 실행해도 된다"""
    out: list[str] = []
    if not isinstance(steps, list) or not steps:
        return ["steps 가 비어 있음"]
    if len(steps) > MAX_STEPS:
        out.append(f"step 이 {len(steps)}개 (최대 {MAX_STEPS})")
    gets = 0
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            out.append(f"step {i}: 객체가 아님")
            continue
        op = s.get("op")
        if op not in OPS:
            out.append(f"step {i}: 모르는 연산 {op!r}")
            continue
        for key in ("of", "a", "b"):
            if key in s and not (isinstance(s[key], int) and 0 <= s[key] < i):
                out.append(f"step {i}: {key} 는 앞 step 번호여야 함 ({s[key]!r})")
        if op == "get":
            gets += 1
            ind = s.get("indicator")
            if not (isinstance(ind, int) and 0 <= ind < n_indicators):
                out.append(f"step {i}: indicator 번호 {ind!r} 가 없음")
            regs = s.get("regions", "all")
            if isinstance(regs, dict):
                k = regs.get("from_step")
                if not (isinstance(k, int) and 0 <= k < i):
                    out.append(f"step {i}: regions.from_step 은 앞 step 번호여야 함")
            elif regs != "all" and not (
                isinstance(regs, list) and all(isinstance(r, str) for r in regs)
            ):
                out.append(f"step {i}: regions 는 'all' · 이름 목록 · from_step 중 하나")
            years = s.get("years") or []
            if not (isinstance(years, list) and years and all(isinstance(y, str) for y in years)):
                out.append(f"step {i}: years 가 비어 있음")
            elif len(years) > 2:
                out.append(f"step {i}: years 는 최대 둘 (한 시점 또는 두 시점)")
        elif op == "change":
            if "of" not in s or not s.get("from") or not s.get("to"):
                out.append(f"step {i}: change 는 of · from · to 가 필요")
            if s.get("how", "diff") not in CHANGE_HOW:
                out.append(f"step {i}: change.how {s.get('how')!r}")
        elif op == "combine":
            if "a" not in s or "b" not in s:
                out.append(f"step {i}: combine 은 a · b 가 필요")
            if s.get("how") not in COMBINE_HOW:
                out.append(f"step {i}: combine.how {s.get('how')!r}")
        elif op == "rank":
            if "of" not in s:
                out.append(f"step {i}: rank 는 of 가 필요")
            top, pct = s.get("top"), s.get("top_pct")
            if top is None and pct is None:
                out.append(f"step {i}: rank 는 top 또는 top_pct 가 필요")
            if top is not None and not (isinstance(top, int) and 1 <= top <= 50):
                out.append(f"step {i}: top 은 1~50")
        elif op == "filter":
            cond = s.get("cond")
            if "of" not in s or not isinstance(cond, dict) or len(cond) != 1:
                out.append(f"step {i}: filter 는 of 와 cond 하나가 필요")
            elif next(iter(cond)) not in FILTER_KEYS:
                out.append(f"step {i}: filter.cond {next(iter(cond))!r}")
        elif op == "pick":
            regs = s.get("regions")
            if "of" not in s or not isinstance(regs, dict) or "from_step" not in regs:
                out.append(f"step {i}: pick 은 of 와 regions.from_step 이 필요")
    if gets > MAX_GETS:
        out.append(f"get 이 {gets}개 (최대 {MAX_GETS})")
    if gets == 0:
        out.append("get 이 없음")
    return out


# ---------------------------------------------------------------- 연산
def _resolve_period(t: Table, expr: str) -> str:
    """'latest' · 'latest-N' · '2019' 를 표의 시점 이름으로"""
    ps = t.periods()
    if not ps:
        raise PlanError("missing_data", None, "값이 없는 표")
    e = str(expr).strip()
    if e == "latest":
        return ps[-1]
    if e.startswith("latest-"):
        n = int(e.split("-", 1)[1])
        base = _period_key(ps[-1])[0] - n
        for p in ps:
            if _period_key(p)[0] == base:
                return p
        raise PlanError(
            "out_of_range", None, f"{e} 에 해당하는 시점이 표에 없음 ({ps[0]}~{ps[-1]})"
        )
    if e in ps:
        return e
    for p in ps:
        if _period_key(p)[0] == _period_key(e)[0]:
            return p
    raise PlanError("out_of_range", None, f"시점 {e} 가 표에 없음 ({ps[0]}~{ps[-1]})")


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _change_value(a: float | None, b: float | None, how: str) -> float | None:
    if a is None or b is None:
        return None
    if how == "diff":
        return _round(b - a)
    if how == "rate":
        return None if a == 0 else _round((b - a) / a * 100)
    return None if a == 0 else _round(b / a)  # ratio


def change(t: Table, from_: str, to: str, how: str = "diff") -> Table:
    p0, p1 = _resolve_period(t, from_), _resolve_period(t, to)
    label = f"{p0}→{p1}"
    rows = {r: {label: _change_value(v.get(p0), v.get(p1), how)} for r, v in t.rows.items()}
    unit = {"diff": "%p" if t.unit == "%" else t.unit, "rate": "%", "ratio": "배"}[how]
    how_ko = {"diff": "변화", "rate": "증감률", "ratio": "배"}[how]
    out = Table(
        rows, t.indicator, unit, f"{t.label or t.indicator or ''} {p0}→{p1} {how_ko}".strip()
    )
    # 답에 "41.36% → 45.83% (+4.47%p)"처럼 전 · 후 값을 같이 보이도록 남긴다
    out.meta["from_to"] = {r: (v.get(p0), v.get(p1)) for r, v in t.rows.items()}
    out.meta["from_to_unit"] = t.unit
    return out


def _combine_value(a: float | None, b: float | None, how: str, scale: float) -> float | None:
    if a is None or b is None:
        return None
    if how == "sum":
        return _round(a + b)
    if how == "ratio":
        return None if b == 0 else _round(a / b * scale)
    return _round(a - b)  # diff · which_larger


def combine(a: Table, b: Table, how: str, scale: float = 1.0) -> Table:
    """같은 지역 · 같은 시점끼리. 한쪽이 시점 하나뿐이면 그 값을 모든 시점에 쓴다"""
    regions = [r for r in a.regions() if r in b.rows]
    if not regions:
        raise PlanError("missing_data", None, "두 결과에 겹치는 지역이 없음")
    rows: dict[str, dict[str, float | None]] = {}
    larger: dict[str, str] = {}
    for r in regions:
        ra, rb = a.rows[r], b.rows[r]
        periods = [p for p in ra if p in rb] or (
            list(ra) if len(rb) == 1 else list(rb) if len(ra) == 1 else []
        )
        row: dict[str, float | None] = {}
        for p in periods:
            va = ra.get(p, next(iter(ra.values())) if len(ra) == 1 else None)
            vb = rb.get(p, next(iter(rb.values())) if len(rb) == 1 else None)
            row[p] = _combine_value(va, vb, how, scale)
            if how == "which_larger" and va is not None and vb is not None:
                larger[r] = "a" if va > vb else "b" if vb > va else "같음"
        rows[r] = row
    if not any(v is not None for row in rows.values() for v in row.values()):
        raise PlanError("missing_data", None, "두 결과에 겹치는 시점이 없음")
    unit = {
        "sum": a.unit,
        "diff": a.unit,
        "which_larger": a.unit,
        "ratio": "%" if scale == 100 else "배",
    }
    how_ko = {"sum": "합", "diff": "차이", "ratio": "비율", "which_larger": "비교"}[how]
    out = Table(rows, None, unit[how], f"{a.label or ''} − {b.label or ''} {how_ko}".strip())
    if how in ("sum", "ratio"):
        out.label = (
            f"{a.label or ''} {'+' if how == 'sum' else '÷'} {b.label or ''} {how_ko}".strip()
        )
    if larger:
        out.meta["larger"] = larger
    return out


def rank(
    t: Table, top: int | None = None, top_pct: float | None = None, ascending: bool = False
) -> Table:
    scored = [(r, t.single(r)) for r in t.regions() if r != NATION and t.single(r) is not None]
    if not scored:
        raise PlanError("missing_data", None, "순위를 매길 값이 없음")
    scored.sort(key=lambda x: (x[1], x[0]), reverse=not ascending)
    n = top if top is not None else max(1, math.ceil(len(scored) * float(top_pct) / 100))
    chosen = scored[:n]
    rows = {r: {t.last_period(): v} for r, v in chosen}
    out = Table(rows, t.indicator, t.unit, t.label, order=[r for r, _ in chosen])
    out.meta["total_regions"] = len(scored)
    out.meta["ascending"] = ascending
    return out


def filter_(t: Table, cond: dict[str, Any]) -> Table:
    key, arg = next(iter(cond.items()))
    values = {r: t.single(r) for r in t.regions() if t.single(r) is not None}
    if key in ("above", "below"):
        base_region = arg if isinstance(arg, str) else NATION
        base = t.single(base_region)
        if base is None:
            raise PlanError("missing_data", None, f"비교 기준 {base_region} 의 값이 없음")
        keep = [
            r
            for r, v in values.items()
            if r != base_region and (v > base if key == "above" else v < base)
        ]
    elif key in ("top_pct", "bottom_pct"):
        ranked = sorted(
            (r for r in values if r != NATION), key=lambda r: values[r], reverse=key == "top_pct"
        )
        n = max(1, math.ceil(len(ranked) * float(arg) / 100))
        keep = ranked[:n]
    else:
        x = float(arg)
        test = {
            "ge": lambda v: v >= x,
            "gt": lambda v: v > x,
            "le": lambda v: v <= x,
            "lt": lambda v: v < x,
        }[key]
        keep = [r for r, v in values.items() if r != NATION and test(v)]
    rows = {r: dict(t.rows[r]) for r in keep}
    out = Table(rows, t.indicator, t.unit, t.label, order=keep)
    out.meta["cond"] = cond
    out.meta["total_regions"] = len([r for r in values if r != NATION])
    return out


def pick(t: Table, regions: list[str]) -> Table:
    rows = {r: dict(t.rows[r]) for r in regions if r in t.rows}
    if not rows:
        raise PlanError("missing_data", None, "앞 step 이 남긴 지역이 이 표에 없음")
    return Table(rows, t.indicator, t.unit, t.label, order=[r for r in regions if r in rows])


# ---------------------------------------------------------------- 실행
async def run(plan: dict, get: Getter, trace: dict | None = None) -> tuple[Table, list[Table]]:
    """steps 를 순서대로. (답 step 의 표, 모든 step 의 표). 실패하면 PlanError(step 번호 포함)"""
    steps = plan.get("steps") or []
    problems = validate(steps, len(plan.get("indicators") or []))
    if problems:
        raise PlanError("bad_plan", None, " · ".join(problems))
    results: list[Table] = []
    log: list[dict] = []
    if trace is not None:
        trace["plan_exec"] = log
    for i, s in enumerate(steps):
        op = s["op"]
        try:
            if op == "get":
                regs = s.get("regions", "all")
                if isinstance(regs, dict):
                    regs = results[regs["from_step"]].regions()
                    if not regs:
                        raise PlanError("missing_data", i, "앞 step 이 남긴 지역이 없음")
                t = await get(s["indicator"], regs, list(s["years"]))
                if not t.rows:
                    raise PlanError("missing_data", i, "가져온 값이 없음")
            elif op == "change":
                t = change(results[s["of"]], s["from"], s["to"], s.get("how", "diff"))
            elif op == "combine":
                t = combine(results[s["a"]], results[s["b"]], s["how"], float(s.get("scale") or 1))
            elif op == "rank":
                t = rank(results[s["of"]], s.get("top"), s.get("top_pct"), bool(s.get("ascending")))
            elif op == "filter":
                t = filter_(results[s["of"]], s["cond"])
            else:
                t = pick(results[s["of"]], results[s["regions"]["from_step"]].regions())
        except PlanError as e:
            if e.step is None:
                e.step = i
            log.append({"step": i, "op": op, "status": e.code, "why": e.why})
            raise
        results.append(t)
        log.append(
            {
                "step": i,
                "op": op,
                "status": "ok",
                "regions": len(t.rows),
                "periods": t.periods()[:4],
            }
        )
    answer = plan.get("answer") or {}
    k = answer.get("step", len(results) - 1)
    if not (isinstance(k, int) and 0 <= k < len(results)):
        raise PlanError("bad_plan", None, f"answer.step {k!r} 가 없음")
    return results[k], results
