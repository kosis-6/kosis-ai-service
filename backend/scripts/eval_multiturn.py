import argparse
import asyncio
import csv
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from scripts import devset_grade as dg
from scripts import eval_devset as ev

OUT_DIR = Path("data/eval")
REQUEST_TYPES = {
    "새 질문": "new",
    "조건 변경": "change",
    "요약 요청": "summary",
    "차트 변경": "chart",
}


def conversations(rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by[row["대화 ID"]].append(row)
    return [sorted(turns, key=lambda r: int(r["턴"] or 0)) for turns in by.values()]


async def run_conversation(turns: list[dict[str, str]], interval: float, raw) -> list[dict]:
    session = None
    out = []
    for i, row in enumerate(turns):
        if i:
            await asyncio.sleep(interval)
        start = time.perf_counter()
        first = await ev._turn(session, row["질문"])
        session = first.get("session_id") or session
        final = None
        if (
            row["기대 행동"] == dg.CLARIFY
            and dg.action_of(first) == dg.CLARIFY
            and row["사용자 답"]
        ):
            final = await ev._turn(session, row["사용자 답"])
            session = final.get("session_id") or session
        ms = round((time.perf_counter() - start) * 1000)
        line = {"id": row["문항 ID"], "first": first, "final": final, "ms": ms}
        raw.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        raw.flush()
        g = dg.grade(row, first, final)
        last = final or first
        out.append(
            {
                "문항": g.id,
                "대화": row["대화 ID"],
                "턴": int(row["턴"] or 0),
                "기대 행동": g.expected,
                "실제 행동": g.actual,
                "기대 요청 종류": row.get("기대 요청 종류", ""),
                "요청 종류": first.get("request_type") or "",
                "정답": g.correct,
                "틀린 이유": g.reason,
                "숫자": g.number,
                "조건": g.condition,
                "출처": g.source,
                "status": last.get("status"),
                "응답 시간(ms)": ms,
            }
        )
        print(f"{g.id} 턴{row['턴']} {g.expected}→{g.actual} 정답 {g.correct} {g.reason}")
    return out


async def run(rows, interval: float, raw_path: Path) -> list[dict]:
    ev._memory_sessions()
    out: list[dict] = []
    with raw_path.open("w", encoding="utf-8") as raw:
        for k, turns in enumerate(conversations(rows), 1):
            if k > 1:
                await asyncio.sleep(interval)
            out += await run_conversation(turns, interval, raw)
    return out


def _same_type(r: dict) -> bool:
    return REQUEST_TYPES.get(r["기대 요청 종류"], r["기대 요청 종류"]) == r["요청 종류"]


def _by_type(recs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        out[r["기대 요청 종류"] or "-"].append(r)
    return out


def _rate(hit: int, total: int) -> str:
    return f"{hit}/{total} ({hit / total * 100:.1f}%)" if total else "-"


def summarize(recs: list[dict], version: str, note: str) -> str:
    ok = [r for r in recs if r["정답"] == "O"]
    follow = [r for r in recs if r["턴"] > 1]
    first = [r for r in recs if r["턴"] == 1]
    follow_ok = sum(r["정답"] == "O" for r in follow)
    same_action = [r for r in recs if r["기대 행동"] == r["실제 행동"]]
    typed = [r for r in follow if r["기대 요청 종류"]]
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_conv[r["대화"]].append(r)
    whole = [c for c in by_conv.values() if all(r["정답"] == "O" for r in c)]
    lines = [
        f"# 멀티턴 평가 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- 골든셋 버전: {version or '-'}",
        f"- 주요 변경 사항: {note or '-'}",
        f"- 대화 {len(by_conv)}개 · 턴 {len(recs)}개",
        "",
        "## 정답률",
        "",
        "| 구분 | 정답 |",
        "|---|---|",
        f"| 전체 턴 | {_rate(len(ok), len(recs))} |",
        f"| 첫 턴 | {_rate(sum(r['정답'] == 'O' for r in first), len(first))} |",
        f"| 후속 턴 (연속 대화 정확도) | {_rate(follow_ok, len(follow))} |",
        f"| 대화 전체가 맞음 | {_rate(len(whole), len(by_conv))} |",
        f"| 행동이 기대와 같음 | {_rate(len(same_action), len(recs))} |",
        f"| 후속 턴 요청 종류 일치 | {_rate(sum(_same_type(r) for r in typed), len(typed))} |",
        "",
        "## 후속 턴 (기대 요청 종류별)",
        "",
        "| 기대 요청 종류 | 정답 | 요청 종류 일치 | 오답 이유 |",
        "|---|---|---|---|",
        *(
            f"| {k} | {_rate(sum(r['정답'] == 'O' for r in v), len(v))} | "
            f"{_rate(sum(_same_type(r) for r in v), len(v))} | "
            + ", ".join(
                f"{why} {n}"
                for why, n in Counter(r["틀린 이유"] for r in v if r["정답"] != "O").most_common()
            )
            + " |"
            for k, v in _by_type(follow).items()
        ),
        "",
        "## 틀린 이유 (처음 어긋난 단계)",
        "",
        "| 단계 | 건수 |",
        "|---|---|",
        *(
            f"| {k or '-'} | {v} |"
            for k, v in Counter(r["틀린 이유"] for r in recs if r["정답"] != "O").most_common()
        ),
        "",
        "## 기대 행동 × 실제 행동",
        "",
        "| 기대 \\ 실제 | 건수 |",
        "|---|---|",
        *(
            f"| {a} → {b} | {v} |"
            for (a, b), v in Counter((r["기대 행동"], r["실제 행동"]) for r in recs).most_common()
        ),
        "",
        "## 턴별 결과 (문항 ID만)",
        "",
        "| 문항 | 턴 | 정답 | 틀린 이유 |",
        "|---|---|---|---|",
        *(f"| {r['문항']} | {r['턴']} | {r['정답']} | {r['틀린 이유']} |" for r in recs),
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="멀티턴 골든셋 평가 (결과 집계만 남긴다)")
    parser.add_argument("--golden", type=Path, required=True, help="멀티턴 골든셋 CSV")
    parser.add_argument("--version", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--interval", type=float, default=ev.CALL_INTERVAL)
    parser.add_argument("--retry-budget", type=float, default=ev.RETRY_BUDGET)
    parser.add_argument("--resummarize", type=Path, help="저장한 결과 CSV로 요약만 다시 만든다")
    args = parser.parse_args()
    if args.resummarize:
        with args.resummarize.open(encoding="utf-8-sig") as f:
            recs = [{**r, "턴": int(r["턴"])} for r in csv.DictReader(f)]
        md = args.resummarize.with_suffix(".md")
        md.write_text(summarize(recs, args.version, args.note), encoding="utf-8")
        print(f"저장: {md}")
        return
    from app.core.config import get_settings

    get_settings().hcx_retry_budget_seconds = args.retry_budget
    get_settings().question_cache = False
    rows = [r for r in ev.load_golden(args.golden) if r.get("문항 ID") and r.get("대화 ID")]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = OUT_DIR / f"multiturn_{datetime.now():%Y%m%d_%H%M%S}"
    recs = asyncio.run(run(rows, args.interval, stem.with_suffix(".raw.jsonl")))
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=recs[0].keys())
        writer.writeheader()
        writer.writerows(recs)
    stem.with_suffix(".md").write_text(summarize(recs, args.version, args.note), encoding="utf-8")
    print(f"저장: {stem}.csv, {stem}.md")


if __name__ == "__main__":
    main()
