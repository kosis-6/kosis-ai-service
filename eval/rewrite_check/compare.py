"""python compare.py base.jsonl v2.jsonl [v1.jsonl]: 세트별 정답·재현율, 문항별 변화"""

import json
import sys
from pathlib import Path

runs = {Path(f).stem: {r["id"]: r for r in map(json.loads, Path(f).read_text().splitlines())}
        for f in sys.argv[1:]}  # fmt: skip
names = list(runs)
base = runs[names[0]]
ids = list(base)


def summary(rows):
    n = len(rows)
    ok = sum(r["correct"] for r in rows)
    hit = sum(r["rank"] is not None and r["rank"] <= 8 for r in rows if r["expect"] != "out_of_scope")
    inscope = sum(r["expect"] != "out_of_scope" for r in rows)
    return f"정답 {ok}/{n} · 후보 8위 안 {hit}/{inscope}"


for name, rows in runs.items():
    team = [rows[i] for i in ids if i.startswith("j") and i in rows]
    fail = [rows[i] for i in ids if i.startswith("x-") and i in rows]
    emb = sum(1 for r in rows.values() if not (r.get("embedding") or {}).get("question"))
    pe = sum(1 for r in rows.values() if r.get("parse_error"))
    print(f"{name:10} 팀원41: {summary(team):28} | 개발오답32: {summary(fail):28} | 임베딩실패 {emb} 슬롯실패 {pe}")

for name in names[1:]:
    print(f"\n== {names[0]} → {name}")
    for i in ids:
        a, b = base[i], runs[name].get(i)
        if b is None:
            continue
        if a["correct"] != b["correct"] or a["rank"] != b["rank"]:
            mark = "얻음" if b["correct"] and not a["correct"] else "잃음" if a["correct"] and not b["correct"] else "  "
            print(f"{mark} {i:7} rank {a['rank']}→{b['rank']}  {a['final']} → {b['final']}  "
                  f"| {b['q'][:30]} | {b.get('search_queries') or b.get('search_query')}")
