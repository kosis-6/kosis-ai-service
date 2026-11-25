import argparse
import csv
import json
from pathlib import Path

ORDER = ["넘김 · 지금 오답", "넘김 · 지금 정답", "그대로 · 지금 오답", "그대로 · 지금 정답"]


def records(raw: Path) -> dict[str, tuple[str, dict]]:
    out = {}
    for line in raw.open(encoding="utf-8"):
        r = json.loads(line)
        for turn in (r.get("final"), r.get("first")):
            rec = ((turn or {}).get("trace") or {}).get("dict_binding")
            if rec:
                out[r["id"]] = (turn.get("question") or "", rec)
                break
    return out


def grades(path: Path) -> dict[str, dict]:
    return {r["문항"]: r for r in csv.DictReader(path.open(encoding="utf-8-sig"))}


def classify(recs: dict, rows: dict) -> dict[str, list]:
    by: dict[str, list] = {k: [] for k in ORDER}
    for key, (question, rec) in recs.items():
        if rec.get("error") or key not in rows:
            continue
        ok = rows[key]["정답"] == "O"
        kind = f"{rec['decision']} · 지금 {'정답' if ok else '오답'}"
        by[kind].append((key, question, rec, rows[key]))
    return by


def report(by: dict[str, list], name: str) -> list[str]:
    total = sum(len(v) for v in by.values())
    lines = [f"# 지표 사전 잇기 기록 ({name})", "", f"지표 사전이 답한 문항: {total}건", ""]
    lines += ["| 분류 | 문항 수 | 문항 |", "|---|---|---|"]
    lines += [f"| {k} | {len(by[k])} | {' '.join(x[0] for x in by[k])} |" for k in ORDER]
    for kind in ORDER[:3]:
        if not by[kind]:
            continue
        lines += [
            "",
            f"## {kind}",
            "",
            "| 문항 | 질문 | 표 | 이유 · 틀린 이유 |",
            "|---|---|---|---|",
        ]
        for key, question, rec, row in by[kind]:
            why = "<br>".join(rec.get("reasons") or []) or row.get("틀린 이유", "")
            lines.append(f"| {key} | {question[:40]} | {rec.get('tbl_id')} | {why} |")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="B 1단계: 지표 사전 답의 잇기 기록 보기")
    parser.add_argument("raw", type=Path, nargs="+", help="평가 원응답(.raw.jsonl)")
    parser.add_argument("--csv", type=Path, nargs="+", required=True, help="같은 평가의 채점 CSV")
    parser.add_argument("--out", type=Path, help="결과를 이 파일(.md)에도 쓴다")
    args = parser.parse_args()
    recs: dict = {}
    for p in args.raw:
        recs.update(records(p))
    rows: dict = {}
    for p in args.csv:
        rows.update(grades(p))
    text = "\n".join(report(classify(recs, rows), " · ".join(p.name for p in args.raw)))
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
