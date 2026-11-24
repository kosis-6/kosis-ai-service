"""지표가 여럿인 질문 계산 (구현 5): 통계표 찾기 꾸러미의 지표마다 계산해 combine.op대로 합친다

통계표 찾기가 질문을 지표 여럿으로 읽으면 꾸러미(found["handoff"])의 targets가 지표 수만큼 온다.
지표마다 계산하고 (사전에 있는 지표는 사전으로, 없는 지표는 넘기기 계산으로), 계획이 정한
combine.op대로 합친다.

  none · compare      지표마다 결과를 나란히 (qa._combine과 같은 split 모양).
                      compare는 단위가 같은 값 둘이면 차이 한 줄
  diff · ratio        두 값이 다 나오고 단위가 맞으면 빼거나 나눈다 (cross_indicator.combine_value).
                      안 되면 나란히 + 직접 계산하지 않았다는 안내
                      compare · diff에서 두 지표의 조사 기준이 다르면 (인구총조사 인구와
                      주민등록인구) 빼거나 견주지 않고 나란히 + 직접 비교하지 않는다는 안내
                      (cross_indicator.basis_gap). ratio는 다른 조사끼리도 나눈다
  rank                지표마다 순위를 나란히
  filter_then_lookup  거르는 지표(role filter)의 1위 지역을 구한 뒤 그 지역으로 다른 지표를 계산
  intersect           지역을 거르는 질문("인구도 많고 출생아도 많은 시")이면 두 순위 상위 지역의
                      교집합. 한 집단이 두 특성을 함께 가진 질문("1인 노인 가구 중 연금 수급자")은
                      두 표의 숫자로 만들 수 없어 함께 집계한 통계가 없다고 안내한다
                      (숫자를 만들지 않는다)

연도를 말하지 않은 값 질문에서 지표마다 가장 최근 시점이 다르면 (주민등록인구 2026년 10월,
등록외국인 2025년, 인구총조사 2020년) 모두 있는 해로 다시 계산해 맞춘다 (사전 지표끼리는
qa._common_year · cross_indicator가 같은 일을 한다). 다시 계산이 하나라도 안 되면 처음 값 그대로.

일부 지표만 계산되면 계산된 지표는 숫자로, 못 한 지표는 표 안내 한 줄로 같은 답에 넣는다.
지표마다 대안 제안 LLM은 부르지 않는다 (HCX · Gemma 호출이 지표 수만큼 늘지 않게).
"""

import asyncio
import logging
import re

from starlette.concurrency import run_in_threadpool

from app.services import cross_indicator, dynamic_indicator, indicators, qa
from app.services.josa import josa

logger = logging.getLogger(__name__)

MAX_TARGETS = 3  # 한 질문에서 계산할 지표 수 (그 밖은 표 안내)
INTERSECT_TOP = 5  # 지역 교집합: 지표마다 상위 몇 곳을 볼지 (질문에 개수가 없으면)
KINDS = ("value", "trend", "compare", "rank", "composition")
# 지역을 거르는 교집합 ("인구도 많고 출생아도 많은 시", "두 지표 모두 높은 지역")
REGION_FILTER = re.compile(
    r"(시|군|구|도|지역|곳|도시|시도|시군구)\S*\s*(는|은|가|이|를)?\s*(어디|알려|$)|어디"
)
RATE_WORDS = re.compile(r"비율|비중|%|퍼센트|몇\s*프로")
# "1인 가구가 가장 많은 시도의 통근 인구": 1위 지역을 고른 뒤 그 지역의 값 (계획이 intersect로 봐도)
TOP_REGION_OF = re.compile(
    r"(가장|제일)\s*(많|높|적|낮|큰|작)\S*\s*(시도|시군구|지역|도시|시|군|구|곳|동네)\S*?\s*의"
)


def _ok(res: dict | None) -> bool:
    return bool(
        res and res.get("status") == "ok" and not res.get("clarification") and res.get("result")
    )


def says_age(text: str) -> bool:
    """말에 연령 조건이 있는가 ("70세 이상 1인 가구", "노인 1인 가구"). "전체 1인 가구"는 없다"""
    from app.services import agegroup

    return bool(qa.extract_ages(text) or agegroup.resolve(text))


