"""수집 결과 표: scripts/tables.csv의 통계표별로 수록 기간, 행 수, 수집 시각을 정리한다

값은 collect_kosis.py가 남긴 data/processed/<표ID>.meta.json에서 읽는다.
meta.json이 없는 표는 '미수집'으로 표시한다.

결과: data/reports/collect_YYYYMMDD_HHMMSS.md (보고서 붙여넣기용) / .csv (엑셀용)
실행: python -m scripts.collect_report
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

TABLES = Path("scripts/tables.csv")
META_DIR = Path("data/processed")
OUT_DIR = Path("data/reports")
COLUMNS = ["topic", "org_id", "tbl_id", "tbl_nm", "period", "rows", "collected_at", "status"]
HEADERS = ["주제", "기관", "통계표 ID", "표 이름", "수록 기간", "행 수", "수집 시각", "상태"]


def summarize(tables: Path = TABLES, meta_dir: Path = META_DIR) -> list[dict[str, Any]]:
    with tables.open(encoding="utf-8") as f:
        targets = list(csv.DictReader(f))
    result = []
    for t in targets:
        row = {
            "topic": t["topic"],
            "org_id": t["org_id"],
            "tbl_id": t["tbl_id"],
            "tbl_nm": t["tbl_nm"],
            "period": "",
            "rows": "",
            "collected_at": "",
            "status": "미수집",
        }
        meta_path = meta_dir / f"{t['tbl_id']}.meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            row |= {
                # 표 이름은 KOSIS가 준 정식 이름을 우선
                "tbl_nm": meta.get("tbl_nm") or t["tbl_nm"],
                "period": f"{meta.get('start', '')}~{meta.get('end', '')}",
                "rows": meta.get("rows", ""),
                # 이전 버전 수집기로 받은 표는 collected_at이 없음
                "collected_at": meta.get("collected_at", "기록 없음"),
                "status": "수집" if meta.get("rows") else "0행",
            }
        result.append(row)
    return result


def to_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["| " + " | ".join(HEADERS) + " |", "|" + " --- |" * len(HEADERS)]
    for r in rows:
        cells = [f"{r[c]:,}" if c == "rows" and r[c] != "" else str(r[c]) for c in COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    done = [r for r in rows if r["status"] == "수집"]
    total_rows = sum(r["rows"] for r in done)
    lines += [
        "",
        f"- 대상 {len(rows)}개 중 수집 {len(done)}개, 총 {total_rows:,}행",
    ]
    missing = [r["tbl_id"] for r in rows if r["status"] != "수집"]
    if missing:
        lines.append(f"- 수집 안 됨: {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="수집 결과 표 만들기")
    parser.add_argument("--tables", type=Path, default=TABLES)
    parser.add_argument("--meta-dir", type=Path, default=META_DIR)
    args = parser.parse_args()

    rows = summarize(args.tables, args.meta_dir)
    md = to_markdown(rows)
    print(md)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"collect_{stamp}.md").write_text(md, encoding="utf-8")
    with (OUT_DIR / f"collect_{stamp}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerow(dict(zip(COLUMNS, HEADERS, strict=True)))
        writer.writerows(rows)
    print(f"저장: {OUT_DIR}/collect_{stamp}.md, .csv")


if __name__ == "__main__":
    main()
