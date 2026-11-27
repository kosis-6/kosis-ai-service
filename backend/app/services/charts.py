from math import isfinite
from typing import Any

MAX_BARS = 15
MAX_PARTS = 6
MAX_LINES = 6  # 겹쳐 그리는 꺾은선 수 (화면의 선 색 수와 같다)


def crowded(chart: dict) -> bool:
    """겹쳐 그리기엔 선이 많은 꺾은선: 지역별 작은 꺾은선으로 그리고 막대 보기는 없다.
    서버가 여기서 정해 차트에 싣고(crowded) 화면은 그 값을 따른다. 값이 없는 차트(이 값이 생기기
    전에 저장된 대화)는 같은 기준으로 센다"""
    if "crowded" in chart:
        return bool(chart["crowded"])
    return chart.get("type") == "line" and len(chart.get("series") or []) > MAX_LINES


def _title(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p)


def _when(result: dict) -> str | None:
    if period := result.get("period_label") or result.get("as_of"):
        return period
    return f"{result['year']}년" if result.get("year") else None


def _sex(result: dict) -> str | None:
    sex = result.get("sex")
    return f"({sex})" if sex and sex != "계" else None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def omission_reason(result: dict | None) -> str | None:
    if not result or result.get("status") != "ok" or result.get("split_by") != "region":
        return None
    parts = result.get("split") or []
    if not parts:
        return None
    first = parts[0]
    for part in parts:
        if part.get("split") or not part.get("series"):
            return "지역별 결과를 하나의 추이 차트로 묶기 어려워 수치로 표시합니다."
        if (
            not part.get("indicator")
            or part.get("unit") is None
            or (part.get("indicator"), part.get("unit"), part.get("sex") or "계")
            != (first.get("indicator"), first.get("unit"), first.get("sex") or "계")
        ):
            return "지역별 지표·단위·성별 기준이 달라 차트는 생략하고 수치를 표시합니다."
        series = part["series"]
        years = [s.get("year") for s in series]
        if any(not isinstance(y, int) or isinstance(y, bool) for y in years) or len(
            set(years)
        ) != len(years):
            return "연도 정보를 하나의 추이로 정렬할 수 없어 차트는 생략하고 수치를 표시합니다."
        if sum(_number(s.get("value")) for s in series) < 2:
            return (
                "지역별로 값이 있는 연도가 2개 이상이어야 하므로 차트는 생략하고 수치를 표시합니다."
            )
    return None


def _line(result: dict) -> dict | None:
    rows = [
        {"label": f"{s['year']}", "value": s["value"] if _number(s.get("value")) else None}
        for s in result["series"]
    ]
    if sum(r["value"] is not None for r in rows) < 2:
        return None
    years = f"{rows[0]['label']}~{rows[-1]['label']}년"
    return {
        "type": "line",
        "title": _title(result.get("region"), result.get("indicator"), _sex(result), years),
        "category": "연도",
        "rows": rows,
    }


def _lines(result: dict) -> dict | None:
    parts = result["split"]
    if omission_reason(result):
        return None
    names = [p.get("region") or "" for p in parts]
    by_year = [
        {s["year"]: s["value"] if _number(s.get("value")) else None for s in p["series"]}
        for p in parts
    ]
    years = sorted({y for values in by_year for y in values})
    rows = [
        {"label": f"{y}", "value": None, "values": [values.get(y) for values in by_year]}
        for y in years
    ]
    if len(rows) < 2:
        return None
    first = parts[0]
    span = f"{rows[0]['label']}~{rows[-1]['label']}년"
    return {
        "type": "line",
        "title": _title(first.get("indicator"), _sex(first), span),
        "category": "연도",
        "rows": rows,
        "series": names,
        "unit": first.get("unit") or "",
        "crowded": len(names) > MAX_LINES,
    }


def _keep_ends(rows: list[dict]) -> list[dict]:
    """MAX_BARS개로 줄이되 가장 높은 곳 · 가장 낮은 곳은 남긴다 (답 문장이 둘을 말한다).
    11/27 차트 점검: 17개 시도 비교에서 '가장 낮은 지역은 세종'이라 하고 막대에는 세종이 없었다"""
    ends = {
        max(range(len(rows)), key=lambda i: rows[i]["value"]),
        min(range(len(rows)), key=lambda i: rows[i]["value"]),
    }
    rest = [i for i in range(len(rows)) if i not in ends][: MAX_BARS - len(ends)]
    return [rows[i] for i in sorted(ends | set(rest))]


def _bar(
    result: dict, items: list[dict], order: str | None = None, ranked: bool = False
) -> dict | None:
    rows = [{"label": i["region"], "value": i["value"]} for i in items if _number(i.get("value"))]
    if len(rows) < 2:
        return None
    note = None
    if len(rows) > MAX_BARS and not ranked:
        note = (
            f"전체 {len(rows)}개 지역 중 {MAX_BARS}개만 그렸습니다"
            " (가장 높은 곳과 가장 낮은 곳은 포함)."
        )
        rows = _keep_ends(rows)
    elif len(rows) > MAX_BARS:
        note = f"전체 {len(rows)}개 지역 중 위에서 {MAX_BARS}개만 그렸습니다."
        rows = rows[:MAX_BARS]
    year = _when(result)
    return {
        "type": "bar",
        "title": _title(year, result.get("indicator"), _sex(result), order and f"({order})"),
        "category": "지역",
        "rows": rows,
        "note": note,
    }


def _parts(result: dict) -> dict | None:
    rows = [
        {"label": p["label"], "value": p["share"], "count": p.get("count")}
        for p in result["parts"]
        if _number(p.get("share"))
    ]
    if len(rows) < 2:
        return None
    year = _when(result)
    note = None
    if len(rows) > MAX_PARTS:
        # 항목이 많으면 비율이 큰 순서의 막대. 지역 막대처럼 MAX_BARS개까지만 그린다
        # (11/27 차트 점검: 이 경로만 자르지 않아 항목 수만큼 막대가 나갔다)
        rows = sorted(rows, key=lambda r: -r["value"])
        if len(rows) > MAX_BARS:
            note = f"전체 {len(rows)}개 항목 중 비율이 큰 {MAX_BARS}개만 그렸습니다."
            rows = rows[:MAX_BARS]
    return {
        "type": "parts" if len(rows) <= MAX_PARTS else "bar",
        "title": _title(year, result.get("region"), result.get("indicator"), _sex(result)),
        "category": "항목",
        "rows": rows,
        "unit": "%",
        "count_unit": result.get("count_unit") or "명",
        "note": note,
    }


def build(result: dict | None) -> dict | None:
    if not result or result.get("status") != "ok":
        return None
    if result.get("split"):
        chart = _lines(result) if result.get("split_by") == "region" else None
    elif result.get("series"):
        chart = _line(result)
    elif result.get("values"):
        chart = _bar(result, result["values"])
    elif result.get("ranking"):
        chart = _bar(result, result["ranking"], result.get("order"), ranked=True)
    elif result.get("parts"):
        chart = _parts(result)
    else:
        chart = None
    if chart is None:
        return None
    return {"unit": result.get("unit") or "", "note": None, "count_unit": None, **chart}
