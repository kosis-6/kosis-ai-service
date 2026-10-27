"""시연용 명령: 질문 답변 → pandas 정답 대조 → 전체 지표를 터미널에 차례로 보여준다

  ask    시트 문항 id(또는 질문 문장)를 서비스에 넣고 답변을 출력 (HCX 호출)
  check  답변 + 기존 자동 채점(행동·표·출처) + pandas 정답 재계산 채점을 나란히 출력
         --saved 를 붙이면 HCX를 부르지 않고 7회차 기록 답변으로 채점
  score  7회차 결과 전체를 pandas 채점해 지표 9개와 감점 문항 출력

실행: python -m scripts.demo ask s12 s30
      python -m scripts.demo ask "2025년 서울 인구는?"
      python -m scripts.demo check s12
      python -m scripts.demo check s16 --saved
      python -m scripts.demo score
"""

import argparse
import asyncio
import csv
import time
from pathlib import Path
from typing import Any

from app.services import qa
from scripts import eval_golden as ev

GOLDEN = Path("eval/golden_sheet.csv")
SAVED = Path("data/eval/golden_r7_20261026_151907.csv")
LINE = "━" * 70
MARK = {True: "○", False: "✗", None: "-"}


def cases() -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in csv.DictReader(GOLDEN.open(encoding="utf-8-sig"))}


def saved_rows() -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in csv.DictReader(SAVED.open(encoding="utf-8-sig"))}


def show_answer(qid: str, question: str, answer: str, seconds: float | None = None) -> None:
    took = f"  ({seconds:.1f}초)" if seconds is not None else ""
    print(f"\n{LINE}\n[질문] {qid}  {question}\n{LINE}")
    print(f"[서비스 답변]{took}\n")
    print(answer.rstrip() or "(답변 없음)")


async def live(case: dict[str, str]) -> tuple[dict[str, Any], float]:
    """서비스에 질문하고 eval_golden과 같은 형식의 결과 행으로 바꾼다"""
    start = time.perf_counter()
    res = await qa.ask(case["question"])
    return ev.grade(case, res), time.perf_counter() - start


async def ask(targets: list[str]) -> None:
    golds = cases()
    for target in targets:
        case = golds.get(target, {"id": "", "question": target})
        start = time.perf_counter()
        res = await qa.ask(case["question"])
        show_answer(case["id"], case["question"], res.get("answer", ""), time.perf_counter() - start)


async def check(targets: list[str], saved: bool) -> None:
    from scripts import eval_oracle as oracle  # DB를 읽어 몇 초 걸리므로 필요할 때만

    golds, records = cases(), saved_rows() if saved else {}
    for qid in targets:
        if saved:
            row, seconds = records[qid], None
        else:
            row, seconds = await live(golds[qid])
        row = {k: str(v) if isinstance(v, bool) else ("" if v is None else v) for k, v in row.items()}
        show_answer(qid, row["question"], row["answer"], seconds)
        if saved:
            print(f"\n(7회차 기록 답변: {SAVED.name})")

        old_pass = row["답변 불가 판단"] == "True" and row["표 선택"] != "False" and row["답변 신뢰성"] == "True"
        print(f"\n[기존 자동 채점]  행동·표·출처만 확인")
        print(f"  행동 {row['action']} (기대 {row['expected_action']}) {MARK.get(row['답변 불가 판단'] == 'True')}"
              f" · 표 {row['tables'] or '-'} {MARK[{'True': True, 'False': False}.get(row['표 선택'])]}"
              f" · 출처 {MARK.get(row['답변 신뢰성'] == 'True')}  →  {'통과' if old_pass else '실패'}")

        g = oracle.grade_case(row, golds[qid])
        print(f"\n[pandas 정답]  {g['basis']}")
        for item in (g["expect"] or "-").split("; "):
            print(f"  - {item}")
        print(f"\n[pandas 채점]  점수 {g['score']:g}")
        parts = [("조건", g.get("cond_ok")), ("값", g.get("values_ok")), ("고지", g.get("notice_ok"))]
        print("  " + " · ".join(f"{name} {MARK[v]}" for name, v in parts if v is not None))
        if g["reason"]:
            print(f"  사유: {g['reason']}")


def score() -> None:
    from scripts import eval_oracle as oracle

    golds = cases()
    rows = [oracle.grade_case(r, golds[r["id"]]) for r in saved_rows().values() if r["id"] in golds]
    print(f"\n{LINE}\n[pandas 정답 재계산 채점]  {SAVED.name}  ({len(rows)}문항)\n{LINE}\n")
    for name, value, desc in oracle.metrics(rows, golds):
        print(f"  {name:<10} {value:<18} {desc}")
    lost = [r for r in rows if r["score"] < 1]
    print(f"\n[감점 문항] {len(lost)}개\n")
    for r in lost:
        print(f"  {r['id']}  {r['score']:g}점  {r['question']}\n        → {r['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="시연용: 답변 → 정답 대조 → 전체 지표")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ask", help="질문 답변 (HCX 호출)")
    p.add_argument("targets", nargs="+", help="시트 문항 id(s12) 또는 질문 문장")
    p = sub.add_parser("check", help="답변 + 기존 채점 + pandas 채점")
    p.add_argument("targets", nargs="+", help="시트 문항 id")
    p.add_argument("--saved", action="store_true", help="HCX 대신 7회차 기록 답변으로 채점")
    sub.add_parser("score", help="7회차 전체 지표")
    args = parser.parse_args()

    if args.cmd == "ask":
        asyncio.run(ask(args.targets))
    elif args.cmd == "check":
        asyncio.run(check(args.targets, args.saved))
    else:
        score()


if __name__ == "__main__":
    main()
