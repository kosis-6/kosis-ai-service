"""'표 찾고도 거절' 원인 집계: 표를 찾았는데 수치 대신 통계표 안내로 끝난 이유를 센다

바로 계산(app/services/dynamic_indicator.py)은 후보 표를 건너뛸 때마다 trace["dynamic"]에
이유(skip)를 남긴다. 최종 답이 통계표 안내(status = tables)인 턴에서 검토가 고른 표
(kind = picked)의 건너뛴 이유를 원인으로 본다. 고른 표가 없으면 첫 시도의 이유를 쓴다.
"질문의 말과 맞는 칸이 없음"은 칸 고르기 LLM의 판단에 따라 다시 나눈다
(LLM이 맞는 칸 없다고 봄 / LLM은 맞다고 했지만 칸을 못 정함 / LLM 오류 / 규칙에서 끝남).

입력: 평가 원응답(data/eval/*.raw.jsonl)
      또는 요청 기록(data/requests/requests.jsonl, dynamic 칸이 있는 줄)
실행: python -m scripts.refusal_reasons data/eval/devset_r1_….raw.jsonl
      python -m scripts.refusal_reasons 새.raw.jsonl --compare 예전.raw.jsonl   (회차 비교)
      python -m scripts.refusal_reasons ….raw.jsonl --golden eval/devset_v2.csv
          (답해야 하는 문항에 * 표시)
      python -m scripts.refusal_reasons data/requests/requests.jsonl --all
          (후보 표 전체의 건너뛴 이유도)
개발셋 평가(scripts.eval_devset)는 문항별 '거절 원인' 칸과 요약의 같은 표를 이 모듈로 만든다
"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

TABLES = "tables"  # 표를 찾았지만 수치 대신 통계표를 안내한 답
# 통계표 찾기가 질문의 조건을 모두 만족하는 표를 찾았다고 판정한 상태 (일부 일치는 partial_match).
# 이 판정인데 통계표 안내로 끝났으면 통계표 찾기와 계산의 기준이 어긋난 것이다
# (docs/cell_binding_design_2026-11-23.md 6장 0단계)
SEARCH_OK = "ok"

# (키, 원인, skip 문구 정규식, 막힌 단계, 고칠 곳). 위에서부터 처음 맞는 것.
# skip 문구가 바뀌면 '분류 안 됨'으로 잡힌다
CATEGORIES: list[tuple[str, str, str, str, str]] = [
    (
        "cell_none",
        "질문의 말과 맞는 칸이 없음",
        r"^질문의 말과 맞는 칸이 없음",
        "칸 고르기",
        "dynamic_indicator.choose · cell_pick",
    ),
    (
        "cell_dim",
        "분류에서 칸을 고르지 못함",
        r"에서 칸을 고르지 못함",
        "칸 고르기",
        "dynamic_indicator.choose · cell_pick",
    ),
    (
        "check",
        "통계표 찾기 조건 불충족",
        r"^통계표 찾기 조건 불충족",
        "표 조건 확인",
        "dynamic_indicator.skip_reason · search.check",
    ),
    (
        "no_sex",
        "표에 성별 구분이 없음",
        r"^표에 성별 구분이 없음",
        "표 조건 확인",
        "dynamic_indicator.choose",
    ),
    (
        "no_age",
        "표에 연령 구분이 없음",
        r"^표에 연령 구분이 없음",
        "표 조건 확인",
        "dynamic_indicator.choose",
    ),
    (
        "age_cells",
        "연령 조건을 표의 칸으로 만들지 못함",
        r"연령 조건을 표의 칸에 반영할 수 없음|연령 구간을 표의 연령 칸으로 만들 수 없음",
        "칸 고르기",
        "dynamic_indicator (연령 칸)",
    ),
    ("band", "맞는 구간 없음", r"구간 없음$", "칸 고르기", "dynamic_indicator (구간)"),
    (
        "uncovered",
        "질문 조건을 칸에 반영하지 못함",
        r"^질문의 조건 '.*'을 표의 칸에 반영할 수 없음|^질문의 대상 |^빼 달라는 대상",
        "칸 고르기",
        "dynamic_indicator.uncovered",
    ),
    (
        "split",
        "나눠 보기를 값 하나로 답할 수 없음",
        r"^나눠 보기",
        "계산",
        "dynamic_indicator.uncovered",
    ),
    (
        "ratio",
        "비율의 분자·분모를 정하지 못함",
        r"^비율의 분자·분모",
        "계산",
        "dynamic_indicator._ratio",
    ),
    (
        "unit",
        "단위가 다름",
        r"단위인데|건수 · 인원이 아닌 값",
        "계산",
        "dynamic_indicator · ask_session",
    ),
    (
        "projection",
        "장래추계 표 조건",
        r"장래추계 표는|장래 추계를 물었는데",
        "표 조건 확인",
        "dynamic_indicator",
    ),
    (
        "old_table",
        "최근을 물었는데 옛 표",
        r"^(연도를 말하지 않았는데|지금을 물었는데)",
        "표 조건 확인",
        "dynamic_indicator",
    ),
    (
        "load",
        "표 적재 안 됨",
        r"^적재 안 됨|^표를 받지 못함",
        "표 적재",
        "dynamic_indicator (적재) · load_queue",
    ),
    ("no_items", "항목 정보가 없음", r"^항목 정보가 없음", "표 조건 확인", "dynamic_indicator"),
    (
        "together",
        "한 통계가 여러 표로 나뉨",
        r"여러 표로 나뉨",
        "표 조건 확인",
        "dynamic_indicator",
    ),
    (
        "top_filter",
        "가장 많은 지역인데 거르는 지표 없음",
        r"^'가장 많은 지역의 값'",
        "계산",
        "dynamic_indicator",
    ),
    (
        "not_picked",
        "고른 표가 아니라 건너뜀",
        r"^고른 표가 아닌데|^검토가 고른 표를",
        "표 조건 확인",
        "dynamic_indicator.skip_reason",
    ),
    ("error", "계산 중 오류", r"^오류$", "계산", "dynamic_indicator"),
]
_COMPILED = [(k, label, re.compile(p), stage, where) for k, label, p, stage, where in CATEGORIES]
CELL_SPLIT = {
    "llm_no": "칸 고르기 LLM이 맞는 칸 없다고 봄",
    "llm_yes": "LLM은 맞다고 했지만 칸을 정하지 못함",
    "llm_error": "칸 고르기 LLM 오류",
    "rule": "LLM 없이 규칙에서 끝남",
}
LABELS = {k: label for k, label, *_ in CATEGORIES} | {
    "status": "계산했지만 실패 (status)",
    "no_try": "바로 계산을 시도하지 않음",
    "unknown": "분류 안 됨 (skip 문구 확인)",
}
WHERE = {k: (stage, where) for k, _, _, stage, where in CATEGORIES} | {
    "status": ("계산", "dynamic_indicator 계산 결과"),
    "no_try": ("통계표 찾기 → 바로 계산 연결", "ask_session · table_guide"),
    "unknown": ("-", "scripts/refusal_reasons.CATEGORIES"),
}


def category(skip: str) -> str:
    """skip 문구 → 원인 키"""
    for key, _, pattern, *_ in _COMPILED:
        if pattern.search(skip):
            return key
    return "unknown"


def _cell_split(entry: dict[str, Any]) -> str:
    llm = entry.get("cell_llm")
    if not llm:
        return "rule"
    if llm.get("error"):
        return "llm_error"
    fits = str((llm.get("raw") or {}).get("fits") or "")
    return "llm_no" if fits.startswith("아니") else "llm_yes"


def reason_of(turn: dict[str, Any] | None) -> dict[str, Any] | None:
    """통계표 안내로 끝난 턴 → 원인 {key, label, sub, skip, tbl_id}. 그 밖의 턴이면 None"""
    if not turn or turn.get("status") != TABLES:
        return None
    steps = (turn.get("trace") or {}).get("dynamic") or turn.get("dynamic") or []
    if not steps:
        return {"key": "no_try", "label": LABELS["no_try"], "sub": "", "skip": "", "tbl_id": ""}
    entry = next((s for s in steps if s.get("kind") == "picked"), steps[0])
    skip = entry.get("skip") or ""
    if not skip and entry.get("status"):
        key, skip = "status", f"status {entry['status']}"
    else:
        key = category(skip) if skip else "unknown"
    sub = _cell_split(entry) if key == "cell_none" else ""
    return {
        "key": key,
        "label": LABELS[key],
        "sub": sub,
        "skip": skip,
        "tbl_id": entry.get("tbl_id") or "",
        "search": _search_status(turn),
    }


def _search_status(turn: dict[str, Any]) -> str:
    """통계표 찾기의 판정 (ok · partial_match …). 없으면 빈 문자열"""
    return str((turn.get("tables") or {}).get("status") or "")


def exact_ids(items: Iterable[tuple[str, dict[str, Any]]]) -> set[str]:
    """통계표 안내로 끝난 턴 중 통계표 찾기가 '조건 충족'(ok)이라고 판정한 것"""
    return {
        key
        for key, turn in items
        if (r := reason_of(turn)) is not None and r["search"] == SEARCH_OK
    }


def all_skips(turn: dict[str, Any] | None) -> Counter:
    """후보 표 전체의 건너뛴 이유 (고른 표 + 다른 후보). 후보를 몇 개까지 볼지 정할 때 쓴다"""
    steps = (turn or {}).get("trace", {}).get("dynamic") or (turn or {}).get("dynamic") or []
    return Counter(category(s["skip"]) for s in steps if s.get("skip"))


def label_text(r: dict[str, Any]) -> str:
    return f"{r['label']} — {CELL_SPLIT[r['sub']]}" if r.get("sub") else r["label"]


# ---- 읽기 -----------------------------------------------------------------------------------


def turns(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """(문항 ID 또는 요청 시각, 최종 턴). 평가 원응답은 되묻기 뒤 답(final)이 있으면 그것을 본다"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if "first" in d:  # 평가 원응답
            yield d["id"], d.get("final") or d["first"]
        else:  # 요청 기록 (qa.log_request)
            yield d.get("at") or "", d


