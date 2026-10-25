"""질문 → 답변 (하위 태스크 1~5 연결)

  1. 질문 이해        LLM이 질문을 JSON(질문 유형, 지표, 지역, 연도)으로 바꾼다
  2~4. 판단·조회·계산  코드(indicators.py)가 계산하고, status로 답변 가능 여부를 알려준다
  5. 답변 생성        코드가 질문 유형별 템플릿에 계산 결과를 채워 문장을 만들고 (summary),
                      숫자 목록·주의사항·출처를 붙인다 (render)

LLM은 1단계(질문 해석)에만 쓴다. 답변 문장은 템플릿이라 숫자·지표 정의가 틀릴 수 없고,
질문 하나에 HCX를 한 번만 부른다.
답변할 수 없는 질문은 status(답변 불가 사유)별로 정해진 안내 문장을 쓴다.

LLM을 부르는 함수(understand, ask)는 async, 나머지는 DB·계산만 하는 일반 함수다.

API: POST /ask
실행: python -m scripts.ask --question "2025년 서울과 부산 고령인구 비율 비교"
      python -m scripts.ask --question "..." --debug   (중간 결과 JSON도 출력)
"""

import json
import re
from datetime import date

from starlette.concurrency import run_in_threadpool

from app.prompts.qa import PARSE_SYSTEM
from app.services import analysis, indicators
from app.services.hcx_client import HcxClient

MODEL = "HCX-007"


async def _chat(prompt, system, temperature=0.1):
    """HCX-007 호출. 설정은 기존 pipeline/clova.py 기본값과 같다"""
    result = await HcxClient().chat(
        MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        thinking={"effort": "low"},
        topP=0.8,
        topK=0,
        maxCompletionTokens=5120,
        temperature=temperature,
        repetitionPenalty=1.1,
    )
    return result.content


async def understand(question):
    """1단계: 질문 → dict. JSON을 못 읽으면 type이 parse_error"""
    lo, hi = await run_in_threadpool(indicators._data_years)
    system = PARSE_SYSTEM.replace("{INDICATORS}", ", ".join(indicators.INDICATORS)).replace(
        "{LATEST}", str(hi)
    )
    text = await _chat(question, system)
    m = re.search(r"\{.*\}", text, re.S)  # 코드 블록(```json)으로 감싸 오는 경우 대비
    try:
        parsed = json.loads(m.group(0))
    except (AttributeError, json.JSONDecodeError):
        return {"type": "parse_error", "raw": text}
    return await run_in_threadpool(correct, question, parsed)


# ---- 질문 해석 보정: LLM 해석을 질문 원문과 대조해 바로잡는다 ------------------------------

GENDER_WORDS = {"여": re.compile(r"여성|여자"), "남": re.compile(r"남성|남자")}
# 남녀를 함께 묻는 표현 (구성비 질문이면 성별 구성)
SEX_SPLIT = re.compile(r"남녀|성별|(남성|남자).*(여성|여자)|(여성|여자).*(남성|남자)")
# 긴 표현부터 찾는다 ("재작년" 안에 "작년"이 있음)
RELATIVE_YEARS = [("재작년", -2), ("지난해", -1), ("작년", -1), ("올해", 0), ("금년", 0)]
AGE_BAND = re.compile(r"\d+(대|세)")
NO_REGION_TYPES = {"rank", "other", "parse_error"}


