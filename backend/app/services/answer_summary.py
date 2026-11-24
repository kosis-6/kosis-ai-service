import re
from typing import Any

from app.services import qa

MAX_POINTS = 4
MAX_HEADLINE = 110
MAX_CAUTIONS = 2
MAX_LISTED = 5
SENTENCE_END = re.compile(r"(?<=니다\.)\s+")
ONE_LINE = re.compile(r"한\s*(줄|마디|문장)")


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_END.split(text.strip()) if s]


def _steps(series: list[dict]) -> list[tuple[int, str]]:
    return [
        (
            s["year"],
            "증가" if s["diff_from_prev"] > 0 else "감소" if s["diff_from_prev"] < 0 else "같음",
        )
        for s in series
        if "diff_from_prev" in s
    ]


def _trend_headline(r: dict) -> str:
    name, unit, c = qa._subject(r), r.get("unit", ""), r["change"]
    first, last = r["series"][0]["value"], r["series"][-1]["value"]
    span = f"{c['from_year']}~{c['to_year']}년"
    ends = f"{qa._val(first, unit, full=True)} → {qa._val(last, qa._tail(unit))}"
    head = f"{r['region']}의 {qa._josa(name, '은는')}"
    if c["diff"] == 0:
        return f"{head} {c['from_year']}년과 {c['to_year']}년이 같습니다({ends})."
    direction = "증가" if c["diff"] > 0 else "감소"
    rate = f", {c['rate']:+}{c['rate_unit']}" if "rate" in c else ""
    return (
        f"{head} {span}에 {qa._val(abs(c['diff']), c['diff_unit'])} "
        f"{direction}했습니다({ends}{rate})."
    )


def _trend_points(r: dict) -> list[str]:
    asked, unit, c = r.get("asked") or {}, r.get("unit", ""), r["change"]
    points = _sentences(qa._trend_answer(r))
    steps = _steps(r["series"])
    dirs = {d for _, d in steps}
    steady = len(dirs) == 1 and "같음" not in dirs
    if len(r["series"]) > 2:
        if steady:
            points.append(f"이 기간 동안 매년 {next(iter(dirs))}했습니다.")
        elif turn := qa._turning_year(steps):
            year, before, after = turn
            points.append(f"{year}년까지 {before}하다가 이후 {after}했습니다.")
        else:
            ups = sum(d == "증가" for _, d in steps)
            downs = sum(d == "감소" for _, d in steps)
            points.append(f"앞 해보다 증가한 해는 {ups}번, 감소한 해는 {downs}번입니다.")
        hi, lo = (r.get("highest_years") or [None])[0], (r.get("lowest_years") or [None])[0]
        if hi and lo and not steady and not asked.get("extreme"):
            many = r.get("kind") == "count"
            top, bottom = ("많았던", "적었던") if many else ("높았던", "낮았던")
            points.append(
                f"가장 {top} 해는 {hi['year']}년({qa._val(hi['value'], unit)}), "
                f"가장 {bottom} 해는 {lo['year']}년({qa._val(lo['value'], unit)})입니다."
            )
        per = c.get("per_year")
        if per and not asked.get("per_year"):
            word = "늘었습니다" if per > 0 else "줄었습니다"
            if unit == "세":
                word = "늦어졌습니다" if per > 0 else "빨라졌습니다"
            size = round(abs(per)) if r.get("kind") == "count" else abs(per)
            points.append(
                f"해마다 평균 {qa._val(size, c['diff_unit'])}씩 {word}"
                "((마지막 값 − 처음 값) ÷ 햇수)."
            )
    return points


def _region_trends(result: dict) -> tuple[str, list[str]] | None:
    parts = result["split"]
    changes = [p.get("change") for p in parts]
    if not all(p.get("series") and c for p, c in zip(parts, changes, strict=True)):
        return None
    if len({(c["from_year"], c["to_year"]) for c in changes}) != 1:
        return None
    name, n = qa._subject(parts[0]), len(parts)
    span = f"{changes[0]['from_year']}~{changes[0]['to_year']}년"
    ups = sum(c["diff"] > 0 for c in changes)
    downs = sum(c["diff"] < 0 for c in changes)
    if ups == n or downs == n:
        word = "증가" if ups == n else "감소"
        headline = f"{span}에 {qa._josa(name, '은는')} {n}곳 모두 {word}했습니다."
    else:
        counts = [f"{ups}곳이 증가" if ups else "", f"{downs}곳이 감소" if downs else ""]
        if same := n - ups - downs:
            counts.append(f"{same}곳은 변동이 없습니다")
            tail = ", ".join(filter(None, counts)) + "."
        else:
            tail = ", ".join(filter(None, counts)) + "했습니다."
        headline = f"{span}에 {qa._josa(name, '은는')} {n}곳 중 {tail}"

    points = []
    by_rate = all("rate" in c for c in changes)
    key = "rate" if by_rate else "diff"
    unit = changes[0]["rate_unit"] if by_rate else changes[0]["diff_unit"]
    moved = sorted(
        ((p["region"], c[key]) for p, c in zip(parts, changes, strict=True) if c["diff"] != 0),
        key=lambda x: x[1],
    )
    if moved:
        low, high = moved[0], moved[-1]
    if len(moved) == 1:
        word = "늘어난" if ups else "줄어든"
        points.append(f"{word} 곳은 {moved[0][0]}({qa._signed(moved[0][1], unit)})입니다.")
    elif moved and low[1] != high[1]:
        if ups and downs:
            a, b = ("가장 크게 늘어난 곳", high), ("가장 크게 줄어든 곳", low)
        elif ups:
            a, b = ("가장 크게 늘어난 곳", high), ("가장 적게 늘어난 곳", low)
        else:
            a, b = ("가장 크게 줄어든 곳", low), ("가장 적게 줄어든 곳", high)
        points.append(
            f"{a[0]}은 {a[1][0]}({qa._signed(a[1][1], unit)}), "
            f"{b[0]}은 {b[1][0]}({qa._signed(b[1][1], unit)})입니다."
        )
    if end := qa._region_trend_end(result).strip():
        points.append(end)
    return headline, points


