"""전처리 데이터 사용 여부에 따른 HCX 답변 비교

같은 질문을 세 가지 조건으로 HCX-007에 보내고, 원본 CSV로 직접 계산한 정답과 비교한다.

  service  우리 서비스: HCX 질문 해석 + 전처리 DB(kosis.duckdb) + 코드 계산·템플릿 답변 (qa.ask)
  raw      전처리 없이 원본 CSV 행을 프롬프트에 그대로 넣고 HCX가 직접 계산
  none     데이터 없이 질문만 HCX에 보냄

정답: 원본 CSV(data/raw/kosis_zip/)를 pandas로 직접 계산 (전처리·DuckDB를 거치지 않음)
결과: data/eval/compare_YYYYMMDD_HHMMSS.{json,md}

실행: python -m scripts.compare_llm               (전체, HCX 약 45회 호출, 수 분 소요)
      python -m scripts.compare_llm --only q1 q6  (일부 질문만)
"""

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.services import qa
from app.services.hcx_client import HcxClient, HcxError

RAW_CSV = Path("data/raw/kosis_zip/processed/DT_1B04006.csv")
OUT_DIR = Path("data/eval")
MODEL = "HCX-007"
CALL_INTERVAL = 5  # 호출 제한(42901) 대응 (초)
RETRY_WAITS = (20, 60, 120)
COLUMNS = ["period", "item_nm", "c1_nm", "c1_path", "c2_nm", "value", "unit"]

# raw·none 조건에 공통으로 붙이는 답변 형식 (채점용)
ANSWER_FORMAT = """

답변 마지막 줄은 반드시 아래 형식으로 쓰세요.
- 수치를 묻는 질문: "최종답: 숫자" (단위·쉼표 없이, 비율은 소수 둘째 자리까지)
- 지역을 묻는 질문: "최종답: 지역1, 지역2"
- 데이터로 답할 수 없는 질문: "최종답: 답변 불가\""""

RAW_SYSTEM = (
    "당신은 통계 분석가입니다. [데이터]로 주어진 KOSIS 원본 통계표 행만 사용해 질문에 답하세요. "
    "필요한 계산은 직접 하고, 데이터에 없는 내용은 지어내지 마세요."
)
NONE_SYSTEM = "당신은 통계 분석가입니다. 질문에 답하세요."

# ---- 정답 계산 (원본 CSV 직접 계산) ------------------------------------------------


def load_raw() -> pd.DataFrame:
    df = pd.read_csv(RAW_CSV, dtype=str, encoding="utf-8-sig", usecols=[*COLUMNS, "c1_level"])
    return df[df.item_nm == "총인구수"]


def _age(label: str) -> int | None:
    if label == "계":
        return None
    return int(re.match(r"\d+", label).group())  # "100세 이상" → 100


def components(df: pd.DataFrame, path: str, year: int) -> dict[str, int]:
    rows = df[(df.c1_path == path) & (df.period == str(year))]
    if rows.empty:
        raise ValueError(f"원본에 {path} {year}년 자료가 없습니다.")
    ages = rows.c2_nm.map(_age)
    values = rows.value.astype(int)
    return {
        "total": int(values[ages.isna()].sum()),
        "young": int(values[ages.between(0, 14)].sum()),
        "old": int(values[ages >= 65].sum()),
    }


def old_ratio(c: dict[str, int]) -> float:
    return c["old"] / c["total"] * 100


def aging_index(c: dict[str, int]) -> float:
    return c["old"] / c["young"] * 100


def sido_paths(df: pd.DataFrame) -> list[str]:
    return sorted(set(df[(df.c1_level == "1") & (df.c1_path != "전국")].c1_path))


# ---- 질문 ---------------------------------------------------------------------------
# kind: number(수치) / regions(지역 목록) / refuse(답변 불가가 정답) / manual(사람이 확인)
# raw: 전처리 없는 검색 단계 흉내. 질문의 지역·연도에 맞는 원본 행을 그대로 가져온다


