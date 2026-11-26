"""보고서에 담은 결과끼리 비교할 수 있는지 검사하고, 비교할 수 있는 쌍만 코드가 계산한다
(명세서 5.1)

LLM은 이 결과의 이름 · 조건 · 방향만 보고 문장을 쓴다. 수치는 여기서만 만든다.

  R1 모집단     conditions.population 이 서로 다르면 비교 · 계산 안 함
  R2 단위       단위가 다르면 차이 · 배율 계산 안 함 (나란히 보여 주기만)
  R3 추계       한쪽만 추계값이면 비교는 하되 "추계값과 실측값" 문구가 있어야 함 (writer 가 검사)
  R4 기준 시점  시점이 다르면 지역 비교 안 함. 같은 지표 · 같은 지역이면 변화로 계산
  R5 출처       같은 지표 이름인데 출처 표가 다르면 직접 비교 안 함
  R6 추세       같은 지표 · 지역의 시점이 3개 미만이면(추이 답 제외) "추세" 표현 금지
"""

import re
from itertools import combinations
from typing import Any

from app.services import qa

SPACES = re.compile(r"\s+")
COUNT_TAIL = re.compile(r"(수|건수)$")


def _norm(name: str | None) -> str:
    """'1인 가구 수'와 '1인가구'를 같은 지표로 본다"""
    return COUNT_TAIL.sub("", SPACES.sub("", name or ""))


def items_of(snapshots: list[dict]) -> list[dict]:
    """담긴 순서대로 r1, r2 … 번호를 붙인 항목. figure 가 없는 snapshot 은 건너뛴다"""
    out = []
    for snap in snapshots:
        fig = (snap.get("snapshot") or snap).get("figure")
        if not fig:
            continue
        out.append(
            {
                "id": f"r{len(out) + 1}",
                "turn_id": snap.get("turn_id"),
                "figure": fig,
                "snapshot": snap.get("snapshot") or snap,
            }
        )
    return out


def _cond(it: dict, key: str) -> Any:
    return (it["figure"].get("conditions") or {}).get(key)


def _same_series(a: dict, b: dict) -> bool:
    """같은 지표 · 같은 지역 · 같은 성별 · 연령 (시점만 다를 수 있음)"""
    fa, fb = a["figure"], b["figure"]
    return (
        _norm(fa.get("indicator")) == _norm(fb.get("indicator"))
        and all(_cond(a, k) == _cond(b, k) for k in ("region", "sex", "ages", "population"))
        and fa.get("unit") == fb.get("unit")
    )


def check_pair(a: dict, b: dict) -> tuple[str | None, str | None]:
    """(막는 규칙, 이유). 비교할 수 있으면 (None, None)"""
    fa, fb = a["figure"], b["figure"]
    pa, pb = _cond(a, "population"), _cond(b, "population")
    if pa and pb and pa != pb:
        return "R1", f"모집단이 다릅니다 ('{pa}', '{pb}')"
    if fa.get("unit") != fb.get("unit"):
        return "R2", f"단위가 다릅니다 ({fa.get('unit')}, {fb.get('unit')})"
    same_name = _norm(fa.get("indicator")) == _norm(fb.get("indicator"))
    ta, tb = set(fa.get("tbl_ids") or []), set(fb.get("tbl_ids") or [])
    if same_name and ta and tb and not ta & tb:
        return "R5", "같은 이름의 지표지만 출처 통계표가 달라 정의가 다를 수 있습니다"
    if _cond(a, "period") != _cond(b, "period") and not _same_series(a, b):
        return "R4", "기준 시점이 달라 지역이나 지표끼리 직접 비교하지 않습니다"
    return None, None


def _fmt(v: float | None, unit: str) -> str:
    return qa._val(v, unit)


def _derive(a: dict, b: dict) -> dict | None:
    """비교할 수 있는 쌍의 차이 · 변화. 값이 하나라도 없거나 의미 있는 비교가 아니면 None"""
    fa, fb = a["figure"], b["figure"]
    va, vb = fa.get("value"), fb.get("value")
    if va is None or vb is None or fa.get("kind") in ("compare", "parts"):
        return None
    unit = fa.get("unit") or ""
    pct = unit == "%"
    if _same_series(a, b) and _cond(a, "period") != _cond(b, "period"):
        diff = vb - va
        out = {
            "kind": "change",
            "of": [a["id"], b["id"]],
            "meaning": f"{_cond(a, 'period')} → {_cond(b, 'period')} {fa.get('indicator')} 변화",
            "value": round(diff, 2),
            "unit": "%p" if pct else unit,
            "direction": "증가" if diff > 0 else "감소" if diff < 0 else "같음",
        }
        if not pct and va:
            out["rate"] = round((vb - va) / abs(va) * 100, 1)
        return out
    if _norm(fa.get("indicator")) == _norm(fb.get("indicator")) and (
        _cond(a, "region") != _cond(b, "region") or _cond(a, "sex") != _cond(b, "sex")
    ):
        diff = va - vb
        return {
            "kind": "gap",
            "of": [a["id"], b["id"]],
            "meaning": f"{a['id']}와 {b['id']}의 {fa.get('indicator')} 차이",
            "value": round(abs(diff), 2),
            "unit": "%p" if pct else unit,
            "direction": f"{a['id']}가 더 큼"
            if diff > 0
            else f"{b['id']}가 더 큼"
            if diff < 0
            else "같음",
        }
    return None