def correct(question, parsed, today=None):
    """같은 질문도 실행마다 HCX 해석이 달라서, 질문 원문에서 확실히 알 수 있는 것은 코드가 맞춘다.

    - 지표: 질문에 지표 이름·별칭이 하나만 있으면 그 지표
      (예: "고령화율"을 노령화지수로 해석한 경우)
    - 성별: "여성/여자", "남성/남자" 중 하나만 있으면 그 성별
    - 구성비 기준: 구성비 질문에 "남녀", "성별"이 있으면 성별 구성
    - 지역: "전국"이 있는데 지역이 비었으면 전국
    - 연도: "작년", "올해" 같은 상대 표현은 오늘 날짜 기준
    고친 내용은 parsed["corrections"]에 남긴다.
    """
    today = today or date.today()
    kind = parsed.get("type")
    fixes = []

    def fix(key, value, reason):
        fixes.append(f"{key}: {parsed.get(key)!r} → {value!r} ({reason})")
        parsed[key] = value

    if kind not in ("other", "parse_error"):
        spans = indicators.find_in_text(question)
        names = {name for *_, name in spans}
        # 지표 표현 말고 "20대", "65세" 같은 연령 조건이 더 있으면 지원 지표와 뜻이 달라서 맡긴다
        rest = indicators._norm(question)
        for start, end, _ in sorted(spans, reverse=True):
            rest = rest[:start] + " " + rest[end:]
        if len(names) == 1 and not AGE_BAND.search(rest):
            name = names.pop()
            current = indicators.canonical_name(parsed.get("indicator") or "")
            # "인구"는 거의 모든 질문에 있어서, HCX가 다른 지표를 골랐으면 총인구로 바꾸지 않는다
            # (예: "부산 인구에서 고령층이 차지하는 비중" → 고령인구 비율)
            if current != name and not (name == "총인구" and current is not None):
                fix("indicator", name, "질문의 지표 표현")

    genders = [sex for sex, pattern in GENDER_WORDS.items() if pattern.search(question)]
    if len(genders) == 1 and parsed.get("sex") in (None, "", "계"):
        fix("sex", genders[0], "질문의 성별 표현")
    if kind == "composition" and parsed.get("by") != "sex" and SEX_SPLIT.search(question):
        fix("by", "sex", "질문의 남녀 구성 표현")

    if "전국" in question and not parsed.get("regions") and kind not in NO_REGION_TYPES:
        fix("regions", ["전국"], "질문의 '전국'")

    for word, offset in RELATIVE_YEARS:
        if word in question:
            year = today.year + offset
            if kind not in ("trend", "other", "parse_error") and parsed.get("year") != year:
                fix("year", year, f"'{word}' = {today.year}년 기준")
            break

    if fixes:
        parsed["corrections"] = fixes
    return parsed


def compute(q):
    """2~4단계: 질문 분석 결과 → indicators.py 계산 결과"""
    kind = q.get("type")
    ind = q.get("indicator") or ""
    regions = q.get("regions") or []
    sex = q.get("sex") or "계"
    lo, hi = indicators._data_years()
    year = q.get("year") or hi

    if kind == "parse_error":
        return {
            "status": "parse_error",
            "reason": "질문을 이해하지 못했습니다. 지역·연도·지표를 넣어 다시 질문해 주세요.",
        }
    if kind == "other":
        return {
            "status": "not_supported_question",
            "reason": "지역별 인구통계 질문에만 답할 수 있습니다.",
            "supported": list(indicators.INDICATORS),
        }

    causal = kind == "causal"
    if causal:  # 답변 불가 사례 4: 인과 판단 없이 수치 변화만 제시
        kind = "trend" if regions else "rank"

    if kind == "composition":  # 구성비는 지표가 아니라 총인구를 나눠 보므로 지표 확인을 건너뜀
        if not regions:
            return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
        by = "sex" if q.get("by") == "sex" else "age"
        try:
            return indicators.composition(regions[0], year, by, sex)
        except Exception as e:
            return {"status": "error", "reason": f"계산 중 오류가 났습니다: {e}"}

    if indicators._get_indicator(ind)[0] is None:  # 지역보다 지표를 먼저 확인
        return {
            "status": "unknown_indicator",
            "indicator": ind,
            "supported": list(indicators.INDICATORS),
        }

    try:
        if kind in ("value", "trend", "compare") and not regions:
            return {"status": "missing_region", "reason": "어느 지역인지 질문에 없습니다."}
        if kind == "value":
            result = indicators.value(ind, regions[0], year, sex)
        elif kind == "trend":
            start, end = q.get("start_year") or hi - 5, q.get("end_year") or hi
            result = indicators.trend(ind, regions[0], start, end, sex)
        elif kind == "compare":
            result = indicators.compare(ind, regions, year, sex)
        elif kind == "rank":
            result = indicators.rank(
                ind,
                year,
                q.get("level") or "시도",
                q.get("sido"),
                q.get("top"),
                bool(q.get("ascending")),
                sex,
            )
        else:
            return {"status": "parse_error", "reason": f"알 수 없는 질문 유형: {kind}"}
    except Exception as e:  # 계산 중 오류도 답변 단계에서 안내
        return {"status": "error", "reason": f"계산 중 오류가 났습니다: {e}"}

    if result.get("series"):
        _add_yearly_change(result)
    if causal:
        result["causal_question"] = (
            "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 수치 변화만 보여드립니다."
        )
    return result


