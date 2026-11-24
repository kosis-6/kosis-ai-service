import asyncio
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services import search
from app.services import slots as slot_parser
from scripts.check_embedding import PROBES

RESULTS = Path(__file__).resolve().parent.parent / "eval" / "results"
OUT_OF_SCOPE = [
    "아파트 매매 가격 추이",
    "서울 지하철 하루 이용객 수",
    "대학교 등록금 평균",
    "올해 쌀 생산량",
    "청년 실업률",
    "오늘 서울 날씨",
]


def summarize(result: dict[str, Any], rule: str | None) -> dict[str, Any]:
    candidates = result.get("candidates") or []
    others = result.get("other_families") or []
    top = candidates[0]["score"] if candidates else None
    table = result.get("table") or {}
    options = result.get("options") or [{}]
    name = (
        table.get("table_name")
        or (result.get("family") or {}).get("family_name")
        or options[0].get("family_name")
        or ""
    )
    return {
        "status": result["status"],
        "name": name,
        "relevant": bool(rule and name and re.search(rule, name)),
        "top": top,
        "gap": top - others[0]["score"] if top is not None and others else None,
    }


def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


async def main() -> None:
    tables = await search.load_tables()
    questions = [(q, rule) for q, rule in PROBES] + [(q, None) for q in OUT_OF_SCOPE]
    rows = []
    for question, rule in questions:
        slots = slot_parser.correct(question, slot_parser.empty_slots(question))
        dense = await search.dense_scores(question)
        if dense is None:
            raise SystemExit("표 벡터나 질문 임베딩이 없음 (pipeline.build_embeddings, 설정 확인)")
        rows.append(
            {
                "question": question,
                "in_scope": rule is not None,
                "keyword": summarize(search.find(tables, slots), rule),
                "hybrid": summarize(search.find(tables, slots, dense=dense), rule),
            }
        )

    lines = [
        f"# 통계표 검색 점검: 키워드 vs 하이브리드 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- 하이브리드: 임베딩 + 키워드 {search.KEYWORD_WEIGHT}배(키워드 점수 "
        f"{search.KEYWORD_GATE} 이상일 때), 임베딩 상위 {search.DENSE_TOP_K}개 후보 포함",
        f"- 범위 밖 기준: 키워드 {search.MIN_SCORE} / 하이브리드 {search.HYBRID_MIN_SCORE}, "
        f"ambiguous 기준(1·2순위 차): 키워드 {search.AMBIGUOUS_GAP} / 하이브리드 "
        f"{search.HYBRID_AMBIGUOUS_GAP}",
        "- 관련 = 최종 표명(clarify·ambiguous는 1순위 패밀리명)에 규칙 단어가 있음. "
        "**골든셋이 아닌 규칙 기반 점검**",
        "",
        "## 요약",
        "",
        "| 방식 | 범위 안: 관련 표·패밀리 | 범위 안: 상태 | 범위 밖: out_of_scope |",
        "|---|---|---|---|",
    ]
    for method in ("keyword", "hybrid"):
        inside = [r[method] for r in rows if r["in_scope"]]
        outside = [r[method] for r in rows if not r["in_scope"]]
        counts = Counter(s["status"] for s in inside)
        lines.append(
            f"| {method} | {sum(s['relevant'] for s in inside)} / {len(inside)} | "
            + ", ".join(f"{k} {v}" for k, v in counts.items())
            + f" | {sum(s['status'] == 'out_of_scope' for s in outside)} / {len(outside)} |"
        )
    lines += [
        "",
        "## 질문별 (상태 · 최종 표 · 1순위 패밀리 점수 / 2순위와의 차)",
        "",
        "| 질문 | 범위 | 키워드 | 하이브리드 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        cells = []
        for method in ("keyword", "hybrid"):
            s = r[method]
            mark = "✓ " if s["relevant"] else ""
            name = s["name"] or "-"
            cells.append(f"{s['status']} · {mark}{name} · {fmt(s['top'])} / {fmt(s['gap'])}")
        scope = "안" if r["in_scope"] else "밖"
        lines.append(f"| {r['question']} | {scope} | " + " | ".join(cells) + " |")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"search_check_{datetime.now():%Y%m%d}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n결과: {out}")


if __name__ == "__main__":
    asyncio.run(main())
