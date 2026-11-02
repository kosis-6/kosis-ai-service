"""검색어 재작성 실험: 질문 세트를 search.search()에 넣고 정답 표 순위·최종 정답 여부를 기록한다.

python eval/rewrite_check/run_eval.py eval/rewrite_check/team41.jsonl eval/rewrite_check/devfail32.jsonl --label base --out base.jsonl
SEARCH_REWRITER=hcx007 python eval/rewrite_check/run_eval.py ... --old-slot-prompt --label v2 --out v2.jsonl
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import embedding, search  # noqa: E402
from app.services import slots as slot_parser  # noqa: E402

if "--old-slot-prompt" in sys.argv:  # 슬롯 프롬프트만 원래 것으로 (재작성 효과만 보려고)
    sys.argv.remove("--old-slot-prompt")
    ns = {}
    exec(Path(__file__).with_name("search_prompt_old.py").read_text(), ns)
    slot_parser.SLOT_SYSTEM = ns["SLOT_SYSTEM"]

_orig_embed = embedding.embed_once
stats = {"embed_retry": 0, "embed_fail": 0}


async def embed_retry(text, model=embedding.DEFAULT_MODEL, timeout=3.0):
    """측정이 호출 제한에 흔들리지 않게 실패하면 기다렸다 다시 부른다"""
    for i in range(5):
        try:
            return await _orig_embed(text, model, max(timeout, 10.0))
        except Exception:
            stats["embed_retry"] += 1
            await asyncio.sleep(15 * (i + 1))
    stats["embed_fail"] += 1
    raise RuntimeError("embedding failed after retries")


embedding.embed_once = embed_retry


def score(item, r, fam_of):
    gold = set(item["gold_tables"])
    gold_fams = {fam_of[t] for t in gold if t in fam_of}
    cands = [c["tbl_id"] for c in r.get("candidates") or []]
    rank = next((i + 1 for i, t in enumerate(cands) if t in gold), None)
    status = r.get("status")
    final = None
    if status in ("ok", "partial_match"):
        final = r["table"]["tbl_id"]
        correct = item["expect"] != "out_of_scope" and final in gold
    elif status == "clarify":
        final = "clarify:" + r["family"]["family_id"]
        correct = r["family"]["family_id"] in gold_fams
    elif status == "ambiguous":
        top = r["options"][0]
        final = "ambiguous:" + top["tbl_id"]
        correct = top["family_id"] in gold_fams
    else:
        correct = item["expect"] == "out_of_scope"
    return {"rank": rank, "final": final, "correct": bool(correct)}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    items = [json.loads(l) for f in a.files for l in Path(f).read_text().splitlines() if l.strip()]
    if a.only:
        items = [i for i in items if i["id"] in a.only]
    tables = await search.load_tables()
    fam_of = {t["tbl_id"]: t["family_id"] for t in tables}
    missing = {g for i in items for g in i["gold_tables"] if g not in fam_of}
    if missing:
        print("카탈로그에 없는 정답 표:", sorted(missing), file=sys.stderr)
    out = open(a.out, "w")
    n_ok = n_hit = 0
    for k, item in enumerate(items, 1):
        trace = {}
        t0 = time.perf_counter()
        r = await search.search(item["q"], trace)
        s = score(item, r, fam_of)
        n_ok += s["correct"]
        n_hit += s["rank"] is not None and s["rank"] <= 8
        row = {
            "label": a.label, "id": item["id"], "q": item["q"], "expect": item["expect"],
            "status": r.get("status"), **s,
            "search_query": r.get("slots", {}).get("search_query"),
            "search_queries": r.get("slots", {}).get("search_queries"),
            "parse_error": r.get("slots", {}).get("parse_error"),
            "retrieval": r.get("retrieval"), "embedding": trace.get("embedding"),
            "llm_pick": (trace.get("llm_pick") or {}).get("pick"),
            "sec": round(time.perf_counter() - t0, 1),
        }  # fmt: skip
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.flush()
        print(f"[{k}/{len(items)}] {item['id']} {'O' if s['correct'] else 'X'} rank={s['rank']} "
              f"{r.get('status')} {s['final']}", flush=True)  # fmt: skip
    print(f"\n{a.label} (rewriter={search.vector_search.get_settings().search_rewriter}): 정답 {n_ok}/{len(items)} · 후보 8위 안 {n_hit}/{len(items)} · {stats}")


asyncio.run(main())
