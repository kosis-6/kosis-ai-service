"""개발셋·테스트셋 자동 평가: 골든셋 시트(CSV로 내려받은 것)의 질문을 챗봇에 넣고 채점한다

  1. 문항마다 새 대화로 질문을 넣는다 (ask_session.handle, /ask와 같은 경로)
  2. 기대 행동이 되묻기이고 챗봇이 되물었으면, 사용자 답 칸을 같은 대화에 이어서 넣는다
  3. 최종 답을 scripts/devset_grade.py 규칙으로 채점한다 (사람 채점 없음)
     숫자·조건·출처·답변 불가·환각 숫자는 코드로 판정하고,
     원인·예측·정책 평가 문장은 표현 사전으로 "환각 문장 후보"만 뽑는다 (사람이 확인)

결과: data/eval/devset_YYYYMMDD_HHMMSS.csv        채점 기록 양식 + 원인 분석용 칸 (엑셀용)
      data/eval/devset_YYYYMMDD_HHMMSS.md         정답률·행동 정확도·환각·틀린 이유·응답 시간 요약
      data/eval/devset_YYYYMMDD_HHMMSS.raw.jsonl  챗봇 원응답 (--regrade로 다시 채점할 때 씀)
실행: python -m scripts.eval_devset --golden eval/devset_v1.csv
      python -m scripts.eval_devset --golden eval/devset_v1.csv --only D-001 D-002
      python -m scripts.eval_devset --golden eval/devset_v1.csv --round 1 --version v1
                                    --note "1차 평가"
      python -m scripts.eval_devset --golden eval/devset_v1.csv
                                    --regrade data/eval/devset_r1_….raw.jsonl
      (채점 규칙만 고쳤을 때)
골든셋 CSV: 구글 시트 "골든셋_개발셋_v1"의 개발셋 탭을 CSV로 내려받은 파일
테스트셋은 동결이라 결과 CSV의 문항별 칸을 보지 않고 .md의 정답률만 기록한다
"""

import argparse
import asyncio
import csv
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import devset_grade as dg

OUT_DIR = Path("data/eval")
CALL_INTERVAL = 3  # 호출 제한(42901) 대응 (초)
RATE_LIMIT_WAITS = (60, 120, 180)  # HcxClient 재시도까지 실패하면 질문 단위로 쉬고 다시 (초)
KINDS = ["단순 수치", "집계", "추이", "비교", "순위", "구성비", "인구 통계 지표"]