def _composition(r: dict) -> tuple[str, list[str]]:
    parts = r["parts"]
    unit = r.get("count_unit", "명")
    biggest = max(parts, key=lambda p: p["share"])
    focus = (r.get("asked") or {}).get("focus") or []
    if r.get("subject"):
        whole = f"{qa._when(r)} {r['region']}의 {r['subject']}"
        asked = [p for p in parts if p["label"] in focus]
    else:
        whole = f"{qa._when(r)} {r['region']} {qa.SEX_WORDS.get(r.get('sex'), '')}인구"
        asked = []
    if asked:
        p = asked[0]
        headline = (
            f"{whole} 중 {qa._josa(p['label'], '은는')} {p['share']}%({p['count']:,}{unit})입니다."
        )
        rest = [x for x in parts if x is not p]
        points = [f"가장 큰 비중은 {biggest['label']} {biggest['share']}%입니다."]
        if biggest is p:
            points = []
    else:
        headline = f"{whole}에서 가장 큰 비중은 {biggest['label']} {biggest['share']}%입니다."
        rest = [x for x in parts if x is not biggest]
        points = []
    if rest:
        listed = ", ".join(f"{x['label']} {x['share']}%" for x in rest[:MAX_LISTED])
        more = f" 등 {len(rest)}개 항목" if len(rest) > MAX_LISTED else ""
        points.append(f"나머지는 {listed}{more}입니다.")
    points.append(f"전체는 {r['total']:,}{unit}입니다.")
    return headline, points


def _compare_points(r: dict) -> list[str]:
    unit = r.get("unit", "")
    valid = [v for v in r["values"] if v["value"] is not None]
    points = []
    if len(valid) > 2 and not (r.get("asked") or {}).get("ratio"):
        listed = ", ".join(
            f"{v['region']}({qa._val(v['value'], unit)})" for v in valid[:MAX_LISTED]
        )
        more = f" 등 {len(valid)}곳" if len(valid) > MAX_LISTED else ""
        points.append(f"높은 순으로 {listed}{more}입니다.")
    if missing := [v["region"] for v in r["values"] if v["value"] is None]:
        points.append(f"{', '.join(missing[:MAX_LISTED])}의 자료는 없습니다.")
    return points


def _rank_points(r: dict) -> list[str]:
    items, unit = r["ranking"], r.get("unit", "")
    target = r.get("target")
    if target and target.get("rank") not in (None, 1) and items:
        top = items[0]
        return [f"1위는 {top['region']}({qa._val(top['value'], unit)})입니다."]
    if target or len(items) < 3 or len(items) != r.get("total_regions"):
        return []
    listed = ", ".join(f"{x['region']}({qa._val(x['value'], unit)})" for x in items[1:3])
    return [f"2~3위는 {listed}입니다."]


def _leaf(r: dict) -> tuple[str, list[str]]:
    if "scenario" in r:
        return _sentences(qa._projection_text(r))[0], []
    if "series" in r:
        return _trend_headline(r), _trend_points(r)
    if "parts" in r:
        return _composition(r)
    if "values" in r:
        first, *rest = _sentences(qa._compare_text(r))
        return first, rest + _compare_points(r)
    if "ranking" in r:
        first, *rest = _sentences(qa._rank_text(r))
        return first, _rank_points(r) + rest
    first, *rest = _sentences(qa._value_text(r))
    members = r.get("members") or []
    if 1 < len(members) <= MAX_LISTED:
        unit = r.get("unit", "")
        listed = ", ".join(f"{m['region']} {qa._val(m['value'], unit)}" for m in members)
        rest.append(f"{listed}을 더한 값입니다.")
    return first, rest


