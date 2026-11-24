import argparse
import asyncio
import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.stats_db import rows as db_rows
from app.services import ask_session, qa
from app.services.hcx_client import RATE_LIMIT_CODE

GOLDEN = Path("eval/golden.csv")
OUT_DIR = Path("data/eval")
CALL_INTERVAL = 3
RATE_LIMIT_WAITS = (60, 120, 180)
PARSE_FIELDS = ["type", "indicator", "regions", "year", "start_year", "end_year", "sex", "by"]
AREAS = ["질의 이해", "데이터 정확성", "답변 신뢰성", "답변 불가 판단", "표 선택"]
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
    "out_of_scope": "범위안내",
    "causal_question": "범위안내",
    "prediction_question": "대안안내",
    "band_mismatch": "대안안내",
    "not_statistics": "범위안내",
}
TABLE_ID = re.compile(r"[A-Z]{2,3}_[0-9A-Z_]+")


def load_golden(path: Path = GOLDEN) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _norm_region(text: str) -> str:
    parts = re.split(r"\s|>", text)
    return "".join(re.sub(r"(특별자치시|특별자치도|특별시|광역시|도)$", "", p) for p in parts)


def _regions(text: str) -> list[str]:
    return [_norm_region(x) for x in text.split(";") if x.strip()]


def grade_parse(case: dict[str, str], parsed: dict[str, Any]) -> tuple[bool | None, list[str]]:
    wrong = []
    for field in PARSE_FIELDS:
        want = case.get(field, "")
        if not want:
            continue
        got = parsed.get(field)
        if field == "regions":
            ok = _regions(want) == [_norm_region(r) for r in got or []]
        elif field == "sex":
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
    target, want = case.get("target", ""), case.get("expected", "")
    if not target or not want:
        return None, None
    got = pick(result, target)
    if got is None:
        return False, None
    if target in ("highest", "lowest", "ranking"):
        want_list = _regions(want)
        got_list = got[: len(want_list)] if isinstance(got, list) else [got]
        return [_norm_region(x) for x in got_list] == want_list, got_list
    expected = Decimal(want.replace(",", ""))
    tolerance = Decimal(1).scaleb(expected.as_tuple().exponent) / 2
    return abs(Decimal(str(got)) - expected) <= tolerance, got


def grade_reliability(status: str | None, res: dict[str, Any]) -> bool:
    if status == "ok":
        return bool(res["sources"]) and all(s.get("url") for s in res["sources"])
    return bool(res["answer"])


def action_of(res: dict[str, Any]) -> str | None:
    status = res.get("status")
    suggestion = (res.get("result") or {}).get("suggestion") or {}
    if status == "out_of_range" and suggestion.get("status") == "ok":
        return "답변"
    if status == "unknown_region" and (res.get("result") or {}).get("alternative"):
        return "대안안내"
    return ACTIONS.get(status)


def grade_refusal(
    case: dict[str, str], status: str | None, res: dict[str, Any] | None = None
) -> tuple[bool, bool]:
    want = case.get("expected_status") or "ok"
    if case.get("allowed_action"):
        action = action_of(res or {"status": status})
        allowed = {a.strip() for a in case["allowed_action"].split(",")}
        hit = action in allowed
        return hit or (action == "답변") == (want == "ok"), hit
    return (want == "ok") == (status == "ok"), want == status


def grade_tables(case: dict[str, str], res: dict[str, Any]) -> bool | None:
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
        "quality": "",
        "error": res.get("error", ""),
        "hcx_raw": trace.get("hcx_raw", ""),
        "parsed": json.dumps(res.get("parsed") or {}, ensure_ascii=False),
        "method": (res.get("result") or {}).get("method") or "",
        "grounded": (res.get("parsed") or {}).get("grounded", ""),
        "replaced": json.dumps(trace.get("replaced") or "", ensure_ascii=False),
    }


async def _ask(question: str, session: bool = False) -> dict[str, Any]:
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            if session:
                return await ask_session.handle(None, question, None, None)
            return await qa.ask(question)
        except Exception as e:
            if wait is None or RATE_LIMIT_CODE not in str(e):
                return {"status": "error", "answer": "", "error": f"{type(e).__name__}: {e}"}
            print(f"  호출 제한, {wait}초 쉬고 다시 시도")
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


async def run(
    cases: list[dict[str, str]], interval: float = CALL_INTERVAL, session: bool = False
) -> list[dict[str, Any]]:
    rows = []
    for i, case in enumerate(cases, 1):
        if i > 1:
            await asyncio.sleep(interval)
        res = await _ask(case["question"], session)
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
    log = db_rows("select max(source_built_at) as built from load_log")[0]
    has_column = db_rows(
        "select 1 from information_schema.columns"
        " where table_name = 'catalog' and column_name = 'collected_at'"
    )
    collected = (
        db_rows("select max(collected_at) as at from catalog")[0]["at"] if has_column else None
    )
    collected_text = f"{collected:%Y-%m-%d %H:%M}" if collected else "기록 없음 (zip 원본)"
    return f"PostgreSQL (생성 {log['built']:%Y-%m-%d %H:%M}, 최근 수집 {collected_text})"


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 자동 평가")
    parser.add_argument("--golden", type=Path, default=GOLDEN, help="골든셋 CSV")
    parser.add_argument("--only", nargs="+", help="이 id의 질문만 실행")
    parser.add_argument("--round", help="회차 번호 (결과 파일 이름과 보고서에 표시)")
    parser.add_argument("--note", default="", help="이번 회차에 바꾼 내용")
    parser.add_argument("--interval", type=float, default=CALL_INTERVAL, help="질문 사이 대기 (초)")
    parser.add_argument(
        "--session", action="store_true", help="대화 처리 전체(ask_session.handle)로 묻기"
    )
    args = parser.parse_args()

    cases = load_golden(args.golden)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    rows = asyncio.run(run(cases, args.interval, args.session))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"golden_r{args.round}" if args.round else "golden"
    stem = OUT_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    head = [f"# 골든셋 평가 {datetime.now():%Y-%m-%d %H:%M}", ""]
    if args.round:
        head.append(f"- 회차: {args.round}")
    if args.note:
        head.append(f"- 변경 내용: {args.note}")
    head.append(f"- 경로: {'ask_session.handle (대화 처리 전체)' if args.session else 'qa.ask'}")
    head += [f"- DB: {db_info()}", f"- 질문: {len(rows)}개", "", ""]
    report = "\n".join(head)
    stem.with_suffix(".md").write_text(report + summarize(rows) + "\n", encoding="utf-8")
    print("\n" + summarize(rows))
    print(f"\n저장: {stem}.csv, {stem}.md")


if __name__ == "__main__":
    main()
