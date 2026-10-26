"""골든셋 자동 평가: eval/golden.csv의 질문을 서비스(qa.ask)에 넣고 5개 영역을 채점한다

  1. 질의 이해     HCX 해석 결과(parsed)를 정답 칸(type, indicator, regions, year ...)과 비교
  2. 데이터 정확성 계산 결과(result)에서 target 값을 꺼내 expected와 비교
  3. 답변 신뢰성   답할 수 있는 질문은 출처와 KOSIS 통계표 링크가 붙었는지,
                   답변 불가 질문은 안내 문장이 있는지
  4. 답변 불가 판단 status를 expected_status와 비교 (거절 여부 + 사유 코드)
  5. 자연어 품질   자동 채점하지 않는다. 결과 CSV의 quality 칸(0~2점)을 사람이 채운다

골든셋 칸 (빈 칸은 채점하지 않음)
  id, category, question
  expected_status  ok 또는 답변 불가 사유 (missing_region, unknown_indicator, out_of_range ...)
  type, indicator, regions(;로 구분), year, start_year, end_year, sex, by   질의 이해 정답
  target           비교할 값: value | diff | rate | gap | highest | lowest | ranking | share:<항목>
  expected         정답 값. 숫자는 적은 자릿수만큼 비교 (20.4 → ±0.05), 지역은 ;로 구분
  expected_source  정답을 어디서 확인했는지 (KOSIS 화면 경로 등)

결과: data/eval/golden_YYYYMMDD_HHMMSS.csv (질문별, 엑셀용) / .md (영역별 요약)
      .md 머리에 회차, 변경 내용, 채점에 쓴 DB(생성·수집 시각)를 남겨 회차끼리 비교한다
실행: python -m scripts.eval_golden                  (전체, 질문마다 HCX 1회)
      python -m scripts.eval_golden --only g01 g02   (일부 질문만)
      python -m scripts.eval_golden --round 2 --note "남녀 비중 보정 추가"
      python -m scripts.eval_golden --interval 10      (호출 제한에 자주 걸릴 때)
"""

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.duckdb import db_path
from app.core.duckdb import rows as db_rows
from app.services import qa
from app.services.hcx_client import RATE_LIMIT_CODE

GOLDEN = Path("eval/golden.csv")
OUT_DIR = Path("data/eval")
CALL_INTERVAL = 3  # 호출 제한(42901) 대응 (초). 그래도 걸리면 HcxClient가 다시 시도
RATE_LIMIT_WAITS = (60, 120, 180)  # HcxClient 재시도까지 실패하면 질문 단위로 쉬고 다시 (초)
PARSE_FIELDS = ["type", "indicator", "regions", "year", "start_year", "end_year", "sex", "by"]
AREAS = ["질의 이해", "데이터 정확성", "답변 신뢰성", "답변 불가 판단", "표 선택"]
# 시트 골든셋(eval/golden_sheet.csv)의 기대 행동 ↔ 서비스 결과
#   답변: 답함 (수록 기간 밖이지만 추계값으로 대신 답한 경우 포함)
#   명확화: 어느 지역인지 되물음 / 범위안내: 지원 범위 밖이라 안내 / 대안안내: 다른 연도·지역을 제안
ACTIONS = {
    "ok": "답변",
    "ambiguous_region": "명확화",
    "missing_region": "명확화",
    "not_supported_question": "범위안내",
    "unknown_indicator": "범위안내",
    "out_of_range": "대안안내",
    "region_not_in_year": "대안안내",
    "missing_data": "대안안내",
    "unknown_region": "다시질문",
}
TABLE_ID = re.compile(r"[A-Z]{2,3}_[0-9A-Z_]+")


def load_golden(path: Path = GOLDEN) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


# ---- 채점 ---------------------------------------------------------------------------


def _norm_region(text: str) -> str:
    # "부산광역시", "부산" → "부산" / "서울특별시 중구", "서울 중구" → "서울중구"
    parts = re.split(r"\s|>", text)
    return "".join(re.sub(r"(특별자치시|특별자치도|특별시|광역시|도)$", "", p) for p in parts)