def _add_yearly_change(result):
    """추이 결과에 전년 대비 변화를 넣는다"""
    kind = indicators.INDICATORS[result["indicator"]]["kind"]
    for prev, cur in zip(result["series"], result["series"][1:], strict=False):
        if prev["value"] is None or cur["value"] is None:
            continue
        diff = analysis.change(prev["value"], cur["value"])
        cur["diff_from_prev"] = int(diff) if kind == "count" else round(diff, 2)
        if kind == "count":
            cur["rate_from_prev"] = round(analysis.growth_rate(prev["value"], cur["value"]), 2)


# ---- 답변 문장: 질문 유형별 템플릿에 계산 결과를 채운다 ----------------------------------

SEX_WORDS = {"남": "남성 ", "여": "여성 "}

# 답변 불가 사유(status)별 안내. reason이 있으면 그 뒤에 붙인다
REFUSAL_GUIDE = {
    "parse_error": "",
    "not_supported_question": "아래 지원 지표로 지역·연도를 넣어 질문해 주세요.",
    "missing_region": "지역을 넣어 다시 질문해 주세요.",
    "unknown_region": "시도나 시군구 이름으로 다시 질문해 주세요.",
    "ambiguous_region": "아래 후보 중 하나로 다시 질문해 주세요.",
    "out_of_range": "수록 기간 안의 연도로 다시 질문해 주세요.",
    "region_not_in_year": "그 지역이 있던 연도로 다시 질문해 주세요.",
    "missing_data": "다른 연도나 지역으로 질문해 주세요.",
    "error": "",
}


def _josa(word, pair):
    """받침이 있으면 pair[0], 없으면 pair[1]을 붙인다. pair 예: "은는", "이가" """
    last = word[-1] if word else ""
    if "가" <= last <= "힣":
        return word + (pair[0] if (ord(last) - ord("가")) % 28 else pair[1])
    return f"{word}{pair[0]}({pair[1]})"


def _val(v, unit):
    """문장 속 값. 설명형 단위(예: "유소년인구 100명당 명")는 뜻풀이 문장으로 따로 쓰므로 뺀다"""
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if " " in unit else f"{v:,}{unit}"


def _meaning(name, v):
    """값의 뜻풀이 문장 (노령화지수처럼 단위만으로 뜻을 알기 어려운 지표)"""
    meaning = indicators.INDICATORS.get(name, {}).get("meaning")
    if not meaning or v is None:
        return ""
    return f" {meaning.format(value=_val(v, ''))}이라는 뜻입니다."


def _subject(r):
    """성별을 붙인 지표 이름 (예: 여성 고령인구 비율)"""
    return SEX_WORDS.get(r.get("sex"), "") + r.get("indicator", "")


def _value_text(r):
    name, unit = _subject(r), r.get("unit", "")
    if r["value"] is None:
        return f"{r['year']}년 {r['region']}의 {_josa(name, '은는')} 자료가 없습니다."
    return (
        f"{r['year']}년 {r['region']}의 {_josa(name, '은는')} {_val(r['value'], unit)}입니다."
        + _meaning(r["indicator"], r["value"])
    )


def _projection_text(r):
    return (
        f"{r['year']}년 {r['region']}의 {r['indicator']} 장래인구추계({r['scenario']}) 값은 "
        f"{_val(r['value'], r.get('unit', ''))}입니다. 실제 통계가 아닌 추계값입니다."
    )


def _trend_text(r):
    name, unit, c = _subject(r), r.get("unit", ""), r["change"]
    first, last = r["series"][0]["value"], r["series"][-1]["value"]
    text = (
        f"{r['region']}의 {_josa(name, '은는')} {c['from_year']}년 {_val(first, unit)}, "
        f"{c['to_year']}년 {_val(last, unit)}입니다. "
    )
    if c["diff"] == 0:
        return text + f"{c['from_year']}년과 같습니다."
    direction = "증가" if c["diff"] > 0 else "감소"
    text += f"{c['from_year']}년 대비 {_val(abs(c['diff']), c['diff_unit'])} {direction}했습니다"
    if "rate" in c:
        text += f"(증감률 {c['rate']:+}{c['rate_unit']})"
    text += "."
    yearly = {
        "증가" if s["diff_from_prev"] > 0 else "감소" if s["diff_from_prev"] < 0 else "같음"
        for s in r["series"]
        if "diff_from_prev" in s
    }
    if len(r["series"]) > 2 and yearly == {direction}:
        text += f" 이 기간 동안 매년 {direction}했습니다."
    return text


