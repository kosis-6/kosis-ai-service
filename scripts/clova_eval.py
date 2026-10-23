"""정확도 비교: 데이터를 주고 CLOVA가 계산과 답변을 모두 하게 한다

우리 서비스(pipeline/qa.py)와 같은 방식으로 질문을 해석하고 같은 데이터를 고른 뒤,
계산을 코드 대신 CLOVA에 맡긴다. 아래에 우리 서비스 코드의 계산 결과를 같이 보여 주므로
"계산을 코드가 하느냐 LLM이 하느냐"만 다른 상태에서 두 답을 비교할 수 있다.

  1. 질문 이해   qa.understand (우리 서비스와 같은 CLOVA 호출)
  2. 데이터 고르기 qa.compute와 같은 조건(지역·연도·성별)으로 정제 DB에서 연령별 행 전부
  3. 계산·답변   데이터 + 지표 정의를 주고 CLOVA가 계산해서 답한다
  4. 비교       같은 해석으로 우리 서비스 코드가 계산한 값 (CLOVA 호출 없음)

  --source none  데이터 없이 질문만 보낸다 (CLOVA가 원래 아는 지식만, 호출 1번)

실행 (컨테이너 안에서)
  python scripts/clova_eval.py -q "2025년 서울 중구 노령화지수는?"
  python scripts/clova_eval.py -q "..." --debug        # 질문 분석 결과와 프롬프트도 출력
  python scripts/clova_eval.py -q "..." --source none
  python scripts/clova_eval.py                         # 질문을 하나씩 입력 (빈 줄이면 종료)

기록: data/eval/clova_eval_log.jsonl 에 질문마다 한 줄씩 쌓인다 (CLOVA 계산 과정 포함)
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import clova  # noqa: E402
import indicators  # noqa: E402
import qa  # noqa: E402

LOG_PATH = ROOT / "data" / "eval" / "clova_eval_log.jsonl"
# 데이터가 이보다 길면 지표 계산에 필요한 연령만 남긴다 (분당 토큰 한도 대비)
MAX_CHARS = 20000
# 연령을 줄여도 이보다 길면 CLOVA에 보내지 않는다 (예: 전국 시군구 순위)
HARD_MAX_CHARS = 60000

SYSTEM_DATA = """당신은 복지 정책 담당자에게 인구통계를 설명하는 분석가입니다.
[데이터]만 사용해 [질문]에 답합니다.
- 필요한 값은 [데이터]에서 직접 골라 [지표 정의]대로 계산합니다. 계산 과정을 단계별로 보여줍니다.
- 데이터에 없는 값은 추측하지 말고 "데이터에 없음"이라고 씁니다.
- 마지막에 질문에 대한 답을 정리합니다."""
SYSTEM_NONE = """당신은 복지 정책 담당자에게 인구통계를 설명하는 분석가입니다.
알고 있는 지식으로 [질문]에 답합니다. 계산이 필요하면 과정을 보여주고, 마지막에 답을 정리합니다."""

# 우리 서비스 코드(indicators.py)가 아는 지표 정의. CLOVA에도 같은 정의를 준다
DEFINITIONS = "\n".join(f"- {name}: {ind['formula']}"
                        for name, ind in indicators.INDICATORS.items())
POP_COLUMNS = ("- 연령 '계' = 전체 연령 합계(총인구). 나머지 연령 행과 더하지 않는다\n"
               "- 연령 '100세 이상' = 100세 이상 전체를 묶은 값")
# 프롬프트가 너무 길 때 지표별로 남길 연령 ('계'는 항상 남긴다)
NEEDED_AGES = {
    "총인구": [],
    "고령인구 비율": [(65, 100)],
    "유소년인구 비율": [(0, 14)],
    "노령화지수": [(0, 14), (65, 100)],
}


# ---- 2. 데이터 고르기 (qa.compute / indicators.py와 같은 조건) --------------------------

def _population(codes, y0, y1, sex, ages=None):
    """indicators._components와 같은 조건: 지역·연도·성별로 고르고 연령별 행 전부"""
    where = ["p.year between ? and ?", "p.sex = ?",
             f"p.region_code in ({', '.join('?' for _ in codes)})"]
    params = [y0, y1, sex, *codes]
    if ages is not None:
        ranges = [" or ".join("p.age between ? and ?" for _ in ages)] if ages else []
        where.append(f"(p.age is null{''.join(' or ' + r for r in ranges)})")
        params += [a for rng in ages for a in rng]
    return indicators._con().execute(f"""
        select p.year as 연도, r.path as 지역, p.sex as 성별, p.age_label as 연령,
               p.population as 인구
        from population p join regions r using (region_code)
        where {' and '.join(where)}
        order by p.region_code, p.year, coalesce(p.age, -1)
    """, params).df()


def _projection(name, codes, year):
    """indicators.projection과 같은 조건: 장래추계 표의 같은 지표, 중위 추계"""
    return indicators._con().execute(f"""
        select f.year as 연도, r.path as 지역, f.scenario as 시나리오, f.label as 지표,
               f.value as 값, f.unit as 단위, f.is_projection as 추계값
        from facts f join regions r using (region_code)
        where f.tbl_id = ? and f.region_code in ({', '.join('?' for _ in codes)})
          and f.year = ? and f.c3_id = ? and f.scenario = ? and not f.is_missing
    """, [indicators.PROJ_TABLE, *codes, year, indicators.INDICATORS[name]["projection_c3"],
          indicators.DEFAULT_SCENARIO]).df()


def _rank_codes(year, level, sido):
    """indicators.rank와 같은 대상 지역: 시도 전체, 또는 (그 시도의) 시군구 전체"""
    where, params = ("r.kind = '시도'", []) if level == "시도" else ("r.is_basic", [])
    if sido:
        s = indicators.resolve_region(sido)
        if s["status"] != "ok":
            return None, s.get("reason")
        where += " and r.sido_code = ?"
        params.append(s["region_code"])
    rows = indicators._rows(f"""
        select distinct r.region_code from regions r
        join population p on p.region_code = r.region_code and p.year = ?
        where {where} order by 1
    """, [year, *params])
    return [r["region_code"] for r in rows], None


def select_data(parsed):
    """질문 분석 결과 → {"status": "ok", "rows", "source", "columns", "notes"}
    고를 수 없으면 {"status": "no_data", "reason"}"""
    kind = parsed.get("type")
    if kind in ("parse_error", "other"):
        return {"status": "no_data", "reason": "인구통계 질문으로 해석되지 않았습니다."}
    if kind == "causal":  # qa.compute와 같게: 지역이 있으면 추이, 없으면 순위
        kind = "trend" if parsed.get("regions") else "rank"
    name, _ = indicators._get_indicator(parsed.get("indicator") or "")
    if name is None:
        return {"status": "no_data",
                "reason": f"'{parsed.get('indicator')}'은(는) 지원하지 않는 지표입니다. "
                          f"지원 지표: {', '.join(indicators.INDICATORS)}"}

    lo, hi = indicators._data_years()
    year = parsed.get("year") or hi
    sex = parsed.get("sex") or "계"
    if kind == "trend":
        y0, y1 = parsed.get("start_year") or hi - 5, parsed.get("end_year") or hi
    else:
        y0 = y1 = year

    if kind == "rank":
        if not lo <= year <= hi:
            return {"status": "no_data", "reason": f"순위는 {lo}~{hi}년 자료만 있습니다."}
        codes, err = _rank_codes(year, parsed.get("level") or "시도", parsed.get("sido"))
        if err:
            return {"status": "no_data", "reason": err}
        regs = []
    else:
        if not parsed.get("regions"):
            return {"status": "no_data", "reason": "어느 지역인지 질문에 없습니다."}
        regs = [indicators.resolve_region(r, y1) for r in parsed["regions"]]
        if bad := next((r for r in regs if r["status"] != "ok"), None):
            return {"status": "no_data", "reason": bad.get("reason"),
                    "candidates": bad.get("candidates")}
        codes = [r["region_code"] for r in regs]

    if y0 < lo or y1 > hi:  # 수록 기간 밖: indicators._check처럼 전국·시도만 장래추계로
        if y1 > hi and regs and all(r["kind"] in ("전국", "시도") for r in regs):
            return {"status": "ok", "rows": _projection(name, codes, y1),
                    "source": f"{indicators.PROJ_SOURCE}\n{indicators.PROJ_BASIS}",
                    "columns": "- 추계값 = True면 실제 통계가 아닌 장래인구추계",
                    "notes": [f"주민등록인구 실적은 {lo}~{hi}년만 있어 장래추계 값을 줌"]}
        return {"status": "no_data", "reason": f"주민등록인구는 {lo}~{hi}년 자료만 있습니다."}

    out = {"status": "ok", "rows": _population(codes, y0, y1, sex),
           "source": f"{indicators.POP_SOURCE}\n{indicators.POP_BASIS}",
           "columns": POP_COLUMNS, "notes": []}
    if len(out["rows"].to_csv(index=False)) > MAX_CHARS:
        out["rows"] = _population(codes, y0, y1, sex, NEEDED_AGES[name])
        out["notes"].append(f"데이터가 커서 {name} 계산에 필요한 연령과 '계'만 줌")
        size = len(out["rows"].to_csv(index=False))
        if size > HARD_MAX_CHARS:
            return {"status": "no_data",
                    "reason": f"데이터가 너무 커서({len(out['rows']):,}행, {size:,}자) "
                              "CLOVA에 줄 수 없습니다. 지역이나 범위를 좁혀 주세요."}
    return out


# ---- 3. CLOVA 계산·답변 -------------------------------------------------------------

def _table_text(df):
    """모든 행이 같은 값인 열은 [공통]으로 빼서 토큰을 줄인다"""
    same = [c for c in df.columns if len(df) > 1 and df[c].nunique(dropna=False) == 1]
    common = ", ".join(f"{c}={df[c].iloc[0]}" for c in same)
    body = df.drop(columns=same).to_csv(index=False, lineterminator="\n").strip()
    return (f"[공통] {common}\n" if common else "") + body


def build_prompt(question, data=None):
    parts = [f"[오늘 날짜]\n{date.today().isoformat()}"]
    if data is not None:
        parts += [f"[출처]\n{data['source']}", f"[열 설명]\n{data['columns']}",
                  f"[지표 정의]\n{DEFINITIONS}",
                  f"[데이터] ({len(data['rows'])}행)\n{_table_text(data['rows'])}"]
    parts.append(f"[질문]\n{question}")
    return "\n\n".join(parts)


def _log(record):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run(question, source="db", debug=False, max_tokens=3000):
    print(f"\n질문: {question}")
    record = {"time": datetime.now().isoformat(timespec="seconds"), "question": question,
              "source": source}

    if source == "none":
        prompt = build_prompt(question)
        res = clova.chat(prompt, system=SYSTEM_NONE, max_tokens=max_tokens, temperature=0.1)
        print(f"\n[CLOVA 답변 (데이터 없음)]\n{res['content']}")
        _log({**record, "prompt": prompt, **res})
        return

    parsed = qa.understand(question)
    if debug:
        print(f"\n[1. 질문 분석]\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
    data = select_data(parsed)
    record["parsed"] = parsed

    if data["status"] != "ok":
        print(f"\n[데이터를 고르지 못함] {data['reason']}")
        if data.get("candidates"):
            print("후보: " + ", ".join(data["candidates"]))
        _log({**record, "no_data": data})
        return

    prompt = build_prompt(question, data)
    print(f"\n[2. 데이터] {len(data['rows'])}행, 프롬프트 {len(prompt):,}자")
    for n in data["notes"]:
        print(f"※ {n}")
    if debug:
        print(f"\n[CLOVA에 보낸 프롬프트]\n{prompt}")

    res = clova.chat(prompt, system=SYSTEM_DATA, max_tokens=max_tokens, temperature=0.1)
    usage = res.get("usage") or {}
    print(f"\n[3. CLOVA 계산·답변]\n{res['content']}")
    print(f"(토큰: 질문 {usage.get('promptTokens')} / 답변 {usage.get('completionTokens')}, "
          f"종료 이유 {res.get('finish_reason')})")

    # 같은 해석으로 우리 서비스 코드가 계산한 값 (CLOVA 호출 없음)
    service = qa.compute(parsed)
    print(f"\n[4. 비교: 우리 서비스 코드 계산]\n{qa.render(service) or service.get('reason', '')}")
    _log({**record, "rows": len(data["rows"]), "notes": data["notes"], "prompt": prompt,
          **res, "service_result": service})


def _clean(text):
    """입력 중 깨진 바이트(지우다 남은 한글 조각 등)와 붙여넣기 표시를 버린다

    터미널은 붙여넣은 글자 앞뒤에 ESC[200~ ... ESC[201~ 표시를 붙인다.
    이게 남으면 "[200~2025년"을 "2020~2025년" 기간으로 해석하는 일이 생긴다.
    """
    cleaned = text.encode("utf-8", "surrogateescape").decode("utf-8", "ignore")
    if re.search(r"\x1b?\[200~", cleaned):  # 끝 표시는 '~'만 남기도 한다
        cleaned = re.sub(r"(\x1b?\[201)?~\s*$", "", cleaned)
    cleaned = re.sub(r"\x1b?\[20[01]~", "", cleaned).strip()
    if cleaned != text.strip():
        print(f"(입력에 깨진 글자가 있어 지웠습니다 → {cleaned})")
    return cleaned


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="데이터 + CLOVA 계산 vs 우리 서비스 코드 계산")
    parser.add_argument("--question", "-q", help="질문 (생략하면 하나씩 입력)")
    parser.add_argument("--source", choices=["db", "none"], default="db",
                        help="CLOVA에 줄 데이터: 정제 DB(기본) / 없음")
    parser.add_argument("--debug", action="store_true", help="질문 분석 결과와 프롬프트도 출력")
    parser.add_argument("--max-tokens", type=int, default=3000, help="답변 최대 토큰")
    args = parser.parse_args()

    if args.question:
        run(_clean(args.question), args.source, args.debug, args.max_tokens)
        return
    # readline을 불러오면 input()이 글자 단위로 편집된다.
    # 없으면 백스페이스가 한글(3바이트)을 1바이트씩 지워 깨진 문자가 남는다
    import readline
    readline.parse_and_bind("set enable-bracketed-paste off")
    print("질문을 입력하세요. 빈 줄이면 종료합니다. (429 오류가 나면 1분 뒤 다시)")
    while question := _clean(input("\n> ")):
        try:
            run(question, args.source, args.debug, args.max_tokens)
        except SystemExit as e:  # CLOVA 오류(429 등)가 나도 다음 질문을 받는다
            print(f"\n오류: {e}")


if __name__ == "__main__":
    main()
