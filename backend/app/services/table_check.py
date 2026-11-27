"""검사 (docs/agent_redesign_2026-11-26.md 2-5): 표 하나가 질문에 맞는지 코드로 본다 (LLM 없음)

질문 조건을 그 표에 칸 잇기(binding.bind)로 직접 이어 보고 판단한다. 판정의 자유 문장(missing)이
칸 이름에 글자로 닿는지 보던 것은 A1 '다시' 71건에서 닿은 14건이 거의 다 앞 답의 칸 · 주제어였고,
정작 '세쌍둥이 → 삼태아 이상' 같은 일상어는 놓쳤다.

  cells_ok     질문 조건이 모두 칸으로 이어짐 (칸 잇기 BOUND)
  differs      다 이었고, 그 칸이 앞 답이 쓴 칸과 다름 → 칸 잇기만으로 다른 답이 나온다
  cells_exist  못 이은 조건이 있지만 그 표에 '그 구분이 없음'은 아니고 고를 칸이 있음
               → recell 이 뜻으로 이을 여지 (일상어 · 합계 칸 없음 · 칸이 여럿)
  kind_ok      묻는 값의 종류(수 · 비율)와 다시 고른 칸의 단위가 맞음 (단위를 모르면 참)
  topic_ok     질문의 말이 그 표의 어느 칸과는 닿음 (칸 잇기에 topic 조건이 남지 않음)

행동 고르기의 '칸 있음' = differs or cells_exist, '맞음' = kind_ok and topic_ok (agent_action 3-3).
사전 게이트 · 지름길에 쓰는 것(2-10)은 아직 아니다.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services import binding
from app.services import dynamic_indicator as di

# 칸 잇기가 '표에 그 구분 자체가 없다'고 본 조건: 칸을 다시 골라도 못 고친다
NO_AXIS = ("구분이 없음", "시나리오를 말하지 않았는데")
RATIO_ASK = re.compile(
    r"비율|비중|%|퍼센트|몇\s*명\s*꼴|\d*\s*(?:명|인|가구)\s*당|[률율](?:은|는|이|가|을|를|$|\s)"
)
COUNT_ASK = re.compile(
    r"몇\s*(?:명|건|가구|쌍|개|곳)|(?:수|인구|건수)\s*(?:는|가|를|은)?\s*(?:몇|얼마)"
)
COUNT_UNITS = {"명", "건", "가구", "쌍", "개", "호", "천명", "만명"}


@dataclass
class Check:
    tbl_id: str
    cells_ok: bool = False
    differs: bool = False
    cells_exist: bool = False
    kind_ok: bool = True
    topic_ok: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def cells(self) -> bool:
        """칸을 다시 고르면 고칠 수 있나 (행동 고르기의 '칸 있음')"""
        return self.differs or self.cells_exist

    @property
    def fits(self) -> bool:
        return self.kind_ok and self.topic_ok

    def as_trace(self) -> dict[str, Any]:
        return {**asdict(self), "cells": self.cells, "fits": self.fits}


def _unit_kind(unit: str | None) -> str | None:
    u = re.sub(r"[\s()]", "", unit or "")
    if not u:
        return None
    if "%" in u or "당" in u or "‰" in u:
        return "ratio"
    if u in COUNT_UNITS:
        return "count"
    return None


def kind_ok(question: str, unit: str | None) -> bool:
    """비율을 물었는데 건수 단위로, 수를 물었는데 비율 단위로 답했으면 False (2-10 P-103)"""
    kind = _unit_kind(unit)
    if kind is None:
        return True
    ratio = bool(RATIO_ASK.search(question or ""))
    if ratio and kind == "count":
        return False
    return not (not ratio and kind == "ratio" and COUNT_ASK.search(question or ""))


def _differs(b: binding.Binding, used: dict[int, set[str]]) -> bool:
    """칸 잇기가 고른 칸이 앞 답이 쓴 칸과 다른가. 앞 답 칸을 모르면(사전 답) 값으로 고른 칸이
    있으면 다르다고 본다 (앞 답의 칸을 모를 때는 repick 이 값을 비교한다)"""
    for x in b.bound:
        if x.dim is None or not x.codes:
            continue
        have = used.get(x.dim, set())
        if x.by in binding.QUIET:
            if have and have != set(x.codes):
                return True  # 앞 답은 특정 칸('남자'), 질문은 전체('계')
        elif set(x.codes) != have:
            return True
    return False


def check(
    tbl_id: str,
    question: str,
    parsed: dict,
    slots: dict | None = None,
    used: set[tuple[int, str]] | None = None,
    hinted_cells: list[str] | None = None,
) -> Check:
    """표 하나 검사. used는 앞 답의 칸, hinted_cells는 판정에서 지목한 후보의 이름이다.
    표 구조가 없으면 di.schema 가 예외를 낸다 (부르는 쪽이 '검사 못 함'으로 처리)"""
    dims, region_dim, _ = di.schema(tbl_id)
    b = binding.bind(tbl_id, question, parsed, slots, {})
    out = Check(tbl_id, cells_ok=b.status == binding.BOUND)
    by_dim: dict[int, set[str]] = {}
    for no, code in used or set():
        by_dim.setdefault(no, set()).add(code)
    if out.cells_ok:
        out.differs = _differs(b, by_dim)
        out.reasons.append(
            "조건이 모두 칸으로 이어짐" + (" · 앞 답과 다른 칸" if out.differs else "")
        )
    else:
        hard = [u for u in b.unbound if any(w in u.why for w in NO_AXIS)]
        open_dims = [d for d in dims if d.no != region_dim and len(d.codes) > 1]
        out.cells_exist = not hard and bool(open_dims)
        left = ", ".join(f"{u.cond}={u.value or '-'}({u.why})" for u in b.unbound)
        out.reasons.append(f"못 이은 조건: {left}")
        if hard:
            out.reasons.append("표에 그 구분이 없음 → 칸으로 못 고침")
    out.topic_ok = not any(u.cond == "topic" for u in b.unbound)
    if not out.topic_ok:
        out.reasons.append("질문의 말이 표의 어느 칸과도 닿지 않음")
    if RATIO_ASK.search(question) or COUNT_ASK.search(question):
        picks = b.picks()
        items = picks.pop(0, [])
        picks.pop(region_dim, None)
        hinted_items = [
            code
            for d in dims
            if d.no == 0
            for code, name in d.codes.items()
            if name in (hinted_cells or [])
        ]
        units = [di._unit(tbl_id, item, picks)[0] for item in hinted_items or items]
        out.kind_ok = all(kind_ok(question, unit) for unit in units)
        if not out.kind_ok:
            shown = ", ".join(dict.fromkeys(unit for unit in units if unit))
            out.reasons.append(f"묻는 값의 종류와 다시 고른 칸의 단위({shown})가 다름")
    return out