def _regions(text: str) -> list[str]:
    return [_norm_region(x) for x in text.split(";") if x.strip()]


def grade_parse(case: dict[str, str], parsed: dict[str, Any]) -> tuple[bool | None, list[str]]:
    """정답 칸이 있는 항목만 비교. (통과 여부, 틀린 항목). 비교할 칸이 없으면 None"""
    wrong = []
    for field in PARSE_FIELDS:
        want = case.get(field, "")
        if not want:
            continue
        got = parsed.get(field)
        if field == "regions":
            ok = _regions(want) == [_norm_region(r) for r in got or []]
        elif field == "sex":  # 비어 있으면 서비스가 "계"로 계산
            ok = (got or "계") == want
        elif field == "indicator":
            ok = qa.indicators.canonical_name(got or "") == want or got == want
        else:
            ok = str(got) == want
        if not ok:
            wrong.append(f"{field}={got!r} (정답 {want})")
    checked = any(case.get(f) for f in PARSE_FIELDS)
    return (not wrong if checked else None), wrong


def pick(result: dict[str, Any], target: str) -> Any:
    """계산 결과에서 비교할 값을 꺼낸다"""
    change = result.get("change") or {}
    if target == "value":
        return result.get("value")
    if target in ("diff", "rate"):
        return change.get(target)
    if target in ("gap", "highest", "lowest"):
        return result.get(target)
    if target == "ranking":
        return [x["region"] for x in result.get("ranking") or []]
    if target.startswith("share:"):
        label = target.removeprefix("share:")
        shares = [p["share"] for p in result.get("parts", []) if p["label"].startswith(label)]
        return shares[0] if shares else None
    raise ValueError(f"알 수 없는 target: {target}")


def grade_value(case: dict[str, str], result: dict[str, Any]) -> tuple[bool | None, Any]:
    """(일치 여부, 서비스 값). 정답이 없거나 답변 불가가 정답이면 None"""
    target, want = case.get("target", ""), case.get("expected", "")
    if not target or not want:
        return None, None
    got = pick(result, target)
    if got is None:
        return False, None
    if target in ("highest", "lowest", "ranking"):
        want_list = _regions(want)
        # 순위는 정답 개수만큼 앞에서 비교 ("가장 낮은 시도는?"에 전체 순위를 함께 보여줘도 맞음)
        got_list = got[: len(want_list)] if isinstance(got, list) else [got]
        return [_norm_region(x) for x in got_list] == want_list, got_list
    expected = Decimal(want.replace(",", ""))
    # 정답을 적은 자릿수의 반올림 오차까지 허용 (20.4 → ±0.05, 9299548 → ±0.5)
    tolerance = Decimal(1).scaleb(expected.as_tuple().exponent) / 2
    return abs(Decimal(str(got)) - expected) <= tolerance, got


def grade_reliability(status: str | None, res: dict[str, Any]) -> bool:
    if status == "ok":
        return bool(res["sources"]) and all(s.get("url") for s in res["sources"])
    return bool(res["answer"])


def action_of(res: dict[str, Any]) -> str | None:
    """서비스 결과 → 시트의 행동 이름"""
    status = res.get("status")
    suggestion = (res.get("result") or {}).get("suggestion") or {}
    if status == "out_of_range" and suggestion.get("status") == "ok":
        return "답변"  # 수록 기간 밖이지만 추계값으로 대신 답함
    return ACTIONS.get(status)


def grade_refusal(
    case: dict[str, str], status: str | None, res: dict[str, Any] | None = None
) -> tuple[bool, bool]:
    """(거절 여부가 맞음, 사유 코드까지 맞음)
    시트 골든셋(allowed_action 칸)은 사유 코드 대신
    기대 행동(답변/명확화/범위안내/대안안내)과 비교"""
    want = case.get("expected_status") or "ok"
    if case.get("allowed_action"):
        action = action_of(res or {"status": status})
        allowed = {a.strip() for a in case["allowed_action"].split(",")}
        hit = action in allowed
        return hit or (action == "답변") == (want == "ok"), hit
    return (want == "ok") == (status == "ok"), want == status