def without_unsaid_age(parsed: dict, phrase: str) -> dict:
    """두 값을 나눌 때 한쪽 말이 연령을 말하지 않았으면 그쪽 해석에서 질문의 연령 조건을 뺀다
    (질문의 연령은 다른 쪽 말의 것이다: "1인 가구 중 70세 이상" → 분모 "전체 1인 가구"는 전 연령)"""
    if says_age(phrase) or not (parsed.get("ages") or parsed.get("age_group")):
        return parsed
    return {**parsed, "ages": None, "age_group": None}


def target_parsed(parsed: dict, target: dict, kind: str | None = None) -> dict:
    """지표 하나를 계산할 해석: 질문 전체 해석에서 지표 이름만 그 지표로 바꾼다"""
    span = target.get("span") or target.get("question") or ""
    # 계획의 dict(사전 지표 이름)가 있으면 그것으로. "젊은 사람 대비 노인 부담"처럼 구절로는
    # 사전에 못 잇지만 계획이 search를 '노년부양비'로 쓴 경우 (11/24 재계획 시험)
    name = target.get("dict") or indicators.canonical_name(span) or span
    q = {
        **parsed,
        "indicator": name,
        "term": span,
        "indicators": None,
        "cross": None,
        "dynamic_spec": None,
        "grounded": None,
    }
    if target.get("unsaid_age"):
        q.update(ages=None, age_group=None)
    k = kind or parsed.get("type")
    if k == "compare" and len(parsed.get("regions") or []) <= 1:
        k = "value"  # 지역 하나의 '비교'는 지표끼리 비교: 지표마다 값
    q["type"] = k if k in KINDS else "value"
    return q


async def _one(question: str, parsed: dict, found: dict, target: dict, kind: str | None = None):
    """지표 하나 계산 → (답 또는 None, 기록). 사전에 있으면 사전, 없으면 캐시 → 넘기기 계산"""
    sub: dict = {"dynamic": []}
    p = target_parsed(parsed, target, kind)
    asked = target.get("question") or question
    try:
        if indicators.spec(p["indicator"]) is not None:
            sub["by"] = "dictionary"
            res = await qa.answer(p, sub, asked)
        else:
            sub["by"] = "dynamic"
            res = await dynamic_indicator.cached_answer(asked, p, sub)
            if res is None:
                res = await dynamic_indicator._compute_target(
                    asked, p, found, target, sub, multi=True
                )
    except Exception:
        logger.exception("지표 하나 계산 실패: %s", target.get("span"))
        res = None
    return res, sub


async def _all(question, parsed, found, targets, kind=None):
    return await asyncio.gather(*(_one(question, parsed, found, t, kind) for t in targets))


def _span(target: dict) -> str:
    return target.get("span") or target.get("question") or "그 지표"


def _record(trace: dict, targets: list[dict], outcomes) -> None:
    trace["multi"]["targets"] = [
        {"span": _span(t), "by": sub.get("by"), "status": (res or {}).get("status")}
        for t, (res, sub) in zip(targets, outcomes, strict=True)
    ]
    for _, (_, sub) in zip(targets, outcomes, strict=True):
        trace.setdefault("dynamic", []).extend(sub.get("dynamic") or [])


def _missed_note(target: dict) -> str:
    """계산하지 못한 지표: 표 안내 한 줄 (후보 1순위 표 이름)"""
    cands = target.get("candidates") or []
    where = f" ('{cands[0].get('tbl_nm')}' 표에서 확인할 수 있습니다)" if cands else ""
    return f"{josa(_span(target), '은는')} 통계표에서 바로 계산하지 못했습니다{where}"


def _response(question: str, parsed: dict, result: dict, trace: dict, notes=(), names=()):
    """qa.answer와 같은 모양의 답"""
    lines = [f"{qa.summary(result)}\n\n{qa.render(result)}".strip()]
    lines += [f"※ {n}" for n in notes if n]
    return {
        "status": result.get("status"),
        "answer": "\n".join(lines),
        "parsed": {**parsed, "indicators": list(names) or parsed.get("indicators")},
        "result": result,
        "sources": qa.sources(result),
        "trace": trace,
    }


