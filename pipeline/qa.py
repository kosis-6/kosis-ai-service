"""질문 → 답변 (하위 태스크 1~5 연결)

  1. 질문 이해        LLM이 질문을 JSON(질문 유형, 지표, 지역, 연도)으로 바꾼다
  2~4. 판단·조회·계산  코드(indicators.py)가 계산하고, status로 답변 가능 여부를 알려준다
  5. 답변 생성        LLM은 숫자 없는 설명 문장만 쓰고,
                      숫자·목록·출처·주의사항은 코드가 계산 결과에서 그대로 만든다 (render)

LLM은 숫자를 쓰지 않는다. 답변의 숫자는 모두 코드가 계산 결과에서 옮긴다.
LLM 문장에 계산 결과에 없는 숫자가 섞이면 한 번 다시 쓰게 하고, 그래도 섞이면 문장을 빼고 수치만 보여준다.
지원하지 않는 지표 안내는 LLM 없이 코드가 고정 문장으로 쓴다.

실행: python pipeline/qa.py --question "2025년 서울과 부산 고령인구 비율 비교"
      python pipeline/qa.py --question "..." --debug   (중간 결과 JSON도 출력)
"""
import argparse
import json
import re
import sys
from pathlib import Path

import clova
import indicators

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "answer_policy.md"
EXAMPLE_QUESTION = "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"

# 1단계: 질문 이해. {INDICATORS}, {LATEST}는 실행할 때 채운다
PARSE_SYSTEM = """당신은 인구통계 질문을 분석해 JSON으로 바꾸는 역할입니다.
답하거나 계산하지 말고 JSON 하나만 출력하세요.

질문 유형(type)
- value: 한 지역·한 연도의 값  예) 2025년 서울 고령인구 비율은?
- trend: 한 지역의 기간 변화  예) 최근 5년 부산 인구 변화는?
- compare: 여러 지역 비교  예) 서울과 부산 중 어디가 높아?
- rank: 순위  예) 고령인구 비율이 가장 높은 시도 3곳은?
- causal: 원인·정책 효과를 묻는 질문  예) 저출산 정책이 효과가 있었나?
- other: 통계와 관계없는 질문 (인사, 잡담 등). 지원하지 않는 지표라도 통계를 묻는 질문이면 other가 아니다.

JSON 형식
{"type": "...", "indicator": "...", "regions": ["..."], "year": 2025, "start_year": null, "end_year": null,
 "sex": "계", "level": "시도", "sido": null, "top": null, "ascending": false}

규칙
- indicator: 지원 지표는 {INDICATORS}. 질문의 지표가 이 중 하나와 뜻이 같으면 그 이름을, 아니면 질문의 표현을 그대로 쓴다.
  causal이면 지원 지표 중 질문과 가장 관련 있는 것을 고른다 (예: 저출산 → 유소년인구 비율, 고령화 → 고령인구 비율).
- regions: 질문에 나온 지역 표현 그대로 (예: "서울", "서울 중구"). 없으면 [].
- 연도가 없으면 최신 연도 {LATEST}. "최근 N년"은 start_year={LATEST}-N, end_year={LATEST}.
- sex: "계", "남", "여" 중 하나.
- level: 시도 순위면 "시도", 시군구 순위면 "시군구". sido: 특정 시도 안의 시군구 순위일 때 그 시도 이름.
- top: "3곳"처럼 개수를 말하면 그 숫자. ascending: "가장 낮은" 순위면 true.
- 해당 없는 항목은 null."""

# 5단계: 답변 생성. {POLICY}는 answer_policy.md 내용
ANSWER_SYSTEM = """당신은 복지 정책 담당자에게 인구통계를 설명하는 분석가입니다.
[분석 결과]를 바탕으로 답변의 설명 문장만 씁니다.
수치 목록·출처·주의사항은 프로그램이 답변 아래에 따로 붙이므로 쓰지 않습니다.

규칙
- [답변 지침]을 반드시 따릅니다.
- 값·차이·비율 같은 수치를 쓰지 않습니다. 연도와 지역 이름은 써도 됩니다.
- [분석 결과]에 없는 내용(해석, 원인, 출처, 참고 자료)을 지어내지 않습니다.
- 한두 문장으로 짧게 씁니다. 인사말이나 "추가로 궁금하신 점" 같은 맺음말은 쓰지 않습니다.
- 원인이나 정책 효과는 판단하지 않습니다.

{POLICY}"""