def _compare_text(r):
    name, unit = _subject(r), r.get("unit", "")
    valid = [v for v in r["values"] if v["value"] is not None]
    if len(valid) < 2:
        return f"{r['year']}년 {name} 값이 있는 지역이 둘 이상이 아니어서 비교할 수 없습니다."
    high, low = valid[0], valid[-1]
    if r["gap"] == 0:
        return (
            f"{r['year']}년 {_josa(name, '은는')} {_josa(high['region'], '과와')} "
            f"{_josa(low['region'], '이가')} 같습니다."
        )
    gap = _val(r["gap"], r["gap_unit"])
    if len(valid) == 2:
        return (
            f"{r['year']}년 {_josa(name, '은는')} {_josa(high['region'], '이가')} "
            f"{low['region']}보다 {gap} 높습니다"
            f"({high['region']} {_val(high['value'], unit)}, "
            f"{low['region']} {_val(low['value'], unit)})."
        )
    return (
        f"{r['year']}년 {_josa(name, '이가')} 가장 높은 지역은 "
        f"{high['region']}({_val(high['value'], unit)}), 가장 낮은 지역은 "
        f"{low['region']}({_val(low['value'], unit)})이며, 차이는 {gap}입니다."
    )


def _rank_text(r):
    name, unit = _subject(r), r.get("unit", "")
    items = r["ranking"]
    if not items:
        return f"{r['year']}년 {name} 순위를 매길 자료가 없습니다."
    order = "낮은" if r["order"] == "낮은 순" else "높은"
    head = f"{r['year']}년 {_josa(name, '이가')} 가장 {order} {r['level']}"
    listed = ", ".join(f"{x['region']}({_val(x['value'], unit)})" for x in items)
    if 1 < len(items) < r["total_regions"]:  # "3곳" 처럼 개수를 물은 경우
        return f"{head} {len(items)}곳은 {listed}입니다."
    top = items[0]
    return f"{_josa(head, '은는')} {top['region']}({_val(top['value'], unit)})입니다."


def _composition_text(r):
    by = "성별" if r["by"] == "sex" else "연령대별"
    whom = SEX_WORDS.get(r.get("sex"), "")
    listed = ", ".join(f"{p['label']} {p['share']}%" for p in r["parts"])
    biggest = max(r["parts"], key=lambda p: p["share"])
    return (
        f"{r['year']}년 {r['region']} {whom}인구 {r['total']:,}명의 {by} 구성은 {listed}입니다. "
        f"가장 큰 비중은 {biggest['label']}입니다."
    )


def _refusal_text(r):
    status = r.get("status")
    if status == "unknown_indicator":
        return (
            f"'{r.get('indicator')}'은(는) 아직 지원하지 않는 지표입니다. "
            "아래 지원 지표로 다시 질문해 주세요."
        )
    parts = [r.get("reason") or "답변할 수 없는 질문입니다.", REFUSAL_GUIDE.get(status, "")]
    return " ".join(p for p in parts if p)


def summary(r):
    """계산 결과 → 답변 문장 (LLM을 거치지 않는다)"""
    status = r.get("status")
    if status == "ok":
        if "scenario" in r:
            text = _projection_text(r)
        elif "series" in r:
            text = _trend_text(r)
        elif "values" in r:
            text = _compare_text(r)
        elif "ranking" in r:
            text = _rank_text(r)
        elif "parts" in r:
            text = _composition_text(r)
        else:
            text = _value_text(r)
        if r.get("causal_question"):
            text = f"{r['causal_question']} {text}"
        return text
    if status == "out_of_range" and r.get("suggestion", {}).get("status") == "ok":
        return f"{r['reason']} 대신 장래인구추계 값을 보여드립니다. " + _projection_text(
            r["suggestion"]
        )
    return _refusal_text(r)


# ---- 숫자 목록: 코드가 계산 결과에서 그대로 만든다 -----------------------------------


def _num(v, unit):
    if v is None:
        return "자료 없음"
    if isinstance(v, float) and v.is_integer():  # 추계값 등 2242904.0 → 2,242,904
        v = int(v)
    # 긴 단위(예: "유소년인구 100명당 명")만 띄어 쓴다
    return f"{v:,}{unit}" if len(unit) <= 2 else f"{v:,} ({unit})"