def _side_by_side(question, parsed, ok, failed, trace, notes=()):
    parts = [r["result"] for _, r in ok]
    first = parts[0]
    result = {
        **{k: first[k] for k in ("source", "basis", "tbl_id", "url") if k in first},
        "status": "ok",
        "split": parts,
        "split_by": "indicator",
    }
    if len(parts) == 1:
        result = parts[0]
    notes = [*notes, *(_missed_note(t) for t in failed)]
    if len(parts) > 1:
        # 비교 한 줄("A가 B보다 N명 많습니다") · 기준 차이 안내를 결과에도 남긴다: 답 판정은 결과만
        # 보므로 답 글에만 있으면 "비교해야 함"으로 막았다 (11/24 replan 손 확인)
        result["notes"] = [n for n in notes if n]
    return _response(question, parsed, result, trace, notes, [_span(t) for t, _ in ok])


def _value(res: dict) -> dict | None:
    """값 질문의 결과 (한 지역 · 한 해 값이 있을 때만)"""
    r = (res or {}).get("result") or {}
    return r if "value" in r and r.get("value") is not None else None


# 값을 나란히 놓거나 서로 계산하는 합치기 (시점을 먼저 맞춘다)
ALIGN_OPS = ("none", "compare", "diff", "ratio", "sum", "which_larger")


def common_year(question: str, parsed: dict, ok) -> int | None:
    """연도를 말하지 않은 값 질문에서 지표마다 답한 시점이 다르면 모두 있는 해.
    맞출 것이 없으면 None. 최신 달 값(as_of)은 연간 값과 섞지 않으므로 연간 자료의
    마지막 해로 본다"""
    if any(parsed.get(k) for k in ("year", "start_year", "end_year")) or qa._says_year(question):
        return None
    rows = [_value(r) for _, r in ok]
    if len(rows) < 2 or not all(rows) or not all(r.get("year") for r in rows):
        return None
    if len({(r["year"], r.get("as_of")) for r in rows}) <= 1:
        return None  # 연도와 기준 달이 모두 같다 (둘 다 2026년 10월 말이면 그대로 둔다)
    year = min(r["year"] for r in rows)
    if any(r.get("as_of") for r in rows):
        year = min(year, indicators._data_years()[1])
    return year


async def _aligned(question: str, parsed: dict, found: dict, ok, year: int):
    """ok였던 지표들을 year로 다시 계산한 결과. 모두 그 해의 값으로 나왔을 때만 돌려준다
    (아니면 None)"""
    asked = {**(parsed.get("asked") or {}), "latest": False}
    # 넘기기 계산은 지표의 조건(target.conditions)에서 연도를 읽는다. 질문 해석에만 넣으면
    # '질문에 없는 연도'로 지워진다 (dynamic_indicator.drop_guessed_year)
    targets = [
        {**t, "conditions": {**(t.get("conditions") or {}), "year": year, "year_end": None}}
        for t, _ in ok
    ]
    outcomes = await _all(question, {**parsed, "year": year, "asked": asked}, found, targets)
    redo = [(t, r) for (t, _), (r, _) in zip(ok, outcomes, strict=True)]
    for _, r in redo:
        row = _value(r) if _ok(r) else None
        if not row or row.get("year") != year or row.get("as_of"):
            return None
    return redo


def _basis_gap(ok) -> str | None:
    """차이 · 비교: 조사 기준이 다른 두 지표가 있으면 그 이유 (cross_indicator.basis_gap)"""
    for i, (ta, ra) in enumerate(ok):
        for tb, rb in ok[i + 1 :]:
            gap = cross_indicator.basis_gap(ra["result"], rb["result"], _span(ta), _span(tb))
            if gap:
                return gap
    return None


def _larger(ok) -> dict | None:
    """which_larger: 두 값 중 큰 쪽 · 차이 · 몇 배 (단위 · 연도가 같을 때만)"""
    if len(ok) != 2:
        return None
    (ta, ra), (tb, rb) = ok
    a, b = _value(ra), _value(rb)
    if not a or not b or a.get("unit") != b.get("unit") or a.get("year") != b.get("year"):
        return None
    if a["value"] == b["value"]:
        return {"larger": None, "gap": 0, "line": f"{_span(ta)}와 {_span(tb)}가 같습니다"}
    (big, hi), (small, lo) = ((ta, a), (tb, b)) if a["value"] > b["value"] else ((tb, b), (ta, a))
    gap = hi["value"] - lo["value"]
    gap_text = f"{gap:,}" if float(gap).is_integer() else f"{round(gap, 2):,}"
    ratio = round(hi["value"] / lo["value"], 2) if lo["value"] else None
    times = f", {ratio}배" if ratio else ""
    return {
        "larger": _span(big),
        "smaller": _span(small),
        "gap": gap,
        "ratio": ratio,
        "line": f"{josa(_span(big), '이가')} {_span(small)}보다 많습니다 "
        f"(차이 {gap_text}{hi.get('unit') or ''}{times})",
    }