def understand(question):
    """1단계: 질문 → dict. JSON을 못 읽으면 type이 parse_error"""
    lo, hi = indicators._data_years()
    system = (PARSE_SYSTEM.replace("{INDICATORS}", ", ".join(indicators.INDICATORS))
              .replace("{LATEST}", str(hi)))
    text = clova.chat(question, system=system, temperature=0.1)["content"]
    m = re.search(r"\{.*\}", text, re.S)  # 코드 블록(```json)으로 감싸 오는 경우 대비
    try:
        return json.loads(m.group(0))
    except (AttributeError, json.JSONDecodeError):
        return {"type": "parse_error", "raw": text}


def compute(q):
    """2~4단계: 질문 분석 결과 → indicators.py 계산 결과"""
    kind = q.get("type")
    ind = q.get("indicator") or ""
    regions = q.get("regions") or []
    sex = q.get("sex") or "계"
    lo, hi = indicators._data_years()
    year = q.get("year") or hi

    if kind == "parse_error":
        return {"status": "parse_error", "reason": "질문을 이해하지 못했습니다. 지역·연도·지표를 넣어 다시 질문해 주세요."}
    if kind == "other":
        return {"status": "not_supported_question", "reason": "지역별 인구통계 질문에만 답할 수 있습니다.",
                "supported": list(indicators.INDICATORS)}

    causal = kind == "causal"
    if causal:  # 답변 불가 사례 4: 인과 판단 없이 수치 변화만 제시
        kind = "trend" if regions else "rank"

    if indicators._get_indicator(ind)[0] is None:  # 지역보다 지표를 먼저 확인
        return {"status": "unknown_indicator", "indicator": ind, "supported": list(indicators.INDICATORS)}

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
            result = indicators.rank(ind, year, q.get("level") or "시도", q.get("sido"),
                                     q.get("top"), bool(q.get("ascending")), sex)
        else:
            return {"status": "parse_error", "reason": f"알 수 없는 질문 유형: {kind}"}
    except Exception as e:  # 계산 중 오류도 답변 단계에서 안내
        return {"status": "error", "reason": f"계산 중 오류가 났습니다: {e}"}

    if result.get("series"):
        _add_yearly_change(result)
    if causal:
        result["causal_question"] = "원인·정책 효과는 판단하지 않고 수치 변화만 제시합니다."
    return result


def _add_yearly_change(result):
    """추이 결과에 전년 대비 변화를 넣는다"""
    kind = indicators.INDICATORS[result["indicator"]]["kind"]
    for prev, cur in zip(result["series"], result["series"][1:]):
        if prev["value"] is None or cur["value"] is None:
            continue
        diff = cur["value"] - prev["value"]
        cur["diff_from_prev"] = int(diff) if kind == "count" else round(diff, 2)
        if kind == "count":
            cur["rate_from_prev"] = round(diff / prev["value"] * 100, 2)


# ---- 숫자 부분: 코드가 계산 결과에서 그대로 만든다 -----------------------------------

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
    """status가 ok인 계산 결과 → 숫자 줄 목록"""
    unit = r.get("unit", "")
    name = r.get("indicator", "")
    if "scenario" in r:  # 장래추계
        return [f"- {r['region']} {r['year']}년 {name} (장래인구추계 {r['scenario']}, 실제 통계 아님): "
                f"{_num(r['value'], unit)}"]
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
            lines.append(f"- 가장 높은 {r['highest']}와 가장 낮은 {r['lowest']}의 차이: {_num(r['gap'], r['gap_unit'])}")
        return lines
    if "ranking" in r:  # 순위
        lines = [f"{r['year']}년 {name} {r['level']} 순위 ({r['order']}, 전체 {r['total_regions']}곳 중)"]
        lines += [f"{x['rank']}. {x['region']}: {_num(x['value'], unit)}" for x in r["ranking"]]
        return lines
    return [f"- {r['region']} {r['year']}년 {name} ({r['sex']}): {_num(r['value'], unit)}"]


