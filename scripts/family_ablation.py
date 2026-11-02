"""패밀리 켬/끔 비교: 개발셋 질문마다 슬롯·임베딩을 한 번 뽑아 두 방식의 find()에 똑같이 넣는다

  켬  지금 검색 그대로 (같은 패밀리 접기 → 멤버·판 선택 → 되묻기)
  끔  표마다 자기 혼자인 패밀리로 바꾼 표 목록을 넣는다 → 접기·멤버 선택·되묻기가 일어나지 않고
      재순위 1등 표가 그대로 답이 된다

슬롯(HCX)·임베딩이 같으므로 차이는 패밀리 단계에서만 생긴다. R@k 차이는 점수가 같을 때 켬이 최신 판을 앞에 두는 데서만 생긴다.
흐름은 score.py(골든셋 README)와 같은 규칙:
  - ambiguous → 사용자가 1번 선택지를 골랐다고 보고 그 패밀리로 멤버·판 선택
  - clarify   → 골든셋 answers 로 한 번 답한 뒤 최종 표 (answers 가 없으면 기본 멤버)

지표 (범위 안 문항)
  흐름 성공   final 이 gold_tables 안에 있음
  엄격 정답   final_table 이 있는 문항만, final == final_table (허용 정답이 넓어 생기는 이득을 뺌)
  겹침        끔일 때 상위 5개 표 안에 같은 패밀리(켬 기준) 표가 겹친 문항 수

결과: eval/results/family_ablation_YYYYMMDD.md · .csv(켬/끔 결과가 다른 문항)
      eval/results/pred_family_on.jsonl · pred_family_off.jsonl (score.py 에 그대로 넣을 수 있음)
실행
  python -m scripts.family_ablation                         # HCX 슬롯 + 임베딩
  python -m scripts.family_ablation --sleep 2               # 호출 제한(42901)에 걸릴 때
  python -m scripts.family_ablation --rule-slots            # HCX 없이 규칙 슬롯 (빠른 점검용)
  python -m scripts.family_ablation --llm-pick              # LLM 검토(HCX-007)까지 켬/끔 각각
슬롯은 eval/results/family_ablation_slots.jsonl 에 저장해 다시 돌릴 때 HCX 를 부르지 않는다
(--fresh 로 무시).
"""