def _compare_line(ok) -> str | None:
    """compare: 단위가 같은 값 둘이면 차이 한 줄"""
    if len(ok) != 2:
        return None
    (ta, ra), (tb, rb) = ok
    a, b = _value(ra), _value(rb)
    if not a or not b or a.get("unit") != b.get("unit") or a.get("year") != b.get("year"):
        return None
    gap = a["value"] - b["value"]
    more, less = (_span(ta), _span(tb)) if gap >= 0 else (_span(tb), _span(ta))
    gap = abs(gap)
    text = f"{gap:,}" if float(gap).is_integer() else f"{round(gap, 2):,}"
    return f"{josa(more, '이가')} {less}보다 {text}{a.get('unit') or ''} 많습니다"


def ratio_order(question: str, ta: dict, tb: dict, combine: dict) -> tuple[bool, str]:
    """ratio에서 ta가 분자인가 → (ta가 분자, 정한 근거 'rule' · 'plan' · 'default').
    1. 코드 문형 규칙 (cross_indicator와 같은 기준): 'A는 B의 몇 배' → A ÷ B,
       'A당 B' · 'A 대비 B' · 'A 중 B 비율' → B ÷ A. 질문에서 두 지표 말을 찾았을 때만
    2. 계획의 분자 · 분모 (계획 규칙 7-1, LLM): combine.num · den이 두 지표 span과 맞을 때만
    3. 예전 규칙: 뒤 지표 ÷ 앞 지표"""
    x = cross_indicator
    text = indicators._norm(question)
    sa, sb = indicators._norm(_span(ta)), indicators._norm(_span(tb))
    ia, ib = text.find(sa), text.find(sb)
    if sa and sb and sa != sb and ia >= 0 and ib >= 0:
        (first, f_end), (second, s_start, s_end) = (
            ((ta, ia + len(sa)), (tb, ib, ib + len(sb)))
            if ia < ib
            else ((tb, ib + len(sb)), (ta, ia, ia + len(sa)))
        )
        if s_start >= f_end:
            between, after = text[f_end:s_start], text[s_end:]
            if x.SUBJECT.match(between) and x.TIMES_AFTER.match(after):
                return first is ta, "rule"  # 앞에 말한 지표 ÷ 뒤에 말한 지표
            if (
                x.PER.match(between)
                or x.VERSUS.match(between)
                or (x.WITHIN.match(between) and x.RATE_AFTER.match(after))
            ):
                return second is ta, "rule"  # 뒤에 말한 지표 ÷ 앞에 말한 지표
    num, den = (combine or {}).get("num"), (combine or {}).get("den")
    spans = {_span(ta), _span(tb)}
    if num and den and num != den and {num, den} == spans:
        return num == _span(ta), "plan"
    return False, "default"


def _two(op: str, question: str, parsed: dict, ok, trace: dict, combine=None) -> dict | None:
    """diff · ratio: 두 값이 다 나오고 단위가 맞으면 빼거나 나눈다.
    안 되면 None (나란히 보여 준다)"""
    (ta, ra), (tb, rb) = ok
    a, b = _value(ra), _value(rb)
    if not a or not b:
        return None
    if op == "diff":
        if a.get("unit") != b.get("unit"):
            return None
        c = cross_indicator.Cross(_span(ta), _span(tb), 1, "diff")
        num, den = a, b
    else:
        units = cross_indicator.COUNT_UNITS
        if a.get("unit") not in units or b.get("unit") not in units:
            return None
        a_is_num, by = ratio_order(question, ta, tb, combine or {})
        trace.setdefault("multi", {})["ratio_by"] = by
        (tn, num), (td, den) = ((ta, a), (tb, b)) if a_is_num else ((tb, b), (ta, a))
        scale = 100 if RATE_WORDS.search(question) else 1
        c = cross_indicator.Cross(
            _span(tn), _span(td), scale, "within" if scale == 100 else "versus"
        )
    c.units = {c.num: num.get("unit"), c.den: den.get("unit")}
    result = cross_indicator.combine_value(
        c, {**num, "indicator": c.num}, {**den, "indicator": c.den}, parsed
    )
    if result.get("status") != "ok":
        return None
    result["method"] = "multi"
    return _response(question, parsed, result, trace, (), [c.num, c.den])