def answer_ids(golden: Path | None) -> set[str]:
    """골든셋에서 기대 행동이 '답변'인 문항 (정답 값은 읽지 않는다)"""
    if golden is None:
        return set()
    with golden.open(encoding="utf-8-sig") as f:
        return {r["문항 ID"] for r in csv.DictReader(f) if r.get("기대 행동") == "답변"}


def collect(
    items: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, list[str]], int, Counter]:
    """→ (원인 글 → 문항 목록, 통계표 안내 턴 수, 후보 전체 이유)"""
    by: dict[str, list[str]] = defaultdict(list)
    total, everything = 0, Counter()
    for key, turn in items:
        everything += all_skips(turn)
        if (r := reason_of(turn)) is None:
            continue
        total += 1
        by[label_text(r)].append(key)
    return by, total, everything


# ---- 표 ------------------------------------------------------------------------------------


def _key_of(text: str) -> str:
    base = text.split(" — ")[0]
    return next((k for k, v in LABELS.items() if v == base), "unknown")


def table(
    by: dict[str, list[str]],
    total: int,
    answers: set[str] = frozenset(),
    old: dict[str, list[str]] | None = None,
    old_total: int = 0,
    exact: set[str] | None = None,
) -> list[str]:
    """원인별 표 (markdown 줄). answers에 있는 문항은 * (답해야 하는 문항).
    exact를 주면 통계표 찾기가 '조건 충족'이라 한 문항을 † 로 표시하고 원인마다 그 수를 센다"""

    def mark(ids: list[str]) -> str:
        return " ".join(
            i + ("*" if i in answers else "") + ("†" if exact and i in exact else "") for i in ids
        )

    lines = [
        f"통계표 안내로 끝난 턴: {total}건" + (f" (비교: {old_total}건)" if old is not None else "")
    ]
    if answers:
        hit = sum(i in answers for ids in by.values() for i in ids)
        lines[0] += f", 그중 답해야 하는 문항 {hit}건 (*)"
    if exact is not None:
        hit = sum(i in exact for ids in by.values() for i in ids)
        lines[0] += f", 통계표 찾기는 '조건 충족'이라 한 턴 {hit}건 (†)"
    lines.append("")
    head = ["원인", "건수"] + (["조건 충족 †"] if exact is not None else [])
    head += (["비교"] if old is not None else []) + ["문항", "막힌 단계", "고칠 곳"]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    names = sorted(set(by) | set(old or {}), key=lambda t: (-len(by.get(t, [])), t))
    for text in names:
        ids = by.get(text, [])
        stage, where = WHERE[_key_of(text)]
        cells = [text, str(len(ids))]
        if exact is not None:
            cells.append(str(sum(i in exact for i in ids)))
        if old is not None:
            cells.append(str(len((old or {}).get(text, []))))
        cells += [mark(ids), stage, where]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="'표 찾고도 거절' 원인 집계")
    parser.add_argument("path", type=Path, help="평가 원응답(.raw.jsonl) 또는 요청 기록(.jsonl)")
    parser.add_argument("--compare", type=Path, help="비교할 예전 회차의 원응답")
    parser.add_argument("--golden", type=Path, help="골든셋 CSV (답해야 하는 문항에 * 표시)")
    parser.add_argument("--all", action="store_true", help="후보 표 전체의 건너뛴 이유도 보인다")
    parser.add_argument("--out", type=Path, help="결과를 이 파일(.md)에도 쓴다")
    args = parser.parse_args()

    by, total, everything = collect(turns(args.path))
    exact = exact_ids(turns(args.path))
    old, old_total = None, 0
    if args.compare:
        old, old_total, _ = collect(turns(args.compare))
    lines = [f"# 표 찾고도 거절 원인 ({args.path.name})", ""]
    lines += table(by, total, answer_ids(args.golden), old, old_total, exact)
    if args.all:
        lines += ["", "## 후보 표 전체의 건너뛴 이유", "", "| 원인 | 건수 |", "|---|---|"]
        lines += [f"| {LABELS[k]} | {n} |" for k, n in everything.most_common()]
    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