def load_golden(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


# ---- 실행 ---------------------------------------------------------------------------------


def _memory_sessions() -> None:
    """평가 대화가 개발 DB의 대화 기록(app.chat_sessions, chat_turns)에 남지 않게 메모리로 돌린다"""
    from app.services import ask_session

    store: dict[str, Any] = {}

    async def load(session_id, at):
        return store.get(session_id), False

    async def save(session_id, state):
        store[session_id] = state

    async def log_turn(*args, **kwargs):
        return None

    ask_session.load, ask_session.save, ask_session.log_turn = load, save, log_turn


async def _turn(session_id: str | None, question: str) -> dict[str, Any]:
    """한 턴. 호출 제한이 짧은 재시도로 안 풀리면 몇 분 쉬고 다시 보낸다"""
    from app.services import ask_session
    from app.services.hcx_client import RATE_LIMIT_CODE

    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            return await ask_session.handle(session_id, question)
        except Exception as e:  # 한 문항이 실패해도 나머지는 계속
            if wait is None or RATE_LIMIT_CODE not in str(e):
                return {"status": "error", "answer": "", "error": f"{type(e).__name__}: {e}"}
            print(f"  호출 제한, {wait}초 쉬고 다시 시도")
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


async def run_case(row: dict[str, str]) -> tuple[dict, dict | None, int]:
    """(첫 응답, 되묻기 뒤 응답, 걸린 시간 ms)"""
    start = time.perf_counter()
    first = await _turn(None, row["질문"])
    final = None
    if row["기대 행동"] == dg.CLARIFY and dg.action_of(first) == dg.CLARIFY and row["사용자 답"]:
        final = await _turn(first.get("session_id"), row["사용자 답"])
    return first, final, round((time.perf_counter() - start) * 1000)


def record(row: dict[str, str], g: dg.Grade, first: dict, final: dict | None, ms: int) -> dict:
    """채점 기록 양식 한 줄 + 원인 분석용 칸"""
    last = final or first
    return {
        "문항": g.id,
        "세트": g.set_name,
        "기대 행동": g.expected,
        "실제 행동": g.actual,
        "숫자": g.number,
        "조건": g.condition,
        "출처": g.source,
        "답변 불가": g.refusal,
        "환각": g.hallucination,
        "정답": g.correct,
        "틀린 이유": g.reason,
        # 분석용
        "질문 종류": row.get("질문 종류", ""),
        "데이터 경로": row.get("데이터 경로", ""),
        "필요 표 수": row.get("필요 표 수", ""),
        "status": last.get("status"),
        "request_type": last.get("request_type"),
        "쓴 표": ",".join(s.get("tbl_id") or "" for s in last.get("sources") or []),
        "거절 근거": (last.get("tables") or {}).get("reason") or "",
        "응답 시간(ms)": ms,
        "상세": json.dumps(g.details, ensure_ascii=False, default=str),
        "되묻기 문장": (first.get("answer") or "") if final else "",
        "답변": last.get("answer") or "",
        "parsed": json.dumps(last.get("parsed") or {}, ensure_ascii=False, default=str),
        "applied": json.dumps(last.get("applied") or [], ensure_ascii=False, default=str),
        "error": last.get("error", ""),
    }


async def run(rows: list[dict[str, str]], interval: float, raw_path: Path) -> list[dict]:
    """챗봇에 묻고 채점한다. 챗봇 원응답은 raw_path(JSONL)에 한 줄씩 바로 남긴다
    (채점 규칙만 고쳤을 때 --regrade로 챗봇을 다시 부르지 않고 다시 채점한다)"""
    _memory_sessions()
    out = []
    with raw_path.open("w", encoding="utf-8") as raw:
        for i, row in enumerate(rows, 1):
            if i > 1:
                await asyncio.sleep(interval)
            first, final, ms = await run_case(row)
            line = {"id": row["문항 ID"], "first": first, "final": final, "ms": ms}
            raw.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            raw.flush()
            g = dg.grade(row, first, final)
            out.append(record(row, g, first, final, ms))
            # 문항별 정답 내용은 찍지 않는다 (테스트셋은 오답을 보지 않는 규칙)
            print(f"[{i}/{len(rows)}] {g.id} {g.expected}→{g.actual} 정답 {g.correct} {g.reason}")
    return out


def regrade(rows: list[dict[str, str]], raw_path: Path) -> list[dict]:
    """저장해 둔 원응답(JSONL)을 지금 채점 규칙으로 다시 채점한다 (챗봇·HCX 호출 없음)"""
    by_id = {r["문항 ID"]: r for r in rows}
    out = []
    for text in raw_path.read_text(encoding="utf-8").splitlines():
        line = json.loads(text)
        if (row := by_id.get(line["id"])) is None:
            continue
        g = dg.grade(row, line["first"], line["final"])
        out.append(record(row, g, line["first"], line["final"], line["ms"]))
    return out


# ---- 요약 ---------------------------------------------------------------------------------


def _rate(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total * 100:.1f}%)" if total else "-"