async def _filter_then_lookup(question, parsed, found, targets, trace) -> dict | None:
    """거르는 지표의 1위 지역을 구한 뒤 그 지역으로 다른 지표를 계산"""
    flt = next((t for t in targets if t.get("role") == "filter"), targets[0])
    rest = [t for t in targets if t is not flt]
    low = bool(re.search(r"(가장|제일)\s*(적|낮|작)", question))  # 가장 적은 지역의 값
    asked = {**parsed, "top": 1, "regions": [], "ascending": low}
    ((res_f, sub_f),) = await _all(question, asked, found, [flt], "rank")
    trace["multi"]["targets"] = [{"span": _span(flt), "by": sub_f.get("by"), "role": "filter"}]
    ranking = ((res_f or {}).get("result") or {}).get("ranking") or []
    if not _ok(res_f) or not ranking:
        return None
    region = ranking[0]["region"]
    outcomes = await _all(question, {**parsed, "regions": [region]}, found, rest, "value")
    _record_more(trace, rest, outcomes)
    ok = [(t, r) for t, (r, _) in zip(rest, outcomes, strict=True) if _ok(r)]
    failed = [t for t, (r, _) in zip(rest, outcomes, strict=True) if not _ok(r)]
    lead = (
        f"{josa(_span(flt), '이가')} 가장 {'낮은' if low else '높은'} 지역은 {region}입니다. "
        "그 지역의 값을 계산했습니다"
    )
    if not ok:
        return _response(
            question, parsed, res_f["result"], trace, [lead, *map(_missed_note, failed)]
        )
    return _side_by_side(question, parsed, [(flt, res_f), *ok], failed, trace, [lead])


def _record_more(trace: dict, targets, outcomes) -> None:
    trace["multi"].setdefault("targets", [])
    trace["multi"]["targets"] += [
        {"span": _span(t), "by": sub.get("by"), "status": (res or {}).get("status")}
        for t, (res, sub) in zip(targets, outcomes, strict=True)
    ]


async def _intersect(question, parsed, found, targets, trace) -> dict | None:
    """지역을 거르는 교집합만 계산한다. 집단의 교집합은 함께 집계한 통계가 없다고 안내"""
    names = [_span(t) for t in targets]
    if not REGION_FILTER.search(question) and parsed.get("type") != "rank":
        joined = " · ".join(f"'{n}'" for n in names)
        text = (
            f"{joined} 조건을 함께 만족하는 대상을 따로 집계한 통계표를 찾지 못했습니다. "
            "두 통계의 숫자를 더하거나 빼서 만들 수 있는 값이 아니라 숫자를 내지 않습니다."
        )
        recs = found.get("recommendations") or []
        out = {
            "status": "no_joint_table",
            "answer": text,
            "parsed": {**parsed, "indicators": names},
            "result": {"status": "no_joint_table", "reason": text},
            "sources": [],
            "trace": trace,
        }
        if recs:
            out["tables"] = {"status": "ok", "recommendations": recs}
        return out
    top = parsed.get("top") or INTERSECT_TOP
    outcomes = await _all(question, {**parsed, "top": None, "regions": []}, found, targets, "rank")
    _record(trace, targets, outcomes)
    if not all(_ok(r) for r, _ in outcomes):
        return None
    tops = [[row["region"] for row in r["result"].get("ranking") or []][:top] for r, _ in outcomes]
    both = [region for region in tops[0] if all(region in t for t in tops[1:])]
    if both:
        lead = f"{' · '.join(names)} 모두 상위 {top}곳에 드는 지역은 {', '.join(both)}입니다"
    else:
        lead = f"{' · '.join(names)} 모두 상위 {top}곳에 드는 지역은 없습니다"
    parts = []
    for r, _ in outcomes:
        part = dict(r["result"])
        part["ranking"] = (part.get("ranking") or [])[:top]
        parts.append(part)
    result = {"status": "ok", "split": parts, "split_by": "indicator"}
    result.update({k: parts[0][k] for k in ("source", "basis", "tbl_id", "url") if k in parts[0]})
    out = _response(question, parsed, result, trace, (), names)
    out["answer"] = f"{lead}.\n\n{out['answer']}"
    return out