def cases(df: pd.DataFrame) -> list[dict[str, Any]]:
    def rows(paths: list[str], years: list[int]) -> pd.DataFrame:
        years = [str(y) for y in years]
        return df[df.c1_path.isin(paths) & df.period.isin(years)]

    sidos = sido_paths(df)
    seoul, busan, daegu = "서울특별시", "부산광역시", "대구광역시"
    ratios_2025 = {p: old_ratio(components(df, p, 2025)) for p in sidos}
    index_2025 = {p: aging_index(components(df, p, 2025)) for p in sidos}

    return [
        {
            "id": "q1",
            "type": "단순 조회",
            "question": "2025년 서울의 고령인구 비율은?",
            "kind": "number",
            "truth": old_ratio(components(df, seoul, 2025)),
            "unit": "%",
            "raw": rows([seoul], [2025]),
            "service": lambda r: r.get("value"),
        },
        {
            "id": "q2",
            "type": "단순 조회 (행정구역 변경: 2023 군위군 편입)",
            "question": "2025년 대구의 총인구는?",
            "kind": "number",
            "truth": components(df, daegu, 2025)["total"],
            "unit": "명",
            "raw": rows([daegu], [2025]),
            "service": lambda r: r.get("value"),
        },
        {
            "id": "q3",
            "type": "추이 (%p)",
            "question": "2020년부터 2025년까지 부산의 고령인구 비율은 몇 %p 변했나?",
            "kind": "number",
            "truth": old_ratio(components(df, busan, 2025))
            - old_ratio(components(df, busan, 2020)),
            "unit": "%p",
            "raw": rows([busan], range(2020, 2026)),
            "service": lambda r: (r.get("change") or {}).get("diff"),
        },
        {
            "id": "q4",
            "type": "추이 (증감률)",
            "question": "2015년 대비 2025년 대구 총인구 증감률은?",
            "kind": "number",
            "truth": (
                components(df, daegu, 2025)["total"] / components(df, daegu, 2015)["total"] - 1
            )
            * 100,
            "unit": "%",
            "raw": rows([daegu], range(2015, 2026)),
            "service": lambda r: (r.get("change") or {}).get("rate"),
        },
        {
            "id": "q5",
            "type": "비교",
            "question": "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?",
            "kind": "regions",
            "truth": [max([seoul, busan], key=ratios_2025.get)],
            "raw": rows([seoul, busan], [2025]),
            "service": lambda r: [r.get("highest")],
        },
        {
            "id": "q6",
            "type": "순위",
            "question": "2025년 고령인구 비율이 가장 높은 시도 3곳은?",
            "kind": "regions",
            "truth": sorted(sidos, key=ratios_2025.get, reverse=True)[:3],
            "raw": rows(sidos, [2025]),
            "service": lambda r: [x["region"] for x in r.get("ranking") or []][:3],
        },
        {
            "id": "q7",
            "type": "최솟값",
            "question": "2025년 노령화지수가 가장 낮은 시도는?",
            "kind": "regions",
            "truth": [min(sidos, key=index_2025.get)],
            "raw": rows(sidos, [2025]),
            "service": lambda r: [x["region"] for x in r.get("ranking") or []][:1],
        },
        {
            "id": "q8",
            "type": "시군구 (같은 이름 '중구' 여럿)",
            "question": "2025년 서울 중구의 노령화지수는?",
            "kind": "number",
            "truth": aging_index(components(df, "서울특별시 > 중구", 2025)),
            "unit": "",
            "raw": rows(["서울특별시 > 중구"], [2025]),
            "service": lambda r: r.get("value"),
        },
        {
            "id": "q9",
            "type": "답변 불가: 수록 기간 밖",
            "question": "2030년 서울의 고령인구 비율은?",
            "kind": "refuse",
            # 2030년 행은 없으므로 가장 가까운 연도를 가져온 상황
            "raw": rows([seoul], [2024, 2025]),
            "service": lambda r: r.get("status") != "ok",
        },
        {
            "id": "q10",
            "type": "답변 불가: 지원하지 않는 지표",
            "question": "2025년 서울의 20대 혼인율은?",
            "kind": "refuse",
            "raw": rows([seoul], [2025]),
            "service": lambda r: r.get("status") != "ok",
        },
        {
            "id": "q11",
            "type": "답변 제한: 원인·정책 효과",
            "question": "최근 10년간 저출산 정책이 효과가 있었나요?",
            "kind": "manual",
            "raw": rows(["전국"], range(2015, 2026)),
            "service": lambda r: bool(r.get("causal_question")),
        },
    ]


