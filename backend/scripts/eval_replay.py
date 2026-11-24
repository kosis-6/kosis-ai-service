import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Any

from app.services import qa
from scripts import eval_golden as ev

GOLDENS = [Path("eval/golden_sheet.csv"), Path("eval/golden.csv")]
RESULTS = Path("data/eval")


def load_cases() -> dict[str, dict[str, str]]:
    return {c["id"]: c for path in GOLDENS if path.exists() for c in ev.load_golden(path)}


def replay(question: str, hcx_raw: str) -> dict[str, Any] | None:
    objects = qa._json_objects(hcx_raw)
    if not objects:
        return None
    parsed = objects[0]
    if len({str(o.get("sex")) for o in objects}) > 1:
        parsed["sex"] = "계"
    parsed = qa.correct(question, parsed)
    result = qa.compute(parsed)
    return {
        "status": result.get("status"),
        "parsed": parsed,
        "result": result,
        "sources": qa.sources(result),
        "answer": f"{qa.summary(result)}\n\n{qa.render(result)}".strip(),
    }


def check(case: dict[str, str], res: dict[str, Any]) -> list[str]:
    problems = []
    refuse_ok, reason_ok = ev.grade_refusal(case, res["status"], res)
    if not (refuse_ok and reason_ok):
        want = case.get("allowed_action") or case.get("expected_status") or "ok"
        problems.append(f"행동 {ev.action_of(res)} / status {res['status']} (정답 {want})")
    parse_ok, wrong = ev.grade_parse(case, res["parsed"])
    if parse_ok is False:
        problems.append("질의 이해: " + "; ".join(wrong))
    value_ok, got = ev.grade_value(case, res["result"])
    if value_ok is False:
        problems.append(f"데이터 정확성: {got} (정답 {case.get('expected')})")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="기록된 HCX 해석으로 지금 코드 재생 검증")
    parser.add_argument("csv", nargs="*", type=Path, help="채점 결과 CSV (없으면 data/eval 전체)")
    args = parser.parse_args()
    paths = args.csv or sorted(RESULTS.glob("golden*_*.csv"))
    cases = load_cases()

    counts, failed = Counter(), []
    for path in paths:
        with path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                case = cases.get(row["id"])
                res = replay(row["question"], row.get("hcx_raw", "")) if case else None
                if res is None:
                    continue
                counts["재생"] += 1
                if problems := check(case, res):
                    failed.append((path.name, row["id"], row["question"], problems))

    print(f"재생한 HCX 해석 {counts['재생']}개 (결과 파일 {len(paths)}개), 틀림 {len(failed)}개")
    for name, qid, question, problems in failed:
        print(f"- {name} {qid} {question}\n    " + "\n    ".join(problems))


if __name__ == "__main__":
    main()