async def answer(question: str, parsed: dict, found: dict, trace: dict) -> dict | None:
    """지표가 여럿인 꾸러미 → 합친 답 (qa.answer와 같은 모양). 하나도 계산하지 못하면 None"""
    h = found["handoff"]
    op = (h.get("combine") or {}).get("op") or "none"
    targets = [t for t in h.get("targets") or [] if (t.get("scope") or "in") == "in"]
    targets = targets[:MAX_TARGETS]
    trace["multi"] = {"op": op, "targets": []}
    if not targets:
        return None
    if op in ("intersect", "rank", "none") and len(targets) >= 2 and TOP_REGION_OF.search(question):
        op = trace["multi"]["op"] = "filter_then_lookup"  # "가장 많은 시도의 B"는 1위 지역의 B
    if op == "filter_then_lookup" and len(targets) >= 2:
        return await _filter_then_lookup(question, parsed, found, targets, trace)
    if op == "intersect" and len(targets) >= 2:
        return await _intersect(question, parsed, found, targets, trace)
    if op == "ratio":
        # "A 중 B 비율": 연령을 말하지 않은 쪽("전체 1인 가구")에는 질문의 연령을 붙이지 않는다.
        # 분모에도 70세 이상을 붙여 182,686 ÷ 182,686 = 100%로 답했다 (11/23)
        targets = [{**t, "unsaid_age": not says_age(_span(t))} for t in targets]
    outcomes = await _all(question, parsed, found, targets)
    _record(trace, targets, outcomes)
    ok = [(t, r) for t, (r, _) in zip(targets, outcomes, strict=True) if _ok(r)]
    failed = [t for t, (r, _) in zip(targets, outcomes, strict=True) if not _ok(r)]
    if not ok:
        return None
    notes = []
    if op in ALIGN_OPS and (year := await run_in_threadpool(common_year, question, parsed, ok)):
        # 지표마다 가장 최근 시점이 달랐다: 다른 해의 값을 나란히 놓거나 서로 계산하지 않는다
        if redo := await _aligned(question, parsed, found, ok, year):
            ok = redo
            notes.append(f"지표마다 가장 최근 자료의 시점이 달라 모두 있는 {year}년으로 맞췄습니다")
        trace["multi"]["aligned"] = {"year": year, "done": bool(redo)}
    gap = None
    if op in ("compare", "diff"):
        # 조사 기준이 다른 통계(인구총조사 인구와 주민등록인구)는 빼거나 크기를 견주지 않고
        # 값과 출처(기준)만 나란히 보여 준다 (답변 정책: 단위·조사 기준 차이)
        if gap := await run_in_threadpool(_basis_gap, ok):
            notes.append(gap)
    if op in ("diff", "ratio") and len(ok) == 2 and not failed and not gap:
        if combined := await run_in_threadpool(
            _two, op, question, parsed, ok, trace, h.get("combine")
        ):
            return combined
        notes.append("두 값의 단위나 기준이 달라 직접 계산하지 않고 나란히 보여 드립니다")
    if op == "sum" and not failed and not gap:
        # 더하기 (재계획 A_10): 같은 시점 · 같은 단위의 건수만. 사전 경로의 합계와 같은 모양
        # (members 가 있어 답 판정 요약에 "합계 — A, B를 더한 값"으로 보인다)
        if total := qa._sum_indicators([r["result"] for _, r in ok], parsed):
            return _response(question, parsed, total, trace, notes, [_span(t) for t, _ in ok])
        notes.append("두 값의 단위나 기준이 달라 더하지 않고 나란히 보여 드립니다")
    verdict = None
    if op in ("compare", "which_larger") and not gap and (line := _compare_line(ok)):
        notes.append(line)
        if op == "which_larger":
            verdict = _larger(ok)
    out = _side_by_side(question, parsed, ok, failed, trace, notes)
    if verdict:
        # 큰 쪽 판정: 결과에 남겨 답 판정이 "비교해야 함"으로 보지 않게 하고, 답 첫 줄로 올린다
        out["result"]["verdict"] = verdict
        out["answer"] = f"{verdict['line']}\n{out['answer']}"
    if gap:
        out["result"]["basis_gap"] = gap  # 요청 기록(result)에 남는다 (cross_indicator와 같은 칸)
    return out
