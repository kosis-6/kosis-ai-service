"""골든셋 재생 검증: 지난 채점에서 기록한 HCX 해석을 지금 코드에 다시 넣어 결과를 확인한다

HCX를 부르지 않으므로 1분 안에 끝난다. 질문 해석 보정(qa.correct)·계산(qa.compute)을 고친 뒤,
지금까지 HCX가 실제로 낸 여러 가지 해석(실행마다 달라짐)을 모두 견디는지 회귀 확인용으로 쓴다.
채점 결과 CSV의 hcx_raw 칸(scripts/eval_golden.py가 남김)이 있는 행만 재생한다.

  답변 불가 판단·기대 행동, 질의 이해, 데이터 정확성을 eval_golden.py와 같은 규칙으로 채점한다.
  (표 선택·답변 신뢰성은 문장 생성까지 같은 코드라 따로 보지 않는다)

실행: python -m scripts.eval_replay                       (data/eval의 모든 채점 결과)
      python -m scripts.eval_replay data/eval/golden_r5_*.csv
"""

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
    """기록된 HCX 출력 → qa.ask와 같은 모양의 결과 (HCX 출력에 JSON이 없으면 None)"""
    objects = qa._json_objects(hcx_raw)
    if not objects:
        return None
    parsed = objects[0]
    if len({str(o.get("sex")) for o in objects}) > 1:  # qa.understand와 같은 처리
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
    """틀린 내용 목록 (비어 있으면 통과)"""
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