def grade_tables(case: dict[str, str], res: dict[str, Any]) -> bool | None:
    """답한 경우 실제로 쓴 통계표가 허용 표 안에 있는지. 허용 표 칸이 없으면 채점하지 않음"""
    allowed = set(TABLE_ID.findall(case.get("allowed_tables", "")))
    if not allowed or action_of(res) != "답변":
        return None
    used = {s.get("tbl_id") for s in res.get("sources") or []}
    return bool(used) and used <= allowed


def grade(case: dict[str, str], res: dict[str, Any]) -> dict[str, Any]:
    status = res.get("status")
    parse_ok, parse_wrong = grade_parse(case, res.get("parsed") or {})
    value_ok, got = grade_value(case, res.get("result") or {})
    refuse_ok, reason_ok = grade_refusal(case, status, res)
    trace = res.get("trace") or {}
    return {
        "id": case["id"],
        "category": case.get("category", ""),
        "question": case["question"],
        "expected_status": case.get("expected_status") or "ok",
        "status": status,
        "질의 이해": parse_ok,
        "parse_wrong": "; ".join(parse_wrong),
        "데이터 정확성": value_ok,
        "target": case.get("target", ""),
        "expected": case.get("expected", ""),
        "got": ";".join(got) if isinstance(got, list) else got,
        "답변 신뢰성": grade_reliability(status, res),
        "답변 불가 판단": refuse_ok,
        "reason_ok": reason_ok,
        "action": action_of(res) or "",
        "expected_action": case.get("allowed_action", ""),
        "표 선택": grade_tables(case, res),
        "tables": ",".join(s.get("tbl_id") or "" for s in res.get("sources") or []),
        "answer": res.get("answer", ""),
        "quality": "",  # 자연어 품질 0~2점, 사람이 채움
        "error": res.get("error", ""),
        # 원인 분석용: HCX 원문과 코드 보정까지 반영한 해석
        "hcx_raw": trace.get("hcx_raw", ""),
        "parsed": json.dumps(res.get("parsed") or {}, ensure_ascii=False),
    }


# ---- 실행 ---------------------------------------------------------------------------


async def _ask(question: str) -> dict[str, Any]:
    """호출 제한이 HcxClient의 짧은 재시도로 안 풀리면 몇 분 쉬고 같은 질문을 다시 보낸다"""
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return await qa.ask(question)
        except Exception as e:  # 한 질문이 실패해도 나머지는 계속
            if wait is None or RATE_LIMIT_CODE not in str(e):
                return {"status": "error", "answer": "", "error": f"{type(e).__name__}: {e}"}
            print(f"  호출 제한, {wait}초 쉬고 다시 시도")
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


async def run(cases: list[dict[str, str]], interval: float = CALL_INTERVAL) -> list[dict[str, Any]]:
    rows = []
    for i, case in enumerate(cases, 1):
        if i > 1:
            await asyncio.sleep(interval)
        res = await _ask(case["question"])
        row = grade(case, res)
        marks = " ".join(f"{a}:{_mark(row[a])}" for a in AREAS)
        error = f" | {row['error']}" if row["error"] else ""
        print(f"[{i}/{len(cases)}] {case['id']} status={row['status']} | {marks}{error}")
        answer = (row["answer"] or "(답변 없음)").replace("\n", "\n     ")
        print(f"  Q. {case['question']}\n  A. {answer}\n")
        rows.append(row)
    return rows


def _mark(v: bool | None) -> str:
    return "-" if v is None else ("O" if v else "X")


def _problems(r: dict[str, Any]) -> list[str]:
    """틀린 영역과 내용. 거절은 맞았는데 사유 코드만 다른 경우도 오류 분석용으로 남긴다"""
    out = []
    if r["error"]:
        out.append(f"오류: {r['error']}")
    if r["질의 이해"] is False:
        out.append(f"질의 이해: {r['parse_wrong']}")
    if r["데이터 정확성"] is False:
        out.append(f"데이터 정확성: {r['target']} {r['got']} (정답 {r['expected']})")
    if r["답변 신뢰성"] is False:
        out.append("답변 신뢰성: 출처·통계표 링크 또는 안내 문장 없음")
    if r["표 선택"] is False:
        out.append(f"표 선택: {r['tables']}")
    if not r["reason_ok"]:
        area = "답변 불가 판단" if r["답변 불가 판단"] is False else "사유 코드"
        if r["expected_action"]:
            got = f"{r['action']} / status {r['status']}"
            out.append(f"{area}: {got} (정답 {r['expected_action']})")
        else:
            out.append(f"{area}: status {r['status']} (정답 {r['expected_status']})")
    return out


