import argparse
import asyncio
import csv
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts import devset_grade as dg
from scripts import refusal_reasons as rr

OUT_DIR = Path("data/eval")
CALL_INTERVAL = 3
RETRY_BUDGET = 90
KINDS = ["단순 수치", "집계", "추이", "비교", "순위", "구성비", "인구 통계 지표"]


def load_golden(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _memory_sessions() -> None:
    from app.services import ask_session

    store: dict[str, Any] = {}

    async def load(session_id, at):
        return store.get(session_id), False

    async def save(session_id, state, conn=None):
        store[session_id] = state

    async def log_turn(*args, **kwargs):
        return None

    @asynccontextmanager
    async def turn_writer(*args, **kwargs):
        yield object()

    ask_session.load, ask_session.save, ask_session.log_turn = load, save, log_turn
    ask_session.turn_writer = turn_writer


async def _turn(session_id: str | None, question: str) -> dict[str, Any]:
    from app.services import ask_session

    try:
        return await ask_session.handle(session_id, question)
    except Exception as e:
        return {"status": "error", "answer": "", "error": f"{type(e).__name__}: {e}"}


async def run_case(row: dict[str, str]) -> tuple[dict, dict | None, int]:
    start = time.perf_counter()
    first = await _turn(None, row["질문"])
    final = None
    if row["기대 행동"] == dg.CLARIFY and dg.action_of(first) == dg.CLARIFY and row["사용자 답"]:
        final = await _turn(first.get("session_id"), row["사용자 답"])
    return first, final, round((time.perf_counter() - start) * 1000)


def flow_columns(g: dg.Grade, first: dict, final: dict | None) -> dict[str, str]:
    from app.services import flow

    turns = [("첫 질문", first), ("되묻기 답", final)] if final else [("", first)]
    texts = []
    for title, res in turns:
        steps = (res.get("trace") or {}).get("flow") or []
        body = flow.text(steps) or "(흐름 기록 없음)"
        texts.append(f"[{title}]\n{body}" if title else body)
    explain = ""
    if g.correct != "O":
        steps = ((final or first).get("trace") or {}).get("flow") or []
        if (s := flow.deciding(steps)) is not None:
            where = f"{s['stage']} [{s['result']}] {s.get('detail') or ''}".strip()
            explain = f"{g.reason} ← 답을 정한 단계: {where}"
        else:
            explain = g.reason
    return {"처리 흐름": "\n".join(texts), "오답 설명": explain}


STAGE_NAMES = {
    "understand": "질문 이해",
    "follow_up": "이어 묻는 말",
    "slots": "조건 추출",
    "plan": "계획",
    "llm_pick": "후보 검토",
    "together": "함께 볼 표",
    "cell_pick": "칸 고르기",
    "alternative": "대안 고르기",
    "unknown": "(이름 없음)",
}


def llm_columns(first: dict, final: dict | None) -> dict[str, Any]:
    from app.services import llm_calls

    turns = [t for t in (first, final) if t]
    if not any("llm_calls" in (t.get("trace") or {}) for t in turns):
        return {k: "" for k in LLM_COLUMNS}
    calls = [c for t in turns for c in (t.get("trace") or {}).get("llm_calls") or []]
    t = llm_calls.totals(calls)
    return {
        "LLM 호출": t["calls"],
        "HCX 호출": t["hcx"],
        "Gemma 호출": t["gemma"],
        "입력 토큰": t["prompt_tokens"],
        "출력 토큰": t["completion_tokens"],
        "예약 토큰": t["reserved_tokens"],
        "다시 시도": t["retries"],
        "LLM 대기(ms)": t["waited_ms"],
        "LLM 실패": t["failed"],
        "단계별 호출": json.dumps(t["by_stage"], ensure_ascii=False),
    }


LLM_COLUMNS = [
    "LLM 호출",
    "HCX 호출",
    "Gemma 호출",
    "입력 토큰",
    "출력 토큰",
    "예약 토큰",
    "다시 시도",
    "LLM 대기(ms)",
    "LLM 실패",
    "단계별 호출",
]


def refusal_columns(last: dict) -> dict[str, str]:
    r = rr.reason_of(last)
    if r is None:
        return {"거절 원인": "", "거절 원인 문구": "", "표 찾기 판정": ""}
    return {
        "거절 원인": rr.label_text(r),
        "거절 원인 문구": f"{r['tbl_id']} {r['skip']}".strip(),
        "표 찾기 판정": r["search"],
    }


def refusal_summary(recs: list[dict]) -> list[str]:
    by: dict[str, list[str]] = {}
    for r in recs:
        if r.get("거절 원인"):
            by.setdefault(r["거절 원인"], []).append(r["문항"])
    if not by:
        return []
    answers = {r["문항"] for r in recs if r["기대 행동"] == dg.ANSWER}
    exact = {r["문항"] for r in recs if r.get("표 찾기 판정") == rr.SEARCH_OK}
    total = sum(len(ids) for ids in by.values())
    head = ["", "## 표 찾고도 거절한 원인 (통계표 안내로 끝난 문항)", ""]
    return head + rr.table(by, total, answers, exact=exact)


def record(row: dict[str, str], g: dg.Grade, first: dict, final: dict | None, ms: int) -> dict:
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
        **flow_columns(g, first, final),
        "질문 종류": row.get("질문 종류", ""),
        "데이터 경로": row.get("데이터 경로", ""),
        "필요 표 수": row.get("필요 표 수", ""),
        "status": last.get("status"),
        "request_type": last.get("request_type"),
        "쓴 표": ",".join(s.get("tbl_id") or "" for s in last.get("sources") or []),
        "거절 근거": (last.get("tables") or {}).get("reason") or "",
        **refusal_columns(last),
        "응답 시간(ms)": ms,
        **llm_columns(first, final),
        "상세": json.dumps(g.details, ensure_ascii=False, default=str),
        "되묻기 문장": (first.get("answer") or "") if final else "",
        "답변": last.get("answer") or "",
        "parsed": json.dumps(last.get("parsed") or {}, ensure_ascii=False, default=str),
        "applied": json.dumps(last.get("applied") or [], ensure_ascii=False, default=str),
        "error": last.get("error", ""),
    }


