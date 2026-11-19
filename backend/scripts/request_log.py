"""요청 로그 보기: 틀린 답이 질문 해석(HCX·보정) 문제인지 조회·계산 문제인지 추적한다

/ask 요청마다 data/requests/requests.jsonl에 한 줄씩 쌓인다 (qa.log_request).
  hcx_raw   HCX 원문         → JSON을 못 읽었거나(parse_error) 해석 자체가 틀렸는지
  parsed    해석 + 보정 내역 → 코드 보정(corrections)이 맞게 고쳤는지
  result    조회·계산 결과   → 해석은 맞는데 값이나 status가 틀렸는지

실행: python -m scripts.request_log                       (상태별 요약 + 최근 10건)
      python -m scripts.request_log --status parse_error  (이 status만)
      python -m scripts.request_log --last 3 --detail     (HCX 원문·해석·계산 결과까지)
      python -m scripts.request_log --flow --find 출생아     (질문이 거친 단계와 순서)
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from app.services import flow
from app.services.qa import REQUEST_LOG


def load(path: Path = REQUEST_LOG) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(records: list[dict]) -> str:
    """status별 건수와 평균 응답 시간"""
    counts = Counter(r["status"] or "실패" for r in records)
    lines = [f"요청 {len(records)}건", "", "| status | 건수 | 평균 응답(ms) |", "|---|---|---|"]
    for status, n in counts.most_common():
        times = [r["elapsed_ms"] for r in records if (r["status"] or "실패") == status]
        times = [t for t in times if t is not None]
        avg = round(sum(times) / len(times)) if times else "-"
        lines.append(f"| {status} | {n} | {avg} |")
    return "\n".join(lines)


def show(record: dict, detail: bool = False, steps: bool = False) -> str:
    parsed = record.get("parsed") or {}
    head = f"[{record['at']}] {record['status'] or '실패'} {record.get('elapsed_ms')}ms"
    lines = [head, f"  질문: {record['question']}"]
    if record.get("error"):
        lines.append(f"  오류: {record['error']}")
    for fix in parsed.get("corrections") or []:
        lines.append(f"  보정: {fix}")
    if steps:
        body = flow.text(record.get("flow")) or "(흐름 기록 없음: 기록을 남기기 전의 요청)"
        lines += ["  처리 흐름:", *(f"    {line}" for line in body.splitlines())]
    if detail:
        result = record.get("result") or {}
        parsed_text = json.dumps(parsed, ensure_ascii=False)
        result_text = json.dumps(result, ensure_ascii=False, default=str)
        lines += [
            f"  HCX 원문: {record.get('hcx_raw')}",
            f"  해석: {parsed_text}",
            f"  계산: {result_text[:500]}",
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="요청 로그 보기")
    parser.add_argument("--status", help="이 status만 (실패한 요청은 '실패')")
    parser.add_argument("--last", type=int, default=10, help="최근 몇 건 (기본 10)")
    parser.add_argument("--detail", action="store_true", help="HCX 원문·해석·계산 결과까지")
    parser.add_argument("--flow", action="store_true", help="질문이 거친 단계와 순서")
    parser.add_argument("--find", help="질문에 이 말이 든 요청만")
    args = parser.parse_args()

    records = load()
    if not records:
        print(f"기록된 요청이 없습니다 ({REQUEST_LOG})")
        return
    print(summarize(records) + "\n")
    if args.status:
        records = [r for r in records if (r["status"] or "실패") == args.status]
    if args.find:
        records = [r for r in records if args.find in (r.get("question") or "")]
    for r in records[-args.last :]:
        print(show(r, args.detail, args.flow) + "\n")


if __name__ == "__main__":
    main()