def summarize(rows: list[dict[str, Any]]) -> str:
    lines = ["| 영역 | 통과 | 채점 대상 | 정확도 |", "|---|---|---|---|"]
    for area in AREAS:
        graded = [r[area] for r in rows if r[area] is not None]
        rate = f"{sum(graded) / len(graded) * 100:.1f}%" if graded else "-"
        lines.append(f"| {area} | {sum(graded)} | {len(graded)} | {rate} |")
    refusals = [r for r in rows if r["expected_status"] != "ok"]
    reason = sum(r["reason_ok"] for r in refusals)
    lines.append(f"\n답변 불가 질문 {len(refusals)}개 중 사유 코드까지 맞음 {reason}개")
    if any(r["expected_action"] for r in rows):
        hit = sum(r["reason_ok"] for r in rows)
        lines.append(f"기대 행동(답변/명확화/범위안내/대안안내) 일치 {hit}/{len(rows)}")
    lines.append("자연어 품질은 결과 CSV의 quality 칸(0~2점)을 사람이 채점합니다.")

    failed = [(r, _problems(r)) for r in rows]
    failed = [(r, p) for r, p in failed if p]
    if failed:
        lines += ["\n## 틀린 질문", "", "| id | 질문 | 내용 |", "|---|---|---|"]
        for r, problems in failed:
            lines.append(f"| {r['id']} | {r['question']} | {'<br>'.join(problems)} |")
    return "\n".join(lines)


def db_info() -> str:
    """채점에 쓴 DB. 정확도가 바뀐 이유가 코드인지 데이터인지 구분하려고 남긴다"""
    path = db_path()
    built = datetime.fromtimestamp(path.stat().st_mtime)
    # zip 원본으로 만든 DB는 수집 시각이 없어 catalog에 collected_at 칸 자체가 없다
    has_column = db_rows(
        "select 1 from information_schema.columns"
        " where table_name = 'catalog' and column_name = 'collected_at'"
    )
    collected = (
        db_rows("select max(collected_at) as at from catalog")[0]["at"] if has_column else None
    )
    collected_text = f"{collected:%Y-%m-%d %H:%M}" if collected else "기록 없음 (zip 원본)"
    return f"{path.name} (생성 {built:%Y-%m-%d %H:%M}, 최근 수집 {collected_text})"


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 자동 평가")
    parser.add_argument("--golden", type=Path, default=GOLDEN, help="골든셋 CSV")
    parser.add_argument("--only", nargs="+", help="이 id의 질문만 실행")
    parser.add_argument("--round", help="회차 번호 (결과 파일 이름과 보고서에 표시)")
    parser.add_argument("--note", default="", help="이번 회차에 바꾼 내용")
    parser.add_argument("--interval", type=float, default=CALL_INTERVAL, help="질문 사이 대기 (초)")
    args = parser.parse_args()

    cases = load_golden(args.golden)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    rows = asyncio.run(run(cases, args.interval))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"golden_r{args.round}" if args.round else "golden"
    stem = OUT_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    # utf-8-sig: 엑셀에서 열어도 한글이 깨지지 않음
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    head = [f"# 골든셋 평가 {datetime.now():%Y-%m-%d %H:%M}", ""]
    if args.round:
        head.append(f"- 회차: {args.round}")
    if args.note:
        head.append(f"- 변경 내용: {args.note}")
    head += [f"- DB: {db_info()}", f"- 질문: {len(rows)}개", "", ""]
    report = "\n".join(head)
    stem.with_suffix(".md").write_text(report + summarize(rows) + "\n", encoding="utf-8")
    print("\n" + summarize(rows))
    print(f"\n저장: {stem}.csv, {stem}.md")


if __name__ == "__main__":
    main()