# ---- HCX 호출 -------------------------------------------------------------------------

usage_log: list[dict[str, Any]] = []
_original_chat = HcxClient.chat


async def _logged_chat(self: HcxClient, model: str, messages: list, **options: Any):
    """모든 HCX 호출의 토큰 사용량을 기록하고, 호출 제한에 걸리면 기다렸다가 다시 시도"""
    for wait in (*RETRY_WAITS, None):
        await asyncio.sleep(CALL_INTERVAL)
        try:
            result = await _original_chat(self, model, messages, **options)
            usage_log.append(result.usage)
            return result
        except HcxError as e:
            if e.code != "42901" or wait is None:
                raise
            print(f"    호출 제한, {wait}초 후 다시 시도")
            await asyncio.sleep(wait)


HcxClient.chat = _logged_chat


async def ask_llm(system: str, prompt: str) -> str:
    result = await HcxClient().chat(
        MODEL,
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        thinking={"effort": "low"},
        maxCompletionTokens=5120,
        temperature=0.1,
    )
    return result.content


async def run_condition(name: str, case: dict[str, Any]) -> dict[str, Any]:
    start, n_calls = time.monotonic(), len(usage_log)
    out: dict[str, Any] = {}
    try:
        if name == "service":
            res = await qa.ask(case["question"])
            out = {"answer": res["answer"], "picked": case["service"](res["result"])}
        elif name == "raw":
            data = case["raw"][COLUMNS].to_csv(index=False)
            out = {
                "answer": await ask_llm(
                    RAW_SYSTEM, f"[데이터]\n{data}\n[질문]\n{case['question']}{ANSWER_FORMAT}"
                )
            }
            out["raw_rows"] = len(case["raw"])
        else:
            out = {"answer": await ask_llm(NONE_SYSTEM, case["question"] + ANSWER_FORMAT)}
    except Exception as e:  # 한 조건이 실패해도 나머지는 계속
        out = {"answer": "", "error": f"{type(e).__name__}: {e}"}
    calls = usage_log[n_calls:]
    out["seconds"] = round(time.monotonic() - start, 1)
    out["calls"] = len(calls)
    out["prompt_tokens"] = sum(u.get("promptTokens", 0) for u in calls)
    out["total_tokens"] = sum(u.get("totalTokens", 0) for u in calls)
    return out


# ---- 채점 ---------------------------------------------------------------------------


def _final(answer: str) -> str | None:
    found = re.findall(r"최종답\s*[:：]\s*(.+)", answer)
    return found[-1].strip().strip("*` ") if found else None


def _norm_region(text: str) -> str:
    # "부산광역시", "부산" → "부산" / "서울특별시 > 중구" → "서울중구"
    text = re.sub(r"\s|>", "", text)
    return re.sub(r"(특별자치시|특별자치도|특별시|광역시|도)$", "", text)


