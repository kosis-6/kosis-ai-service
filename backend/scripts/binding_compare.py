"""조건 → 칸 잇기 1단계 비교: 지금 계산이 고른 칸과 잇기가 고른 칸을 견준다

평가를 BINDING_SHADOW=true로 돌리면 바로 계산이 표마다 처리 기록(trace["dynamic"])의 그 표 칸에
"binding"(잇기 결과 + 지금 계산과의 비교)을 남긴다 (app/services/binding.py). 이 스크립트는
평가 원응답에서 문항마다 대표 표 하나를 골라 네 가지로 나눈다:

  같음 · 다름        지금 계산도 잇기도 성공. 다름이면 잇기 규칙을 확인한다
  잇기만 성공         지금은 표 안내로 끝나지만 잇기는 모든 조건을 이었다 → 2단계에서 고쳐질 문항
  지금 계산만 성공    잇기가 못 이은 조건이 있다 → 규칙이 빠졌다 (2단계 전에 채울 것)
  둘 다 실패          잇지 못한 조건과 이유 → 2단계에서 LLM을 붙일 대상

대표 표: 답을 낸 표(status ok) → 검토가 고른 표(kind picked) → 첫 시도.
같은 표를 여러 번 계산했으면(칸 고르기 LLM 뒤 다시 계산) 마지막 기록을 쓴다.

실행 (backend 에서):
  BINDING_SHADOW=true python -m scripts.eval_devset --golden eval/devset_v2.2.csv …
  python -m scripts.binding_compare data/eval/devset_….raw.jsonl [--out 결과.md]
"""

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts import refusal_reasons as rr

ORDER = [
    "같음",
    "다름",
    "잇기만 성공",
    "칸 같음 · 계산 검사에서 막힘",
    "지금 계산만 성공",
    "둘 다 실패",
    "견주지 않음 (비율 · 구성비)",
]


def representative(turn: dict[str, Any]) -> dict[str, Any] | None:
    """문항의 대표 표 기록 (binding이 있는 것만)"""
    steps = [s for s in (turn.get("trace") or {}).get("dynamic") or [] if s.get("binding")]
    if not steps:
        return None
    for want in (lambda s: s.get("status") == "ok", lambda s: s.get("kind") == "picked"):
        hit = [s for s in steps if want(s)]
        if hit:
            return hit[-1]
    return steps[0]


def collect(path: Path) -> dict[str, list[tuple[str, dict]]]:
    """분류 → [(문항, 대표 표 기록)]"""
    out: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, turn in rr.turns(path):
        step = representative(turn)
        if step is None:
            continue
        b = step["binding"]
        kind = b.get("vs_current") or ("잇기 오류" if b.get("error") else "?")
        out[kind].append((key, step))
    return out


def _bound_text(b: dict) -> str:
    names = b.get("names") or []
    cells = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
    return f"{b['cond']} '{b['value']}' → {b['by']}" + (f" ({cells})" if cells else "")


def report(by: dict[str, list[tuple[str, dict]]], name: str) -> list[str]:
    total = sum(len(v) for v in by.values())
    lines = [f"# 잇기 1단계 비교 ({name})", "", f"잇기 기록이 있는 문항: {total}건", ""]
    lines += ["| 분류 | 문항 수 | 문항 |", "|---|---|---|"]
    for kind in ORDER + sorted(set(by) - set(ORDER)):
        if kind in by:
            ids = " ".join(k for k, _ in by[kind])
            lines.append(f"| {kind} | {len(by[kind])} | {ids} |")
    if by.get("다름"):
        lines += ["", "## 다름: 고른 칸이 다른 분류", ""]
        lines += ["| 문항 | 표 | 분류 | 지금 계산 | 잇기 |", "|---|---|---|---|---|"]
        for key, step in by["다름"]:
            for d in step["binding"].get("diff") or []:
                cur, mine = ", ".join(d["current"]), ", ".join(d["binding"])
                lines.append(f"| {key} | {step['tbl_id']} | {d['dim']} | {cur} | {mine} |")
    for kind in ("지금 계산만 성공", "둘 다 실패"):
        if by.get(kind):
            lines += ["", f"## {kind}: 잇지 못한 조건", ""]
            lines += ["| 문항 | 표 | 조건 | 이유 |", "|---|---|---|---|"]
            for key, step in by[kind]:
                for u in step["binding"].get("unbound") or []:
                    val = f"{u['cond']} '{u['value']}'" if u["value"] else u["cond"]
                    lines.append(f"| {key} | {step['tbl_id']} | {val} | {u['why']} |")
    if by.get("잇기만 성공"):
        lines += ["", "## 잇기만 성공: 이은 조건", ""]
        lines += ["| 문항 | 표 | 지금 계산이 멈춘 이유 | 잇기 |", "|---|---|---|---|"]
        for key, step in by["잇기만 성공"]:
            got = "<br>".join(_bound_text(b) for b in step["binding"].get("bound") or [])
            lines.append(f"| {key} | {step['tbl_id']} | {step.get('skip', '')} | {got} |")
    return lines


def binding_used(path: Path) -> dict[str, list[str]]:
    """2단계(BINDING_USE): 잇기로 다시 계산한 문항 → 결과별. 처리 흐름(flow)의 '표 칸으로 계산'에
    '(잇기)'가 붙은 단계를 본다 (한 분류 안의 연산으로 답하면 trace.dynamic이 응답에 남지 않아
    흐름으로 센다)"""
    out: dict[str, list[str]] = defaultdict(list)
    for key, turn in rr.turns(path):
        for f in (turn.get("trace") or {}).get("flow") or []:
            if f.get("stage") == "표 칸으로 계산" and "(잇기)" in (f.get("detail") or ""):
                out[str(f.get("result"))].append(key)
                break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="잇기 1단계: 지금 계산과 잇기 견주기")
    parser.add_argument(
        "path", type=Path, help="평가 원응답(.raw.jsonl, BINDING_SHADOW=true로 돌린 것)"
    )
    parser.add_argument("--out", type=Path, help="결과를 이 파일(.md)에도 쓴다")
    args = parser.parse_args()
    lines = report(collect(args.path), args.path.name)
    if used := binding_used(args.path):
        lines += ["", "## 잇기로 다시 계산한 문항 (BINDING_USE)", "", "| 결과 | 문항 수 | 문항 |"]
        lines += ["|---|---|---|"]
        lines += [f"| {k} | {len(v)} | {' '.join(v)} |" for k, v in sorted(used.items())]
    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
