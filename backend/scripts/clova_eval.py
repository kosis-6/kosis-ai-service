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
MAX_CHARS = 20000
HARD_MAX_CHARS = 60000

SYSTEM_DATA = """당신은 복지 정책 담당자에게 인구통계를 설명하는 분석가입니다.
[데이터]만 사용해 [질문]에 답합니다.
- 필요한 값은 [데이터]에서 직접 골라 [지표 정의]대로 계산합니다. 계산 과정을 단계별로 보여줍니다.
- 데이터에 없는 값은 추측하지 말고 "데이터에 없음"이라고 씁니다.
- 마지막에 질문에 대한 답을 정리합니다."""
SYSTEM_NONE = """당신은 복지 정책 담당자에게 인구통계를 설명하는 분석가입니다.
알고 있는 지식으로 [질문]에 답합니다. 계산이 필요하면 과정을 보여주고, 마지막에 답을 정리합니다."""

DEFINITIONS = "\n".join(
    f"- {name}: {ind['formula']}" for name, ind in indicators.INDICATORS.items()
)
POP_COLUMNS = (
    "- 연령 '계' = 전체 연령 합계(총인구). 나머지 연령 행과 더하지 않는다\n"
    "- 연령 '100세 이상' = 100세 이상 전체를 묶은 값"
)
NEEDED_AGES = {
    "총인구": [],
    "고령인구 비율": [(65, 100)],
    "유소년인구 비율": [(0, 14)],
    "노령화지수": [(0, 14), (65, 100)],
}


def _population(codes, y0, y1, sex, ages=None):
    where = [
        "p.year between ? and ?",
        "p.sex = ?",
        f"p.region_code in ({', '.join('?' for _ in codes)})",
    ]
    params = [y0, y1, sex, *codes]
    if ages is not None:
        ranges = [" or ".join("p.age between ? and ?" for _ in ages)] if ages else []
        where.append(f"(p.age is null{''.join(' or ' + r for r in ranges)})")
        params += [a for rng in ages for a in rng]
    return (
        indicators._con()
        .execute(
            f"""
        select p.year as 연도, r.path as 지역, p.sex as 성별, p.age_label as 연령,
               p.population as 인구
        from population p join regions r using (region_code)
        where {" and ".join(where)}
        order by p.region_code, p.year, coalesce(p.age, -1)
    """,
            params,
        )
        .df()
    )


def _projection(name, codes, year):
    return (
        indicators._con()
        .execute(
            f"""
        select f.year as 연도, r.path as 지역, f.scenario as 시나리오, f.label as 지표,
               f.value as 값, f.unit as 단위, f.is_projection as 추계값
        from facts f join regions r using (region_code)
        where f.tbl_id = ? and f.region_code in ({", ".join("?" for _ in codes)})
          and f.year = ? and f.c3_id = ? and f.scenario = ? and not f.is_missing
    """,
            [
                indicators.PROJ_TABLE,
                *codes,
                year,
                indicators.INDICATORS[name]["projection_c3"],
                indicators.DEFAULT_SCENARIO,
            ],
        )
        .df()
    )


def _rank_codes(year, level, sido):
    where, params = ("r.kind = '시도'", []) if level == "시도" else ("r.is_basic", [])
    if sido:
        s = indicators.resolve_region(sido)
        if s["status"] != "ok":
            return None, s.get("reason")
        where += " and r.sido_code = ?"
        params.append(s["region_code"])
    rows = indicators._rows(
        f"""
        select distinct r.region_code from regions r
        join population p on p.region_code = r.region_code and p.year = ?
        where {where} order by 1
    """,
        [year, *params],
    )
    return [r["region_code"] for r in rows], None