def grade(case: dict[str, Any], name: str, out: dict[str, Any]) -> tuple[str, Any]:
    """(판정, 비교에 쓴 값). 판정: 정확 / 근사 / 오답 / 거절 / 답함 / 확인필요 / 오류"""
    if out.get("error"):
        return "오류", None
    kind = case["kind"]
    if name == "service":
        picked = out["picked"]
    else:
        final = _final(out["answer"])
        if final is None:
            return "형식없음", None
        refused = "답변 불가" in final or "답변불가" in final
        if kind in ("refuse", "manual"):
            return ("거절" if refused else "답함"), final
        if refused:
            return "거절", final
        if kind == "number":
            nums = re.findall(r"-?\d[\d,]*\.?\d*", final)
            picked = float(nums[0].replace(",", "")) if nums else None
        else:
            picked = [x.strip() for x in re.split(r"[,、/]| 및 ", final) if x.strip()]

    if kind == "manual":
        return ("인과 판단 회피" if picked else "확인필요"), picked
    if kind == "refuse":
        return ("거절" if picked else "답함"), picked
    if kind == "number":
        if picked is None:
            return "오답", None
        diff = abs(float(picked) - case["truth"])
        if diff <= 0.0051 or (case["unit"] == "명" and diff == 0):
            return "정확", picked
        if diff <= max(abs(case["truth"]) * 0.01, 0.05):
            return "근사", picked
        return "오답", picked
    truth = [_norm_region(x) for x in case["truth"]]
    got = [_norm_region(x) for x in picked][: len(truth)]
    return ("정확" if got == truth else "오답"), picked


# ---- 실행 ---------------------------------------------------------------------------

CONDITIONS = ["service", "raw", "none"]


async def main(only: list[str] | None) -> None:
    print("원본 CSV 읽는 중...")
    df = load_raw()
    selected = [c for c in cases(df) if not only or c["id"] in only]
    records = []
    for case in selected:
        print(f"\n[{case['id']}] {case['question']}")
        rec = {k: case.get(k) for k in ("id", "type", "question", "kind", "truth", "unit")}
        for name in CONDITIONS:
            out = await run_condition(name, case)
            out["grade"], out["value"] = grade(case, name, out)
            rec[name] = out
            print(f"  {name:<8} {out['grade']:<8} 값={out['value']}  토큰={out['total_tokens']:,}")
        records.append(rec)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUT_DIR / f"compare_{stamp}.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), "utf-8")
    md_path = OUT_DIR / f"compare_{stamp}.md"
    md_path.write_text(to_markdown(records), "utf-8")
    print(f"\n저장: {json_path}, {md_path}")


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return "" if v is None else str(v)


def to_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "| ID | 유형 | 정답 | 서비스 | 원본+HCX | HCX 단독 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        cells = [f"{r[n]['grade']} ({_fmt(r[n]['value'])})" for n in CONDITIONS]
        truth = _fmt(r["truth"]) if r["kind"] in ("number", "regions") else "답변 불가/제한"
        lines.append(f"| {r['id']} | {r['type']} | {truth} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "| 조건 | HCX 호출 | 입력 토큰 합계 | 전체 토큰 합계 | 시간(초) 합계 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for n in CONDITIONS:
        rs = [r[n] for r in records]
        lines.append(
            f"| {n} | {sum(x['calls'] for x in rs)} | {sum(x['prompt_tokens'] for x in rs):,} | "
            f"{sum(x['total_tokens'] for x in rs):,} | {sum(x['seconds'] for x in rs):.0f} |"
        )
    lines += ["", "## 답변 원문", ""]
    for r in records:
        lines.append(f"### {r['id']} {r['question']}")
        for n in CONDITIONS:
            lines += [
                f"**{n}** ({r[n]['grade']})",
                "",
                "```",
                r[n].get("answer") or r[n].get("error", ""),
                "```",
                "",
            ]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="전처리 데이터 사용 여부에 따른 HCX 답변 비교")
    parser.add_argument("--only", nargs="*", help="실행할 질문 ID (예: q1 q6)")
    asyncio.run(main(parser.parse_args().only))