def render(result):
    """계산 결과 → 답변 아래에 붙일 숫자·목록·출처·주의사항 (LLM을 거치지 않는다)"""
    status = result.get("status")
    src = result
    lines = []
    if status == "ok":
        lines = _body(result)
    elif status == "out_of_range" and result.get("suggestion", {}).get("status") == "ok":
        src = result["suggestion"]
        lines = _body(src)
    elif status == "ambiguous_region":
        lines = [f"- {c}" for c in result.get("candidates", [])]
    elif status in ("unknown_indicator", "not_supported_question"):
        lines = ["지원 지표: " + ", ".join(result.get("supported", []))]

    notes = list(src.get("notes") or [])
    if src.get("formula"):
        notes.insert(0, f"계산식: {src['formula']}")
    if result.get("causal_question"):
        notes.append(result["causal_question"])
    lines += [f"※ {n}" for n in notes]
    if src.get("source"):
        lines.append(f"출처: {src['source']} ({src['basis']})")
    return "\n".join(lines)


def _guide(result):
    """status별 답변 지침. 프롬프트 규칙만으로는 LLM이 자주 빠뜨려서 계산 결과에 맞춰 코드가 정한다"""
    status = result.get("status")
    if status == "ok":
        if result.get("causal_question"):
            return ("원인이나 정책 효과는 통계 수치만으로 판단할 수 없다고 먼저 밝히고, "
                    "아래 수치 변화가 늘었는지 줄었는지만 숫자 없이 말하세요.")
        return "질문에 대한 결론(어느 쪽이 높은지, 늘었는지 줄었는지 등)을 숫자 없이 한두 문장으로 말하세요."
    if status == "out_of_range":
        g = f"먼저 수록 기간 밖이라고 알리세요: {result.get('reason')}"
        if result.get("suggestion", {}).get("status") == "ok":
            g += (" 그리고 장래인구추계 값을 아래에 함께 보여준다고 알리되, 실제 통계가 아닌 추계값이라고 밝히세요."
                  " 추계값은 아래에 따로 표시되므로 예시나 가정(A%, B% 등)을 들지 마세요.")
        return g
    if status == "unknown_indicator":
        return f"'{result.get('indicator')}'은(는) 지원하지 않는 지표라고 알리세요. 값을 추정하지 마세요."
    if status == "ambiguous_region":
        return f"{result.get('reason')} 아래 후보 중 어느 지역인지 물어보세요."
    return f"답변할 수 없는 이유를 안내하세요: {result.get('reason')}"


def _direction(diff):
    return "증가" if diff > 0 else "감소" if diff < 0 else "변화 없음"


def facts_for_llm(result):
    """LLM에 넘길 정보. 값·차이·비율은 빼고 지역·기간·방향(증가/감소)·순서만 남긴다"""
    f = {k: result[k] for k in ("status", "indicator", "formula", "region", "year", "sex", "reason",
                                "candidates", "highest", "lowest", "causal_question") if k in result}
    if "series" in result:
        c = result["change"]
        f["period"] = f"{c['from_year']}~{c['to_year']}년"
        f["overall"] = _direction(c["diff"])
        yearly = {_direction(s["diff_from_prev"]) for s in result["series"] if "diff_from_prev" in s}
        f["yearly"] = f"매년 {yearly.pop()}" if len(yearly) == 1 else "해마다 다름"
    if "values" in result:
        f["order_high_to_low"] = [v["region"] for v in result["values"]]
    if "ranking" in result:
        f["level"], f["order"] = result["level"], result["order"]
        f["ranking"] = [x["region"] for x in result["ranking"]]
    if result.get("suggestion", {}).get("status") == "ok":
        f["projection_available"] = True
    if "supported" in result:
        f["supported_listed_below"] = True
    return f