def _series_change(it: dict) -> dict | None:
    """추이 결과 하나의 처음 → 끝 변화. steady: 모든 구간이 같은 방향인가 ("꾸준히"의 근거)"""
    f = it["figure"]
    vals = [r.get("value") for r in f.get("rows") or [] if r.get("value") is not None]
    if f.get("kind") != "series" or len(vals) < 2:
        return None
    rows = f["rows"]
    change = f.get("change") or {}
    # 답이 계산한 변화(반올림 전 값)가 있으면 그것을 쓴다: 답 문장과 같은 숫자가 되도록
    diff = change["diff"] if change.get("diff") is not None else vals[-1] - vals[0]
    steps = [b - a for a, b in zip(vals, vals[1:], strict=False)]
    pct = f.get("unit") == "%"
    out = {
        "kind": "series_change",
        "of": [it["id"]],
        "meaning": f"{rows[0]['label']} → {rows[-1]['label']} {f.get('indicator')} 변화",
        "value": round(diff, 2),
        "unit": "%p" if pct else f.get("unit") or "",
        "direction": "증가" if diff > 0 else "감소" if diff < 0 else "같음",
        "steady": all(x > 0 for x in steps) or all(x < 0 for x in steps),
    }
    if not pct and change.get("rate") is not None:
        out["rate"] = round(change["rate"], 1)
    elif not pct and vals[0]:
        out["rate"] = round(diff / abs(vals[0]) * 100, 1)
    return out


def _trend_ok(items: list[dict]) -> bool:
    if any(
        it["figure"].get("kind") == "series" and len(it["figure"].get("rows") or []) >= 3
        for it in items
    ):
        return True
    groups: dict[tuple, set] = {}
    for it in items:
        f = it["figure"]
        key = (_norm(f.get("indicator")), _cond(it, "region"), _cond(it, "sex"), _cond(it, "ages"))
        groups.setdefault(key, set()).add(_cond(it, "period"))
    return any(len(periods) >= 3 for periods in groups.values())


def compare(items: list[dict]) -> dict[str, Any]:
    not_comparable, derived, mixed = [], [], []
    for a, b in combinations(items, 2):
        rule, why = check_pair(a, b)
        if rule:
            not_comparable.append({"pair": [a["id"], b["id"]], "rule": rule, "why": why})
            continue
        if a["figure"].get("is_projection") != b["figure"].get("is_projection"):
            mixed.append([a["id"], b["id"]])
        if d := _derive(a, b):
            d["id"] = f"c{len(derived) + 1}"
            derived.append(d)
    for it in items:
        if d := _series_change(it):
            d["id"] = f"c{len(derived) + 1}"
            derived.append(d)
    trend_ok = _trend_ok(items)
    label = {it["id"]: it["figure"].get("label") or it["id"] for it in items}
    by_id = {it["id"]: it for it in items}

    def worth_saying(x: dict) -> bool:
        """단위나 지표가 아예 다른 쌍은 당연히 비교하지 않으니 한계에 적지 않는다"""
        a, b = (by_id[i]["figure"] for i in x["pair"])
        return x["rule"] in ("R1", "R5") or (
            x["rule"] == "R4" and _norm(a.get("indicator")) == _norm(b.get("indicator"))
        )

    limits = [
        f"'{label[x['pair'][0]]}'와 '{label[x['pair'][1]]}'는 직접 비교하지 않았습니다: {x['why']}."
        for x in not_comparable
        if worth_saying(x)
    ]
    if any(it["figure"].get("is_projection") for it in items):
        limits.append("장래추계 값은 실제 통계가 아닌 예상값이며, 다음 추계 때 바뀔 수 있습니다.")
    if not trend_ok:
        limits.append("같은 지표의 시점이 세 개 미만이라 추세는 판단하지 않았습니다.")
    limits.append("이 보고서는 통계 값만 정리하며, 값이 달라진 원인은 판단하지 않습니다.")
    return {
        "not_comparable": not_comparable,
        "derived": derived,
        "projection_mixed": mixed,
        "trend_ok": trend_ok,
        "limits": limits,
    }


def value_text(it_or_d: dict) -> str:
    """자리표시자 {rN.value} · {cN.value} 에 들어갈 글"""
    if "figure" in it_or_d:
        f = it_or_d["figure"]
        return _fmt(f.get("value"), f.get("unit") or "")
    d = it_or_d
    text = _fmt(d["value"], d.get("unit") or "")
    if d.get("rate") is not None:
        text += f"({d['rate']:+.1f}%)"
    return text
