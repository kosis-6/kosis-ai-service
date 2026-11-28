"""답의 대표 수치 (figure): 여러 모양의 result를 한 가지 형식으로 바꾼다

result는 질문 유형마다 모양이 다르다 (value / series / values / ranking / parts / members / split).
화면의 수치 강조 · 계산 근거 표시 · 보고서에 담기는 이 함수가 만든 figure 하나만 읽는다.
답 문장이나 계산은 바꾸지 않는다: result에 이미 있는 값만 옮긴다.

  figure_of(result, parsed)   result → figure (수치로 답하지 못했거나 묶음 질문이면 None)
  reportable(res)             보고서에 담을 수 있는 답인가 (명세서 2.4)
"""

from typing import Any


def _when(r: dict) -> str | None:
    if r.get("period_label") or r.get("as_of"):
        return str(r.get("period_label") or r.get("as_of"))
    if r.get("year") is not None:
        return f"{r['year']}년"
    return None


def _ages(parsed: dict | None) -> str | None:
    bands = (parsed or {}).get("ages") or []
    words = []
    for band in bands:
        try:
            lo, hi = band
        except (TypeError, ValueError):
            words.append(str(band))
            continue
        if lo is not None and hi is None:
            words.append(f"{lo}세 이상")
        elif lo is None and hi is not None:
            words.append(f"{hi}세 이하")
        elif lo == hi:
            words.append(f"{lo}세")
        else:
            words.append(f"{lo}~{hi}세")
    return ", ".join(words) or None


def is_projection(r: dict) -> bool:
    """결과만 보고 추계값인지 판단한다 (DB 조회 없음)"""
    if isinstance(r.get("is_projection"), bool):
        return r["is_projection"]
    if r.get("scenario") or r.get("future_after"):
        return True
    return "추계" in str(r.get("basis") or "")


def _region(r: dict, rows: list[dict]) -> str | None:
    if r.get("region"):
        return str(r["region"])
    if r.get("level"):  # 순위: "시도", "서울특별시 시군구"
        return str(r["level"])
    names = [x["label"] for x in rows]
    return ", ".join(names) or None


def _operands(r: dict) -> list[dict]:
    return [
        {
            "label": str(o.get("label") or ""),
            "value": o.get("value"),
            "unit": str(o.get("unit") or ""),
            "conditions": None,  # 단계 2: 각 값의 지역 · 연도 · 모집단
            # 두 표를 엮은 계산은 값마다 자기 표가 있다. 없으면(한 표의 두 칸) 결과의 표
            "tbl_id": o.get("tbl_id") or r.get("tbl_id"),
        }
        for o in r.get("operands") or []
    ]


def _value_kind(r: dict) -> str:
    op = r.get("op")
    if op in ("−", "-"):
        return "diff"
    if op == "+" or r.get("members"):
        return "sum"
    if r.get("kind") == "ratio" or r.get("unit") == "%":
        return "ratio"
    return "value"


def _shape(r: dict) -> tuple[str, Any, list[dict], dict | None] | None:
    """(kind, 대표 값, rows, change). 모르는 모양이면 None"""
    if r.get("series"):
        rows = [{"label": f"{s['year']}년", "value": s.get("value")} for s in r["series"]]
        return "series", rows[-1]["value"], rows, r.get("change")
    if r.get("values"):
        rows = [{"label": v["region"], "value": v.get("value")} for v in r["values"]]
        return "compare", None, rows, None
    if r.get("ranking"):
        rows = [
            {"label": v["region"], "value": v.get("value"), "rank": v.get("rank")}
            for v in r["ranking"]
        ]
        target = (r.get("target") or {}).get("value")
        return "ranking", target, rows, None
    if r.get("parts"):
        rows = [
            {"label": p["label"], "value": p.get("share"), "count": p.get("count")}
            for p in r["parts"]
        ]
        return "parts", None, rows, None
    if "value" in r:
        rows = [{"label": m["region"], "value": m.get("value")} for m in r.get("members") or []]
        return _value_kind(r), r.get("value"), rows, None
    return None


def figure_of(result: dict | None, parsed: dict | None = None) -> dict | None:
    r = result or {}
    if r.get("status") != "ok" or r.get("split"):
        return None
    shape = _shape(r)
    if shape is None:
        return None
    kind, value, rows, change = shape
    unit = str(r.get("unit") or "")
    if kind == "parts":
        unit = "%"
    period = _when(r)
    if kind == "series" and rows:
        period = f"{rows[0]['label']}~{rows[-1]['label']}"
    sex = r.get("sex")
    conditions = {
        "region": _region(r, rows),
        "period": period,
        "sex": None if sex in (None, "계") else str(sex),
        "ages": _ages(parsed),
        "population": None,  # 단계 2: 표의 집계 대상
    }
    indicator = str(r.get("subject") or r.get("indicator") or "")
    label = " ".join(x for x in (period, conditions["region"], indicator) if x)
    tbl_ids = [r["tbl_id"]] if r.get("tbl_id") else []
    tbl_ids += [s["tbl_id"] for s in r.get("extra_sources") or [] if s.get("tbl_id")]
    return {
        "label": label,
        "indicator": indicator,
        "value": value,
        "unit": unit,
        "kind": kind,
        "conditions": conditions,
        "is_projection": is_projection(r),
        "formula": r.get("formula"),
        "operands": _operands(r),
        "rows": rows,
        "change": change,
        "tbl_ids": list(dict.fromkeys(tbl_ids)),
    }


def _judge_ok(res: dict) -> bool:
    record = (res.get("trace") or {}).get("answer_judge")
    if not record:  # 판정을 끈 경우 · 판정 대상이 아닌 답
        return True
    if record.get("replaced"):  # 다시 계산한 답이 판정을 통과해 처음 답을 바꿨다
        return True
    first = record.get("first") or {}
    return first.get("verdict") == "ok"


def reportable(res: dict) -> bool:
    """보고서에 담을 수 있는 답인가 (명세서 2.4). figure 가 붙은 뒤에 부른다"""
    fig = res.get("figure")
    if not fig or res.get("status") != "ok":
        return False
    if (res.get("result") or {}).get("reused"):
        return False
    if res.get("also_missing"):  # 묶음 질문 중 함께 물은 지표를 답하지 못함
        return False
    if not fig["tbl_ids"]:
        return False
    if fig["value"] is None:
        if not fig["rows"] or any(x.get("value") is None for x in fig["rows"]):
            return False
    if any(o["value"] is None for o in fig["operands"]):
        return False
    return _judge_ok(res)