import argparse
import asyncio
import copy
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from app.services import search
from app.services import slots as slot_parser

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "dev_20261102" / "dev_all.jsonl"
RESULTS = ROOT / "eval" / "results"
SLOT_CACHE = RESULTS / "family_ablation_slots.jsonl"
OOS = {"out_of_scope", "not_statistics"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def family_off(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """표마다 자기 혼자인 패밀리 (순서는 그대로 → 임베딩 점수 배열과 맞음)"""
    return [
        {**t, "family_id": t["tbl_id"], "family_name": t["table_name"],
         "target": None, "sex": None, "edition_order": 1}
        for t in tables
    ]  # fmt: skip


async def get_slots(q: str, rule: bool, sleep: float) -> tuple[dict[str, Any], str]:
    if rule:
        return slot_parser.correct(q, slot_parser.empty_slots(q)), "rule"
    for attempt in range(3):
        try:
            return await slot_parser.extract(q, {}), "hcx"
        except Exception as e:  # 호출 제한 등
            print(f"    슬롯 추출 실패 ({type(e).__name__}: {e}) → {5 * (attempt + 1)}초 뒤 다시")
            await asyncio.sleep(5 * (attempt + 1) + sleep)
    return slot_parser.correct(q, slot_parser.empty_slots(q)), "rule(HCX 실패)"


async def get_dense(q: str, slots: dict[str, Any]) -> list[float] | None:
    """search.search()와 같게: 질문 임베딩 + 재작성 검색어 임베딩 평균"""
    dense = await search.dense_scores(q)
    rewrite = (slots.get("search_query") or "").strip()
    if dense is not None and rewrite and rewrite != q.strip():
        dense2 = await search.dense_scores(rewrite)
        if dense2 is not None:
            dense = [(a + b) / 2 for a, b in zip(dense, dense2, strict=True)]
    return dense


def answered(slots: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """골든셋 answers({"target": ...} / {"region": [...]})를 되묻기 답으로 넣는다"""
    s = copy.deepcopy(slots)
    if "target" in answers:
        s["target"] = answers["target"]
    if "region" in answers:
        r = answers["region"]
        s["conditions"]["region"] = " ".join(r) if isinstance(r, list) else r
    if "sex" in answers:
        s["conditions"]["sex"] = answers["sex"]
    if "edition" in answers:
        s["edition"] = answers["edition"]
    return s


async def flow(
    g: dict[str, Any], tables: list[dict[str, Any]], slots: dict[str, Any],
    dense: list[float] | None, use_llm: bool,
) -> dict[str, Any]:  # fmt: skip
    r = search.find(tables, copy.deepcopy(slots), True, dense)
    if use_llm:
        from app.services import llm_pick

        r = await llm_pick.review(g["q"], r, tables, {})
    first = r["status"]
    ranked = [c["tbl_id"] for c in r.get("candidates") or []]
    if first == "ambiguous":
        r = search.resolve(tables, copy.deepcopy(r["slots"]), r["options"][0]["family_id"], True)
    asked = r["status"] == "clarify"
    if asked:
        fam = r["family"]["family_id"]
        s = answered(r["slots"], g.get("answers") or {})
        r = search.resolve(tables, s, fam, allow_clarify=False)
    return {
        "id": g["id"],
        "ranked": ranked,
        "final": (r.get("table") or {}).get("tbl_id"),
        "status": first if first in OOS else "ok",
        "first_status": first,
        "asked": asked,
        "family": (r.get("family") or {}).get("family_id"),
    }


def metrics(gs: list[dict[str, Any]], pred: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ins = [g for g in gs if g.get("gold_tables")]
    oos = [g for g in gs if not g.get("gold_tables")]
    m: dict[str, Any] = {"n": len(ins), "oos_n": len(oos)}
    ok = [pred[g["id"]]["final"] in g["gold_tables"] for g in ins]
    m["flow"] = sum(ok)
    rank = [
        next((i for i, t in enumerate(pred[g["id"]]["ranked"]) if t in g["gold_tables"]), 99)
        for g in ins
    ]
    m["r1"] = sum(x < 1 for x in rank)
    m["r5"] = sum(x < 5 for x in rank)
    strict = [g for g in ins if g.get("final_table")]
    m["strict_n"] = len(strict)
    m["strict"] = sum(pred[g["id"]]["final"] == g["final_table"] for g in strict)
    clar = [g for g in ins if g.get("expect") == "clarify"]
    m["clar_n"] = len(clar)
    m["clar"] = sum(pred[g["id"]]["final"] in g["gold_tables"] for g in clar)
    m["asked"] = sum(pred[g["id"]]["asked"] for g in ins)
    m["oos"] = sum(pred[g["id"]]["status"] in OOS for g in oos)
    m["false_oos"] = sum(pred[g["id"]]["status"] in OOS for g in ins)
    return m


def pct(a: int, n: int) -> str:
    return f"{a / n:.0%} ({a}/{n})" if n else "-"


def report(
    golden: list[dict[str, Any]], on: dict[str, Any], off: dict[str, Any],
    fam_of: dict[str, str], name_of: dict[str, str], slot_src: dict[str, int], use_llm: bool,
) -> tuple[str, list[dict[str, Any]]]:  # fmt: skip
    lines = [
        f"# 패밀리 켬/끔 비교 ({date.today():%Y-%m-%d})",
        "",
        f"- 개발셋: {GOLDEN.relative_to(ROOT)} ({len(golden)}문항)",
        f"- 슬롯: {', '.join(f'{k} {v}' for k, v in slot_src.items())}"
        f" · LLM 검토: {'켬' if use_llm else '끔'}",
        "- 슬롯·임베딩은 두 방식이 같다 → 차이는 패밀리 단계(접기·멤버·판 선택, 동점일 때 최신 판 우선)에서만 생긴다",
        "",
    ]

    def block(title: str, gs: list[dict[str, Any]]) -> None:
        a, b = metrics(gs, off), metrics(gs, on)
        if not a["n"] and not a["oos_n"]:
            return
        lines.extend([f"## {title}", "", "| 지표 | 패밀리 끔 | 패밀리 켬 | 차이 |", "| --- | --- | --- | --- |"])
        rows = [
            ("표 R@1", "r1", "n"), ("표 R@5", "r5", "n"), ("**흐름 성공**", "flow", "n"),
            ("엄격 정답 (final_table)", "strict", "strict_n"), ("되묻기 문항 성공", "clar", "clar_n"),
        ]  # fmt: skip
        for label, k, nk in rows:
            if a[nk]:
                d = (b[k] - a[k]) / a[nk]
                lines.append(f"| {label} | {pct(a[k], a[nk])} | {pct(b[k], b[nk])} | {d:+.0%}p |")
        lines.append(f"| 되물은 문항 수 | {a['asked']} | {b['asked']} | |")
        if a["oos_n"]:
            lines.append(f"| 범위 밖 재현율 | {pct(a['oos'], a['oos_n'])} | {pct(b['oos'], b['oos_n'])} | |")
        lines.append(f"| 범위 안→밖 오판 | {a['false_oos']} | {b['false_oos']} | |")
        lines.append("")

    block("전체", golden)
    for key, label in (("set", "셋"), ("fam_size", "패밀리 크기"), ("type", "질의 유형")):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for g in golden:
            if g.get("gold_tables"):
                groups[str(g.get(key) or "없음")].append(g)
        lines.extend([f"## {label}별 흐름 성공", "", f"| {label} | 문항 | 끔 | 켬 | 차이 |", "| --- | --- | --- | --- | --- |"])
        for k in sorted(groups):
            a, b = metrics(groups[k], off), metrics(groups[k], on)
            lines.append(
                f"| {k} | {a['n']} | {pct(a['flow'], a['n'])} | {pct(b['flow'], b['n'])} | {b['flow'] - a['flow']:+d} |"
            )
        lines.append("")

    # 끔일 때 상위 5개 안의 같은 계열 겹침 (켬은 접기 때문에 겹치지 않는다)
    dup = [
        5 - len({fam_of.get(t, t) for t in off[g["id"]]["ranked"][:5]})
        for g in golden if g.get("gold_tables") and len(off[g["id"]]["ranked"]) >= 5
    ]  # fmt: skip
    lines.extend([
        "## 후보 다양성",
        "",
        f"- 패밀리 끔: 상위 5개 표 안에 같은 패밀리 표가 겹친 문항 {sum(d > 0 for d in dup)}/{len(dup)}"
        f" (평균 {sum(dup) / max(len(dup), 1):.2f}자리가 같은 계열 중복)",
        "- 패밀리 켬: 같은 패밀리를 한 자리로 접으므로 상위 5자리가 모두 서로 다른 계열",
        "",
    ])  # fmt: skip

    changed = []
    for g in golden:
        if not g.get("gold_tables"):
            continue
        a, b = off[g["id"]], on[g["id"]]
        a_ok, b_ok = a["final"] in g["gold_tables"], b["final"] in g["gold_tables"]
        if a_ok != b_ok:
            changed.append({
                "결과": "켬이 맞힘" if b_ok else "켬이 틀림(손해)",
                "id": g["id"], "셋": g.get("set"), "유형": g.get("type"), "패밀리 크기": g.get("fam_size"),
                "질문": g["q"],
                "정답": " | ".join(f"{t} {name_of.get(t, '')}" for t in g["gold_tables"]),
                "끔 최종": f"{a['final']} {name_of.get(a['final'], '')}",
                "켬 최종": f"{b['final']} {name_of.get(b['final'], '')}",
                "켬 패밀리": b["family"], "켬 첫 상태": b["first_status"], "켬 되물음": b["asked"],
            })  # fmt: skip
    gain = sum(c["결과"] == "켬이 맞힘" for c in changed)
    lines.extend([
        "## 켬/끔 결과가 다른 문항",
        "",
        f"- 켬이 맞히고 끔이 틀림: {gain}",
        f"- **끔이 맞히고 켬이 틀림 (패밀리 때문에 손해): {len(changed) - gain}**",
        "",
        "| 결과 | id | 유형 | 질문 | 끔 최종 | 켬 최종 |",
        "| --- | --- | --- | --- | --- | --- |",
    ])  # fmt: skip
    for c in sorted(changed, key=lambda c: (c["결과"] != "켬이 틀림(손해)", c["id"])):
        lines.append(f"| {c['결과']} | {c['id']} | {c['유형']} | {c['질문']} | {c['끔 최종']} | {c['켬 최종']} |")
    return "\n".join(lines) + "\n", changed


async def main_async(args: argparse.Namespace) -> None:
    golden = read_jsonl(args.golden)
    RESULTS.mkdir(parents=True, exist_ok=True)
    cache = {} if args.fresh or args.rule_slots else {r["id"]: r for r in read_jsonl(SLOT_CACHE)}
    tables_on = await search.load_tables()
    tables_off = family_off(tables_on)
    fam_of = {t["tbl_id"]: t["family_id"] for t in tables_on}
    name_of = {t["tbl_id"]: t["table_name"] for t in tables_on}

    on: dict[str, Any] = {}
    off: dict[str, Any] = {}
    slot_src: dict[str, int] = defaultdict(int)
    for i, g in enumerate(golden, 1):
        if g["id"] in cache:
            slots, src = cache[g["id"]]["slots"], "캐시"
        else:
            slots, src = await get_slots(g["q"], args.rule_slots, args.sleep)
            if src == "hcx":
                with open(SLOT_CACHE, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"id": g["id"], "slots": slots}, ensure_ascii=False) + "\n")
            if args.sleep:
                await asyncio.sleep(args.sleep)
        slot_src[src] += 1
        dense = await get_dense(g["q"], slots)
        off[g["id"]] = await flow(g, tables_off, slots, dense, args.llm_pick)
        on[g["id"]] = await flow(g, tables_on, slots, dense, args.llm_pick)
        mark = lambda p: "O" if p["final"] in g["gold_tables"] else ("-" if not g["gold_tables"] else "X")  # noqa: E731
        print(f"[{i:3}/{len(golden)}] {g['id']:<6} 끔 {mark(off[g['id']])} 켬 {mark(on[g['id']])}  {g['q'][:40]}")

    for name, pred in (("off", off), ("on", on)):
        with open(RESULTS / f"pred_family_{name}.jsonl", "w", encoding="utf-8") as f:
            for p in pred.values():
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    text, changed = report(golden, on, off, fam_of, name_of, dict(slot_src), args.llm_pick)
    stem = RESULTS / f"family_ablation_{date.today():%Y%m%d}"
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    with open(stem.with_suffix(".csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(changed[0]) if changed else ["id"])
        w.writeheader()
        w.writerows(changed)
    print("\n" + text.split("## 패밀리 크기별")[0])
    print(f"→ {stem.with_suffix('.md')} / {stem.with_suffix('.csv')}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--golden", type=Path, default=GOLDEN)
    p.add_argument("--rule-slots", action="store_true", help="HCX 없이 규칙 슬롯")
    p.add_argument("--llm-pick", action="store_true", help="LLM 검토(HCX-007)까지 켬/끔 각각")
    p.add_argument("--sleep", type=float, default=0.0, help="질문 사이 쉬는 초")
    p.add_argument("--fresh", action="store_true", help="저장된 슬롯을 쓰지 않음")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