def _values_together(parts: list[dict]) -> str | None:
    plain = all(
        p.get("value") is not None and not any(k in p for k in ("series", "scenario", "members"))
        for p in parts
    )
    names = [qa._subject(p) for p in parts]
    if not plain or len(set(names)) != len(names):
        return None
    if len({(qa._when(p), p.get("region")) for p in parts}) != 1:
        return None
    units = [p.get("unit", "") for p in parts]
    listed = ", ".join(
        f"{qa._josa(n, '은는')} "
        + (
            qa._val(p["value"], qa._tail(u))
            if i and u == units[0]
            else qa._val(p["value"], u, full=True)
        )
        for i, (n, p, u) in enumerate(zip(names, parts, units, strict=True))
    )
    return f"{qa._when(parts[0])} {parts[0]['region']}의 {listed}입니다."


def _trends_together(leaves: list[dict]) -> tuple[str, list[str]] | None:
    changes = [r.get("change") for r in leaves]
    if not all(r.get("series") and c and c["diff"] for r, c in zip(leaves, changes, strict=True)):
        return None
    names = [qa._subject(r) for r in leaves]
    if len(set(names)) != len(names) or len({r.get("region") for r in leaves}) != 1:
        return None
    if len({(c["from_year"], c["to_year"]) for c in changes}) != 1:
        return None
    span = f"{changes[0]['from_year']}~{changes[0]['to_year']}년에 "
    moved = [
        f"{qa._josa(n, '은는')} {span if i == 0 else ''}"
        f"{qa._val(abs(c['diff']), c['diff_unit'])} {'증가' if c['diff'] > 0 else '감소'}"
        for i, (n, c) in enumerate(zip(names, changes, strict=True))
    ]
    points = []
    for n, r, c in zip(names, leaves, changes, strict=True):
        unit = r.get("unit", "")
        first, last = r["series"][0]["value"], r["series"][-1]["value"]
        rate = f", {c['rate']:+}{c['rate_unit']}" if "rate" in c else ""
        ends = f"{qa._val(first, unit, full=True)} → {qa._val(last, qa._tail(unit))}{rate}"
        points.append(f"{n}: {ends}")
    return f"{leaves[0]['region']}의 {', '.join(moved)}했습니다.", points


def _split(result: dict) -> tuple[str, list[str]]:
    parts = result["split"]
    if result.get("split_by") == "region" and (grouped := _region_trends(result)):
        return grouped
    if together := _values_together(parts):
        tail = _sentences(qa._split_text(result))[-1]
        return together, [tail] if tail.endswith("더 높습니다.") else []
    if 1 < len(qa._leaves(result)) <= MAX_POINTS and (
        together := _trends_together(qa._leaves(result))
    ):
        return together
    leaves = [_leaf(p) for p in qa._leaves(result)]
    headlines = [h for h, _ in leaves]
    if len(leaves) == 2 and len(" ".join(headlines)) <= MAX_HEADLINE:
        return " ".join(headlines), []
    return headlines[0], headlines[1:]


def _cautions(result: dict) -> list[str]:
    out = []
    if result.get("causal_question"):
        out.append(result["causal_question"])
    leaves = qa._leaves(result)
    if any("scenario" in r for r in leaves):
        out.append("실제 통계가 아닌 추계값입니다.")
    out += [r["answer_note"] for r in leaves if r.get("answer_note")]
    for note in qa._source_result(result).get("notes") or []:
        out.append(note if note.endswith(".") else f"{note}.")
    return list(dict.fromkeys(out))[:MAX_CAUTIONS]


def build(result: dict[str, Any] | None) -> dict[str, Any] | None:
    result = result or {}
    cautions: list[str] = []
    suggestion = result.get("suggestion") or {}
    if result.get("status") == "out_of_range" and suggestion.get("status") == "ok":
        cautions.append("물은 연도는 수록 기간 밖이라 장래인구추계 값으로 답했습니다.")
        result = suggestion
    if result.get("status") != "ok":
        return None
    headline, points = _split(result) if result.get("split") else _leaf(result)
    if lead := result.get("lead"):
        basis = f"{result['lead_basis']} " if result.get("lead_basis") else ""
        headline = f"{lead} {basis}{headline}"
    cautions = list(dict.fromkeys(cautions + _cautions(result)))[:MAX_CAUTIONS]
    return {
        "headline": headline,
        "points": list(dict.fromkeys(points))[:MAX_POINTS],
        "cautions": cautions,
    }


def one_line(question: str) -> bool:
    return bool(ONE_LINE.search(question))


def text(summary: dict[str, Any], one_line: bool = False) -> str:
    if one_line:
        return summary["headline"]
    lines = [summary["headline"]]
    lines += [f"- {p}" for p in summary.get("points") or []]
    lines += [f"※ {c}" for c in summary.get("cautions") or []]
    return "\n".join(lines)