def select_data(parsed):
    kind = parsed.get("type")
    if kind in ("parse_error", "other"):
        return {"status": "no_data", "reason": "인구통계 질문으로 해석되지 않았습니다."}
    if kind == "causal":
        kind = "trend" if parsed.get("regions") else "rank"
    name, _ = indicators._get_indicator(parsed.get("indicator") or "")
    if name is None:
        return {
            "status": "no_data",
            "reason": f"'{parsed.get('indicator')}'은(는) 지원하지 않는 지표입니다. "
            f"지원 지표: {', '.join(indicators.INDICATORS)}",
        }

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
            return {
                "status": "no_data",
                "reason": bad.get("reason"),
                "candidates": bad.get("candidates"),
            }
        codes = [r["region_code"] for r in regs]

    if y0 < lo or y1 > hi:
        if y1 > hi and regs and all(r["kind"] in ("전국", "시도") for r in regs):
            return {
                "status": "ok",
                "rows": _projection(name, codes, y1),
                "source": f"{indicators.PROJ_SOURCE}\n{indicators.PROJ_BASIS}",
                "columns": "- 추계값 = True면 실제 통계가 아닌 장래인구추계",
                "notes": [f"주민등록인구 실적은 {lo}~{hi}년만 있어 장래추계 값을 줌"],
            }
        return {"status": "no_data", "reason": f"주민등록인구는 {lo}~{hi}년 자료만 있습니다."}

    out = {
        "status": "ok",
        "rows": _population(codes, y0, y1, sex),
        "source": f"{indicators.POP_SOURCE}\n{indicators.POP_BASIS}",
        "columns": POP_COLUMNS,
        "notes": [],
    }
    if len(out["rows"].to_csv(index=False)) > MAX_CHARS:
        out["rows"] = _population(codes, y0, y1, sex, NEEDED_AGES[name])
        out["notes"].append(f"데이터가 커서 {name} 계산에 필요한 연령과 '계'만 줌")
        size = len(out["rows"].to_csv(index=False))
        if size > HARD_MAX_CHARS:
            return {
                "status": "no_data",
                "reason": f"데이터가 너무 커서({len(out['rows']):,}행, {size:,}자) "
                "CLOVA에 줄 수 없습니다. 지역이나 범위를 좁혀 주세요.",
            }
    return out


def _table_text(df):
    same = [c for c in df.columns if len(df) > 1 and df[c].nunique(dropna=False) == 1]
    common = ", ".join(f"{c}={df[c].iloc[0]}" for c in same)
    body = df.drop(columns=same).to_csv(index=False, lineterminator="\n").strip()
    return (f"[공통] {common}\n" if common else "") + body


def build_prompt(question, data=None):
    parts = [f"[오늘 날짜]\n{date.today().isoformat()}"]
    if data is not None:
        parts += [
            f"[출처]\n{data['source']}",
            f"[열 설명]\n{data['columns']}",
            f"[지표 정의]\n{DEFINITIONS}",
            f"[데이터] ({len(data['rows'])}행)\n{_table_text(data['rows'])}",
        ]
    parts.append(f"[질문]\n{question}")
    return "\n\n".join(parts)


def _log(record):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run(question, source="db", debug=False, max_tokens=3000):
    print(f"\n질문: {question}")
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "question": question,
        "source": source,
    }

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
    print(
        f"(토큰: 질문 {usage.get('promptTokens')} / 답변 {usage.get('completionTokens')}, "
        f"종료 이유 {res.get('finish_reason')})"
    )

    service = qa.compute(parsed)
    print(f"\n[4. 비교: 우리 서비스 코드 계산]\n{qa.render(service) or service.get('reason', '')}")
    _log(
        {
            **record,
            "rows": len(data["rows"]),
            "notes": data["notes"],
            "prompt": prompt,
            **res,
            "service_result": service,
        }
    )


def _clean(text):
    cleaned = text.encode("utf-8", "surrogateescape").decode("utf-8", "ignore")
    if re.search(r"\x1b?\[200~", cleaned):
        cleaned = re.sub(r"(\x1b?\[201)?~\s*$", "", cleaned)
    cleaned = re.sub(r"\x1b?\[20[01]~", "", cleaned).strip()
    if cleaned != text.strip():
        print(f"(입력에 깨진 글자가 있어 지웠습니다 → {cleaned})")
    return cleaned


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="데이터 + CLOVA 계산 vs 우리 서비스 코드 계산")
    parser.add_argument("--question", "-q", help="질문 (생략하면 하나씩 입력)")
    parser.add_argument(
        "--source",
        choices=["db", "none"],
        default="db",
        help="CLOVA에 줄 데이터: 정제 DB(기본) / 없음",
    )
    parser.add_argument("--debug", action="store_true", help="질문 분석 결과와 프롬프트도 출력")
    parser.add_argument("--max-tokens", type=int, default=3000, help="답변 최대 토큰")
    args = parser.parse_args()

    if args.question:
        run(_clean(args.question), args.source, args.debug, args.max_tokens)
        return
    import readline

    readline.parse_and_bind("set enable-bracketed-paste off")
    print("질문을 입력하세요. 빈 줄이면 종료합니다. (429 오류가 나면 1분 뒤 다시)")
    while question := _clean(input("\n> ")):
        try:
            run(question, args.source, args.debug, args.max_tokens)
        except SystemExit as e:
            print(f"\n오류: {e}")


if __name__ == "__main__":
    main()
