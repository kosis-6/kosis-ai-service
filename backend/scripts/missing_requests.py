"""DB에 없는 데이터 요청 집계: 다음 회차에 어떤 데이터를 추가할지 정할 때 본다

/ask가 답하지 못한 질문 중 데이터가 없어서 거절한 것(qa.DATA_GAP)이 data/requests/missing.jsonl에
쌓인다. 사유·지표·지역·연도가 같은 요청끼리 묶어 많이 나온 순서로 보여준다.

실행: python -m scripts.missing_requests
      python -m scripts.missing_requests --since 2026-10-26   (이 날짜 이후만)
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from app.services.qa import MISSING_LOG

# 사유별로 무엇이 없어서 거절했는지
REASONS = {
    "unknown_indicator": "지원하지 않는 지표",
    "out_of_range": "수록 기간 밖 연도",
    "region_not_in_year": "그 연도에 없던 지역",
    "missing_data": "값이 비어 있음",
}
# 지원하지 않는 지표의 구분: 수집한 표에 관련 자료가 있는지 (qa.compute가 기록)
GAPS = {"not_implemented": "미구현 (수집한 표에 있음)", "not_collected": "수집 안 함"}


def load(path: Path = MISSING_LOG, since: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in records if not since or r["at"] >= since]


def key(record: dict) -> tuple[str, str, str, str, str]:
    p = record.get("parsed") or {}
    years = p.get("year") or "~".join(str(y) for y in (p.get("start_year"), p.get("end_year")) if y)
    return (
        record["status"],
        GAPS.get(record.get("gap"), ""),
        p.get("indicator") or "",
        ", ".join(p.get("regions") or []),
        str(years or ""),
    )


def summarize(records: list[dict]) -> str:
    counts = Counter(key(r) for r in records)
    examples = {}
    for r in records:
        examples.setdefault(key(r), r["question"])
    lines = [
        f"요청 {len(records)}건, 묶음 {len(counts)}개",
        "",
        "| 건수 | 사유 | 구분 | 지표 | 지역 | 연도 | 예시 질문 |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, n in counts.most_common():
        status, gap, ind, regions, years = k
        reason = REASONS.get(status, status)
        lines.append(f"| {n} | {reason} | {gap} | {ind} | {regions} | {years} | {examples[k]} |")
    gaps = Counter(r.get("gap") for r in records if r.get("gap"))
    if gaps:
        lines.append(
            "\n지원하지 않는 지표: "
            + ", ".join(f"{GAPS[g]} {n}건" for g, n in gaps.items() if g in GAPS)
            + " (미구현은 코드·지표 사전 작업, 수집 안 함은 다음 회차 수집 후보)"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="DB에 없는 데이터 요청 집계")
    parser.add_argument("--since", help="이 시각(YYYY-MM-DD) 이후 요청만")
    args = parser.parse_args()
    records = load(since=args.since)
    if not records:
        print(f"기록된 요청이 없습니다 ({MISSING_LOG})")
        return
    print(summarize(records))


if __name__ == "__main__":
    main()