async def run(rows: list[dict[str, str]], interval: float, raw_path: Path) -> list[dict]:
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
            print(f"[{i}/{len(rows)}] {g.id} {g.expected}→{g.actual} 정답 {g.correct} {g.reason}")
    return out


def regrade(rows: list[dict[str, str]], raw_path: Path) -> list[dict]:
    by_id = {r["문항 ID"]: r for r in rows}
    out = []
    for text in raw_path.read_text(encoding="utf-8").splitlines():
        line = json.loads(text)
        if (row := by_id.get(line["id"])) is None:
            continue
        g = dg.grade(row, line["first"], line["final"])
        out.append(record(row, g, line["first"], line["final"], line["ms"]))
    return out


def _rate(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total * 100:.1f}%)" if total else "-"


def summarize(recs: list[dict]) -> str:
    answer = [r for r in recs if r["기대 행동"] != dg.REFUSE]
    refuse = [r for r in recs if r["기대 행동"] == dg.REFUSE]
    ok = lambda rs: sum(r["정답"] == "O" for r in rs)  # noqa: E731
    lines = ["## 정답률", "", "| 구분 | 정답 |", "|---|---|"]
    lines.append(f"| 전체 (답변·되묻기·대안 답변 문항) | {_rate(ok(answer), len(answer))} |")
    alt = [r for r in recs if r["기대 행동"] == dg.ALTERNATIVE]
    if alt:
        lines.append(f"| 대안 답변 | {_rate(ok(alt), len(alt))} |")
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
    lines += refusal_summary(recs)

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
    lines += llm_summary(recs)
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


def llm_summary(recs: list[dict]) -> list[str]:
    rs = [r for r in recs if r.get("LLM 호출") not in ("", None)]
    if not rs:
        return []
    n = len(rs)
    total = lambda key: sum(int(r[key]) for r in rs)  # noqa: E731
    calls = total("LLM 호출")
    lines = [
        "",
        "## LLM 호출 (문항당, 되묻기 문항은 두 턴 합)",
        "",
        f"- 호출: {calls}회 (문항당 평균 {calls / n:.1f}회, HCX {total('HCX 호출')} · "
        f"Gemma {total('Gemma 호출')})",
        f"- 토큰: 입력 {total('입력 토큰'):,} · 출력 {total('출력 토큰'):,} · "
        f"예약(응답 최대 길이 합) {total('예약 토큰'):,}",
        f"- 다시 시도 {total('다시 시도')}회 · 기다린 시간 {total('LLM 대기(ms)') / 1000:.1f}초 · "
        f"실패 {total('LLM 실패')}회",
    ]
    stages: dict[str, dict[str, int]] = {}
    asked: Counter = Counter()
    for r in rs:
        for name, s in json.loads(r["단계별 호출"] or "{}").items():
            acc = stages.setdefault(name, {"calls": 0, "ms": 0, "tokens": 0})
            for k in acc:
                acc[k] += s.get(k) or 0
            asked[name] += 1
    lines += [
        "",
        "| 단계 | 호출 | 부른 문항 | 문항당 | 평균 시간 | 토큰 합 |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in sorted(stages.items(), key=lambda kv: -kv[1]["calls"]):
        lines.append(
            f"| {STAGE_NAMES.get(name, name)} | {s['calls']} | {asked[name]} | "
            f"{s['calls'] / n:.2f} | {s['ms'] / s['calls'] / 1000:.1f}초 | {s['tokens']:,} |"
        )
    top = sorted(rs, key=lambda r: -int(r["LLM 호출"]))[:5]
    lines += [
        "",
        "호출이 가장 많은 문항: " + ", ".join(f"{r['문항']} {r['LLM 호출']}회" for r in top),
    ]
    return lines


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
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=RETRY_BUDGET,
        help="HCX 호출 하나가 호출 제한이 풀리기를 기다리는 시간 상한 (초)",
    )
    args = parser.parse_args()
    from app.core.config import get_settings

    get_settings().hcx_retry_budget_seconds = args.retry_budget
    if not args.question_cache:
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
