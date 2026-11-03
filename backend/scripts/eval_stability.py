"""골든셋 반복 채점 비교: 같은 코드·DB로 여러 번 돌렸을 때 문항마다 결과가 같은지

HCX는 같은 질문도 실행마다 해석이 달라질 수 있다(시드 고정 없음). 회차 사이 점수 차이에
코드 수정 효과와 이 흔들림이 섞이므로, 같은 조건으로 반복한 결과를 비교해 흔들림을 따로 잰다.

  행동 일치   매번 같은 행동(답변/명확화/범위안내/대안안내)으로 답했는지
  답변 일치   매번 답변 문장 첫 줄까지 같은지 (지표·지역·기간 해석이 같으면 같다)
  기대 행동   매번 기대 행동과 맞았는지

실행: python -m scripts.eval_stability data/eval/golden_r4-1_*.csv data/eval/golden_r4-2_*.csv ...
결과: data/eval/stability_<시각>.md
"""

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

OUT_DIR = Path("data/eval")


def load(paths: list[Path]) -> dict[str, list[dict[str, str]]]:
    """문항 id → 실행별 결과 목록"""
    runs: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                runs.setdefault(row["id"], []).append(row)
    return runs


def first_line(answer: str) -> str:
    return (answer or "").split("\n")[0].strip()


def compare(runs: dict[str, list[dict[str, str]]]) -> list[dict]:
    out = []
    for qid, rows in runs.items():
        actions = Counter(r.get("action") or r["status"] for r in rows)
        answers = Counter(first_line(r["answer"]) for r in rows)
        out.append(
            {
                "id": qid,
                "question": rows[0]["question"],
                "runs": len(rows),
                "action_same": len(actions) == 1,
                "answer_same": len(answers) == 1,
                "expected_hits": sum(r["reason_ok"] == "True" for r in rows),
                "actions": actions,
                "answers": answers,
            }
        )
    return out


def summarize(items: list[dict], paths: list[Path]) -> str:
    n = len(items)
    runs = max(i["runs"] for i in items)
    action_same = sum(i["action_same"] for i in items)
    answer_same = sum(i["answer_same"] for i in items)
    always = sum(i["expected_hits"] == i["runs"] for i in items)
    never = sum(i["expected_hits"] == 0 for i in items)
    lines = [
        f"# 골든셋 반복 채점 비교 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- 비교한 결과: {', '.join(p.name for p in paths)}",
        f"- 문항 {n}개 × {runs}회",
        "",
        "| 항목 | 문항 수 | 비율 |",
        "|---|---|---|",
        f"| 매번 같은 행동 | {action_same} | {action_same / n * 100:.1f}% |",
        f"| 매번 같은 답변 (첫 줄) | {answer_same} | {answer_same / n * 100:.1f}% |",
        f"| 매번 기대 행동과 일치 | {always} | {always / n * 100:.1f}% |",
        f"| 한 번도 기대 행동과 맞지 않음 | {never} | {never / n * 100:.1f}% |",
    ]
    for r in range(runs):
        hits = sum(1 for i in items if i["expected_hits"] > r)
        lines.append(f"\n기대 행동을 {r + 1}회 이상 맞힌 문항: {hits}개")
    unstable = [i for i in items if not (i["action_same"] and i["answer_same"])]
    if unstable:
        lines += ["", "## 결과가 달라진 문항", "", "| id | 질문 | 행동 | 답변 첫 줄 (횟수) |"]
        lines.append("|---|---|---|---|")
        for i in unstable:
            acts = ", ".join(f"{a} {c}회" for a, c in i["actions"].items())
            ans = "<br>".join(f"({c}) {a[:120]}" for a, c in i["answers"].items())
            lines.append(f"| {i['id']} | {i['question']} | {acts} | {ans} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 반복 채점 비교")
    parser.add_argument("csv", nargs="+", type=Path, help="같은 골든셋을 채점한 결과 CSV 여러 개")
    args = parser.parse_args()
    report = summarize(compare(load(args.csv)), args.csv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"stability_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