def summarize(recs: list[dict]) -> str:
    """정답률은 답변·되묻기 문항(최종 답이 있는 문항) 기준. 답변 불가 문항은 따로 센다"""
    answer = [r for r in recs if r["기대 행동"] != dg.REFUSE]
    refuse = [r for r in recs if r["기대 행동"] == dg.REFUSE]
    ok = lambda rs: sum(r["정답"] == "O" for r in rs)  # noqa: E731
    lines = ["## 정답률", "", "| 구분 | 정답 |", "|---|---|"]
    lines.append(f"| 전체 (답변·되묻기 문항) | {_rate(ok(answer), len(answer))} |")
    for path in ("적재", "API"):
        rs = [r for r in answer if r["데이터 경로"] == path]
        lines.append(f"| {path} | {_rate(ok(rs), len(rs))} |")
    for kind in KINDS:
        rs = [r for r in answer if r["질문 종류"] == kind]
        lines.append(f"| {kind} | {_rate(ok(rs), len(rs))} |")
    multi = [r for r in answer if r["필요 표 수"] == "다중"]
    lines.append(f"| 다중 표 | {_rate(ok(multi), len(multi))} |")
    lines.append(f"| 답변 불가 문항 (유형·대안까지) | {_rate(ok(refuse), len(refuse))} |")

    same = sum(r["기대 행동"] == r["실제 행동"] for r in recs)
    errors = Counter(dg.action_error(r["기대 행동"], r["실제 행동"]) for r in recs)
    errors.pop("", None)
    lines += ["", "## 행동 정확도", "", f"- 기대 행동과 같음: {_rate(same, len(recs))}"]
    lines += [f"- {k}: {v}건" for k, v in errors.most_common()]
    pairs = Counter((r["기대 행동"], r["실제 행동"]) for r in recs)
    lines += ["", "| 기대 \\ 실제 | 답변 | 되묻기 | 답변 불가 |", "|---|---|---|---|"]
    for e in (dg.ANSWER, dg.CLARIFY, dg.REFUSE):
        lines.append(
            f"| {e} | "
            + " | ".join(str(pairs[(e, a)]) for a in (dg.ANSWER, dg.CLARIFY, dg.REFUSE))
            + " |"
        )

    halluc = sum(int(r["환각"]) for r in recs)
    claims = sum(len(json.loads(r["상세"]).get("환각 문장 후보") or []) for r in recs)
    lines += [
        "",
        "## 환각",
        "",
        f"- 환각 숫자: {halluc}건 (계산 결과·조건·출처에 없는 숫자)",
        f"- 환각 문장 후보: {claims}건 (원인·예측·정책 평가 표현, 사람이 확인)",
    ]

    reasons = Counter(r["틀린 이유"].split(" ")[0] for r in recs if r["틀린 이유"])
    lines += ["", "## 틀린 이유 (처음 어긋난 단계)", "", "| 단계 | 건수 |", "|---|---|"]
    lines += [f"| {k} | {reasons.get(k, 0)} |" for k in ("행동", "표", "조건", "계산", "요약")]

    lines += [
        "",
        "## 응답 시간 (문항당, 되묻기 문항은 두 턴 합)",
        "",
        "| 경로 | 평균 | 최대 |",
        "|---|---|---|",
    ]
    for path in ("적재", "API", ""):
        ms = [int(r["응답 시간(ms)"]) for r in recs if r["데이터 경로"] == path]
        label = path or "답변 불가 (경로 없음)"
        if ms:
            lines.append(f"| {label} | {sum(ms) / len(ms) / 1000:.1f}초 | {max(ms) / 1000:.1f}초 |")
    unmapped = Counter(
        r["status"] for r in refuse if json.loads(r["상세"]).get("불가 유형", "").startswith("(")
    )
    if unmapped:
        lines += [
            "",
            "참고: 답변 불가 유형을 정하지 못한 status (REFUSAL_TYPES에 없음): "
            + ", ".join(f"{k} {v}건" for k, v in unmapped.items()),
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="개발셋·테스트셋 자동 평가")
    parser.add_argument("--golden", type=Path, required=True, help="골든셋 시트를 내려받은 CSV")
    parser.add_argument("--only", nargs="+", help="이 문항 ID만 실행")
    parser.add_argument("--round", help="회차 번호")
    parser.add_argument("--version", default="", help="채점에 쓴 골든셋 버전 (v1 등)")
    parser.add_argument("--note", default="", help="이번 회차의 주요 변경 사항")
    parser.add_argument("--interval", type=float, default=CALL_INTERVAL, help="문항 사이 대기 (초)")
    parser.add_argument(
        "--regrade",
        type=Path,
        help="챗봇을 부르지 않고 저장해 둔 원응답(.raw.jsonl)을 다시 채점",
    )
    parser.add_argument(
        "--question-cache",
        action="store_true",
        help="질문 이해 캐시를 켠 채로 돌린다 (기본은 꺼서 문항마다 질문 이해를 새로 한다)",
    )
    args = parser.parse_args()
    if not args.question_cache:
        from app.core.config import get_settings

        get_settings().question_cache = False

    rows = load_golden(args.golden)
    if args.only:
        rows = [r for r in rows if r["문항 ID"] in args.only]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"devset_r{args.round}" if args.round else "devset"
    stem = OUT_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    if args.regrade:
        recs = regrade(rows, args.regrade)
    else:
        recs = asyncio.run(run(rows, args.interval, stem.with_suffix(".raw.jsonl")))
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=recs[0].keys())
        writer.writeheader()
        writer.writerows(recs)
    head = [f"# {recs[0]['세트'] or '골든셋'} 평가 {datetime.now():%Y-%m-%d %H:%M}", ""]
    head += [f"- 회차: {args.round}"] if args.round else []
    head += [f"- 골든셋 버전: {args.version}"] if args.version else []
    head += [f"- 주요 변경 사항: {args.note}"] if args.note else []
    head += [f"- 문항: {len(recs)}개", "", ""]
    body = summarize(recs)
    stem.with_suffix(".md").write_text("\n".join(head) + body + "\n", encoding="utf-8")
    print("\n" + body)
    print(f"\n저장: {stem}.csv, {stem}.md")


if __name__ == "__main__":
    main()
