"""차트 정보: 계산 결과(qa.ask의 result)에서 화면이 그릴 차트를 고른다

서버는 그림을 만들지 않는다. 차트 종류와 행(이름, 값)만 JSON으로 넘기고 화면(Recharts)이 그린다.
값은 계산 결과에 있는 숫자를 그대로 옮긴다 (새로 계산하거나 채우지 않는다).

  결과에 있는 것        차트             이유
  series (연도별 값)    line  꺾은선     시간에 따른 변화
  지역마다 series       line  여러 꺾은선 지역별 추이 (series에 지역 이름, 행마다 지역별 값)
  values (지역별 값)    bar   가로 막대  크기 비교 (값이 큰 순)
  ranking (순위)        bar   가로 막대  크기 비교 (순위 순, 많으면 위에서 MAX_BARS개)
  parts (구성비)        parts 나뉜 막대  전체가 어떻게 나뉘는지
  값 하나·답변 불가     없음            숫자 하나는 문장이 더 잘 전한다

"막대그래프로 보여줘" 같은 뒤 요청은 다시 조회하지 않고 화면이 종류만 바꾼다 (line ↔ bar).
"""

from typing import Any

MAX_BARS = 15  # 시군구 순위(200개가 넘는다)는 위에서 이만큼만 그린다
MAX_PARTS = 6  # 나뉜 막대는 조각이 이보다 많으면 읽기 어렵다 → 가로 막대로
MAX_LINES = 6  # 꺾은선은 이보다 많으면 색을 구분하기 어렵다 (색을 돌려 쓰지 않는다)


def _title(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p)


def _when(result: dict) -> str | None:
    """제목의 시점: 연간 값은 "2025년", 최신 달 값은 "2026년 10월 말" (indicators._mark_monthly)"""
    if result.get("as_of"):
        return result["as_of"]
    return f"{result['year']}년" if result.get("year") else None


def _sex(result: dict) -> str | None:
    sex = result.get("sex")
    return f"({sex})" if sex and sex != "계" else None


def _line(result: dict) -> dict | None:
    rows = [{"label": f"{s['year']}", "value": s["value"]} for s in result["series"]]
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
    """여러 지역의 추이 → 꺾은선 여러 개. 행은 연도, values는 series(지역) 순서의 값.
    지역마다 남녀를 또 나눈 결과처럼 겹이 더 있으면 그리지 않는다 (문장·수치 목록이 전한다)"""
    parts = result["split"]
    if len(parts) > MAX_LINES or any(p.get("split") or not p.get("series") for p in parts):
        return None
    names = [p.get("region") or "" for p in parts]
    by_year = [{s["year"]: s["value"] for s in p["series"]} for p in parts]
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
    }


def _bar(result: dict, items: list[dict], order: str | None = None) -> dict | None:
    rows = [{"label": i["region"], "value": i["value"]} for i in items if i["value"] is not None]
    if len(rows) < 2:
        return None
    note = None
    if len(rows) > MAX_BARS:
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
        if p.get("share") is not None
    ]
    if len(rows) < 2:
        return None
    year = _when(result)
    return {
        # 조각이 많으면 나뉜 막대 대신 가로 막대 (큰 순)
        "type": "parts" if len(rows) <= MAX_PARTS else "bar",
        "title": _title(year, result.get("region"), result.get("indicator"), _sex(result)),
        "category": "항목",
        "rows": rows if len(rows) <= MAX_PARTS else sorted(rows, key=lambda r: -r["value"]),
        "unit": "%",
        "count_unit": result.get("count_unit") or "명",
    }


def build(result: dict | None) -> dict | None:
    """계산 결과 → 차트 정보. 그릴 것이 없으면 None (값 하나, 답변 불가, 여러 지표를 묶은 답)"""
    if not result or result.get("status") != "ok":
        return None
    if result.get("split"):
        chart = _lines(result) if result.get("split_by") == "region" else None
    elif result.get("series"):
        chart = _line(result)
    elif result.get("values"):
        chart = _bar(result, result["values"])
    elif result.get("ranking"):
        chart = _bar(result, result["ranking"], result.get("order"))
    elif result.get("parts"):
        chart = _parts(result)
    else:
        chart = None
    if chart is None:
        return None
    return {"unit": result.get("unit") or "", "note": None, "count_unit": None, **chart}