def _signed(v, unit):
    return f"{v:+,}{unit}"


def _body(r):
    """status가 ok인 계산 결과 → 숫자 줄 목록. 값 하나짜리(조회·추계)는 문장에 있어 비운다"""
    unit = r.get("unit", "")
    name = r.get("indicator", "")
    if "series" in r:  # 추이
        lines = [f"{r['region']} {name} ({r['sex']})"]
        for s in r["series"]:
            line = f"- {s['year']}년: {_num(s['value'], unit)}"
            if "diff_from_prev" in s:
                ch = _signed(s["diff_from_prev"], r["change"]["diff_unit"])
                if "rate_from_prev" in s:
                    ch += f", {_signed(s['rate_from_prev'], '%')}"
                line += f" (전년 대비 {ch})"
            lines.append(line)
        c = r["change"]
        total = _signed(c["diff"], c["diff_unit"])
        if "rate" in c:
            total += f", {_signed(c['rate'], c['rate_unit'])}"
        lines.append(f"- {c['from_year']}년 → {c['to_year']}년 전체 변화: {total}")
        return lines
    if "values" in r:  # 지역 비교
        lines = [f"{r['year']}년 {name} ({r['sex']}, 높은 순)"]
        lines += [f"- {v['region']}: {_num(v['value'], unit)}" for v in r["values"]]
        if r.get("gap") is not None:
            lines.append(
                f"- 가장 높은 {r['highest']}와 가장 낮은 {r['lowest']}의 차이: "
                f"{_num(r['gap'], r['gap_unit'])}"
            )
        return lines
    if "ranking" in r:  # 순위
        lines = [
            f"{r['year']}년 {name} {r['level']} 순위 ({r['order']}, 전체 {r['total_regions']}곳 중)"
        ]
        lines += [f"{x['rank']}. {x['region']}: {_num(x['value'], unit)}" for x in r["ranking"]]
        return lines
    if "parts" in r:  # 구성비
        lines = [f"{r['region']} {r['year']}년 인구 구성 ({r['sex']}, 전체 {r['total']:,}명)"]
        lines += [f"- {p['label']}: {p['count']:,}명 ({p['share']}%)" for p in r["parts"]]
        return lines
    return []


def _source_result(result):
    """출처를 보여줄 계산 결과 (수록 기간 밖이라 추계값을 대신 보여주면 추계 결과)"""
    suggestion = result.get("suggestion", {})
    if result.get("status") == "out_of_range" and suggestion.get("status") == "ok":
        return suggestion
    return result


def render(result):
    """계산 결과 → 답변 아래에 붙일 숫자·목록·출처·주의사항 (LLM을 거치지 않는다)"""
    status = result.get("status")
    src = _source_result(result)
    lines = []
    if status == "ok":
        lines = _body(result)
    elif status == "ambiguous_region":
        lines = [f"- {c}" for c in result.get("candidates", [])]
    elif status in ("unknown_indicator", "not_supported_question"):
        lines = ["지원 지표: " + ", ".join(result.get("supported", []))]

    notes = list(src.get("notes") or [])
    if src.get("formula"):
        notes.insert(0, f"계산식: {src['formula']}")
    lines += [f"※ {n}" for n in notes]
    if src.get("source"):
        lines.append(f"출처: {src['source']} ({src['basis']})")
    if src.get("url"):
        lines.append(f"통계표: {src['url']}")
    return "\n".join(lines)


def sources(result):
    """답변에 쓴 통계표 목록 [{"name", "basis", "tbl_id", "url"}]"""
    src = _source_result(result)
    if not src.get("source"):
        return []
    return [
        {
            "name": src["source"],
            "basis": src["basis"],
            "tbl_id": src.get("tbl_id"),
            "url": src.get("url"),
        }
    ]


async def ask(question):
    """질문 하나를 끝까지 처리. {"status", "answer", "parsed", "result", "sources"}를 반환한다.
    status: ok 또는 답변 불가 사유 (indicators.py 결과의 status)"""
    parsed = await understand(question)
    # DuckDB 조회는 동기 함수라 이벤트 루프를 막지 않도록 스레드에서 실행
    result = await run_in_threadpool(compute, parsed)
    return {
        "status": result.get("status"),
        "answer": f"{summary(result)}\n\n{render(result)}".strip(),
        "parsed": parsed,
        "result": result,
        "sources": sources(result),
    }