def _fixed_text(result):
    """LLM 없이 코드가 쓰는 설명 문장.
    지원하지 않는 지표는 LLM이 다른 기능을 지어내 제안해서 고정 문장으로 안내한다"""
    if result.get("status") == "unknown_indicator":
        return (f"'{result.get('indicator')}'은(는) 아직 지원하지 않는 지표입니다. "
                "아래 지원 지표로 다시 질문해 주세요.")
    return None


def explain(question, result, stream=False, extra=""):
    """5단계: LLM이 숫자 없는 설명 문장을 쓴다. 숫자는 보여주지 않는다"""
    system = ANSWER_SYSTEM.replace("{POLICY}", POLICY_PATH.read_text(encoding="utf-8"))
    prompt = (f"[질문]\n{question}\n\n"
              f"[분석 결과]\n{json.dumps(facts_for_llm(result), ensure_ascii=False, indent=2, default=str)}\n\n"
              f"[답변 지침]\n{_guide(result)}{extra}")
    return clova.chat(prompt, system=system, stream=stream, temperature=0.1)["content"]


RETRY_EXTRA = ("\n이전 답변에 [분석 결과]에 없는 숫자가 들어 있었습니다. "
               "숫자(값, 연도 추정, 어림수)를 하나도 쓰지 마세요.")


def write_text(question, result):
    """설명 문장을 만들고 숫자 확인까지 마친다. (문장, 버린 문장의 근거 없는 숫자 목록)을 돌려준다

    숫자 확인에 걸리면 한 번 다시 쓰게 하고, 그래도 걸리면 문장을 버리고 코드 출력만 남긴다.
    확인을 마친 뒤에 보여줘야 하므로 스트리밍하지 않는다.
    """
    fixed = _fixed_text(result)
    if fixed is not None:
        return fixed, []
    text = explain(question, result)
    if not check_numbers(text, question, result):
        return text, []
    text = explain(question, result, extra=RETRY_EXTRA)
    bad = check_numbers(text, question, result)
    return ("", bad) if bad else (text, [])


NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def check_numbers(text, question, result):
    """LLM 문장 속 숫자가 질문이나 LLM에 넘긴 정보에 있는지 확인. 어디에도 없는 숫자 목록을 돌려준다"""
    result = facts_for_llm(result)
    allowed = set()

    def collect(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            allowed.add(abs(float(v)))
        elif isinstance(v, str):
            allowed.update(float(n.replace(",", "")) for n in NUMBER.findall(v))
        elif isinstance(v, dict):
            for x in v.values():
                collect(x)
        elif isinstance(v, list):
            for x in v:
                collect(x)

    collect(result)
    collect(question)
    return [n for n in NUMBER.findall(text) if float(n.replace(",", "")) not in allowed]


def ask(question):
    """질문 하나를 끝까지 처리. {"parsed", "result", "answer", "dropped_numbers"}를 반환한다.
    dropped_numbers: 다시 써도 근거 없는 숫자가 남아 설명 문장을 뺐을 때 그 숫자들"""
    parsed = understand(question)
    result = compute(parsed)
    text, dropped = write_text(question, result)
    block = render(result)
    return {"parsed": parsed, "result": result,
            "answer": f"{text}\n\n{block}".strip(),
            "dropped_numbers": dropped}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="지역별 인구통계 질의응답")
    parser.add_argument("--question", "-q", default=EXAMPLE_QUESTION,
                        help=f"질문 (생략하면 예시 질문: {EXAMPLE_QUESTION})")
    parser.add_argument("--debug", action="store_true", help="질문 분석·계산 결과 JSON도 출력")
    args = parser.parse_args()

    print(f"질문: {args.question}\n")
    parsed = understand(args.question)
    result = compute(parsed)
    if args.debug:
        print("[1. 질문 분석]")
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        print("\n[2~4. 계산 결과]")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("\n[5. 답변]")
    text, dropped = write_text(args.question, result)
    if text:
        print(text)
    block = render(result)
    if block:
        print(f"\n{block}")
    if dropped:
        print(f"\n[숫자 확인] 설명 문장에 근거 없는 숫자({', '.join(dropped)})가 있어 "
              "문장을 빼고 수치만 표시")
    else:
        print("\n[숫자 확인] 이상 없음")


if __name__ == "__main__":
    main()
