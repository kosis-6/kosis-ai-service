"""CLOVA Studio 임베딩 사용 가능 여부·모델 비교 점검 (실전 2 연구계획서 3.2, E2 사전 점검)

  1. 사용 가능 여부  모델마다 호출 성공, 차원, 응답 시간(p50·p95, 10회)
  2. 입력 길이       100자~2만 자 입력이 받아지는지, 토큰 수 (넘으면 40003)
  3. 동시 호출       20건을 한꺼번에 보내 호출 제한에 걸리는지
  4. 의미 매칭 예비 점검
     일상어 질문 10개로 전체 표 2,364개의 '표명'을 검색해, 규칙으로 정한 관련 표
     (표명에 규칙 단어가 있는 표)가 Top-10에 몇 개 드는지, 첫 관련 표 순위를 본다.
     같은 질문을 현재 키워드 검색(search.keyword_scores, 질문 원문)에도 넣어 비교한다.
     골든셋이 아니므로 정확도 주장에는 쓰지 않는다. 정식 비교는 골든셋으로 하는 E2.

결과: eval/results/embedding_check_YYYYMMDD.md (요약) / .csv (질문 × 방식별 결과)
실행: python -m scripts.check_embedding           (1~3, 모델당 약 36회 호출)
      python -m scripts.check_embedding --tables  (4 포함. 분당 60건 제한이라 모델당 약 40분)
"""

import argparse
import asyncio
import csv
import math
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services import embedding, search
from app.services import slots as slot_parser
from app.services.hcx_client import HcxError

RESULTS = Path(__file__).resolve().parent.parent / "eval" / "results"
TOP_K = 10
LENGTHS = (100, 200, 300, 400, 500, 2000, 20000)  # 입력 글자 수

# (일상어 질문, 관련 표 규칙: 표명에 이 단어가 있으면 관련 표)
PROBES = [
    ("노인 인구가 얼마나 늘었는지 볼 수 있는 표", r"고령자|고령인구|65세"),
    ("아기가 몇 명 태어났는지", r"출생"),
    ("결혼 몇 건 했는지", r"혼인"),
    ("이혼한 부부 수", r"이혼"),
    ("혼자 사는 가구", r"1인\s*가구"),
    ("우리 동네 외국인 주민 수", r"외국인"),
    ("귀화한 사람 통계", r"귀화|국적을 취득"),
    ("돌아가신 분 수", r"사망"),
    ("다른 지역으로 이사 간 사람", r"이동|전입|전출"),
    ("앞으로 인구가 어떻게 될지 예측", r"장래|추계"),
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


def ranking_stats(scores: list[float], relevant: list[bool]) -> dict[str, Any]:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    hits = sum(relevant[i] for i in order[:TOP_K])
    first = next((rank for rank, i in enumerate(order, start=1) if relevant[i]), None)
    return {f"hits@{TOP_K}": hits, "first_rank": first, "top1": order[0]}


async def availability(model: str) -> dict[str, Any]:
    times, dim = [], None
    for _ in range(10):
        start = time.perf_counter()
        result = await embedding.embed_with_usage("시군구별 혼인 귀화자 수", model)
        times.append((time.perf_counter() - start) * 1000)
        dim = len(result["embedding"])
    lengths: dict[int, Any] = {}
    for n in LENGTHS:
        try:
            result = await embedding.embed_with_usage("인구 " * (n // 3), model)
            lengths[n] = result["inputTokens"]
        except HcxError as e:  # 40003 Context length exceeded
            lengths[n] = f"초과({e.code})"
    start = time.perf_counter()
    await embedding.embed([f"동시 호출 {i}" for i in range(20)], model)
    concurrent_s = round(time.perf_counter() - start, 1)
    limits = await embedding.rate_limit(model)
    return {
        "model": model,
        "dim": dim,
        "p50_ms": round(statistics.median(times)),
        "p95_ms": round(sorted(times)[int(len(times) * 0.95) - 1]),
        "tokens": lengths,
        "concurrent_20_s": concurrent_s,
        "limit_per_min": limits.get("x-ratelimit-limit-requests"),
    }


async def probe(tables: list[dict[str, Any]], models: list[str]) -> list[dict[str, Any]]:
    names = [t["table_name"] for t in tables]
    vectors = {}
    for model in models:
        start = time.perf_counter()
        vectors[model] = await embedding.embed(names, model)
        print(f"  {model}: 표명 {len(names)}개 임베딩 {time.perf_counter() - start:.0f}초")

    rows = []
    for question, rule in PROBES:
        relevant = [bool(re.search(rule, n)) for n in names]
        tokens = search.query_tokens(slot_parser.empty_slots(question))
        methods = {"keyword": search.keyword_scores(tables, tokens)}
        for model in models:
            q = (await embedding.embed([question], model))[0]
            methods[model] = [cosine(q, v) for v in vectors[model]]
        for method, scores in methods.items():
            stats = ranking_stats(scores, relevant)
            top = sorted(scores, reverse=True)
            rows.append(
                {
                    "question": question,
                    "rule": rule,
                    "relevant_tables": sum(relevant),
                    "method": method,
                    f"hits@{TOP_K}": stats[f"hits@{TOP_K}"],
                    "first_rank": stats["first_rank"],
                    "top1_table": names[stats["top1"]],
                    "score_top1": round(top[0], 3),
                    "score_top10": round(top[TOP_K - 1], 3),
                    "score_median": round(statistics.median(scores), 3),
                }
            )
    return rows


def doc_lengths(tables: list[dict[str, Any]]) -> dict[str, list[int]]:
    """검색 문서 후보별 글자 수 (API 호출 없음). 문서 구성은 기능 정의서 F-02 초안"""

    def parts(t: dict[str, Any]) -> dict[str, str]:
        classes = t["classifications"] or []
        items = [i["name"] for i in t["items"] or []]
        name = f"{t['survey_name']} {t['table_name']}"
        with_classes = f"{name} {' '.join(c['name'] for c in classes)}"
        return {
            "표명+조사명": name,
            "+분류명": with_classes,
            "+분류명+항목명": f"{with_classes} {' '.join(items)}",
            "+분류명+항목명+분류 값+경로": " ".join(
                [
                    with_classes,
                    *items,
                    *[v for c in classes for v in c.get("sample_values") or [] if v],
                    *(t["path_texts"] or []),
                ]
            ),
        }

    out: dict[str, list[int]] = {}
    for t in tables:
        for k, v in parts(t).items():
            out.setdefault(k, []).append(len(v))
    return out


def write_report(
    avail: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    path: Path,
    tables: list[dict[str, Any]],
) -> None:
    lines = [
        f"# CLOVA Studio 임베딩 점검 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "- 키: HCX_API_KEY (HCX와 같은 키), 엔드포인트 `/v1/api-tools/embedding/{model}`",
        "- 목적: 연구계획서 3.2 '임베딩 API 사용 가능 여부' 확인, E2 모델 비교 전 예비 점검",
        "",
        "## 1~3. 사용 가능 여부·입력 길이·동시 호출",
        "",
        "| 모델 | 차원 | 응답 p50 | 응답 p95 "
        "| 입력 100/200/300/400/500/2천/2만 자 → 토큰 | 동시 20건 | 분당 호출 한도 |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in avail:
        tokens = " / ".join(str(v) for v in a["tokens"].values())
        waited = " (호출 제한 대기 포함)" if a["concurrent_20_s"] > 5 else ""
        lines.append(
            f"| {a['model']} | {a['dim']} | {a['p50_ms']}ms | {a['p95_ms']}ms | {tokens} "
            f"| 성공 {a['concurrent_20_s']}초{waited} "
            f"| {a['limit_per_min']}건 |"
        )
    lines += [
        "",
        "- 호출 제한은 모델마다 따로 센다. 표 2,364개 임베딩 = 모델 하나에 약 40분",
        "- 서비스에서는 질문마다 질문 임베딩 1건 → `/search`도 분당 60건이 상한",
        "- dolphin 두 모델은 한국어 100자가 약 133토큰(v2는 34토큰), 300자는 되고 400자는 40003"
        " → 입력 한도 약 512토큰(한국어 약 380자)",
        "",
        f"## 검색 문서 길이 (표 {len(tables)}개, 글자 수)",
        "",
        "| 문서 구성 | 중앙값 | 90% | 최대 | 380자 넘는 표 (dolphin 불가) |",
        "|---|---|---|---|---|",
    ]
    for label, lengths in doc_lengths(tables).items():
        q = statistics.quantiles(lengths, n=10)
        over = sum(n > 380 for n in lengths)
        lines.append(
            f"| {label} | {statistics.median(lengths):.0f} | {q[-1]:.0f} | {max(lengths)} "
            f"| {over}개 ({over / len(lengths):.0%}) |"
        )
    if rows:
        methods = list(dict.fromkeys(r["method"] for r in rows))
        lines += [
            "",
            f"## 4. 의미 매칭 예비 점검 (표명만, 표 {len(tables)}개)",
            "",
            "관련 표 = 표명에 규칙 단어가 있는 표. **골든셋이 아닌 규칙 기반 점검**이라",
            "정확도 주장에는 쓰지 않는다.",
            "키워드는 현재 검색(search.keyword_scores)에 질문 원문을 넣은 결과.",
            "",
            f"| 방식 | Top-{TOP_K} 안 관련 표 (평균) | 첫 관련 표 1위인 질문 "
            "| 1위·10위·중앙값 점수 (평균) |",
            "|---|---|---|---|",
        ]
        for m in methods:
            rs = [r for r in rows if r["method"] == m]
            hits = statistics.mean(r[f"hits@{TOP_K}"] for r in rs)
            first = sum(r["first_rank"] == 1 for r in rs)
            spread = " / ".join(
                f"{statistics.mean(r[k] for r in rs):.3f}"
                for k in ("score_top1", "score_top10", "score_median")
            )
            lines.append(f"| {m} | {hits:.1f} / {TOP_K} | {first} / {len(rs)} | {spread} |")
        lines += [
            "",
            "### 질문별",
            "",
            "| 질문 | 관련 표 수 | "
            + " | ".join(f"{m} (Top-10 / 첫 순위)" for m in methods)
            + " |",
            "|---|---|" + "---|" * len(methods),
        ]
        for question, _ in PROBES:
            rs = {r["method"]: r for r in rows if r["question"] == question}
            cells = [f"{rs[m][f'hits@{TOP_K}']} / {rs[m]['first_rank']}" for m in methods]
            first = next(iter(rs.values()))
            lines.append(f"| {question} | {first['relevant_tables']} | " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="*", default=list(embedding.MODELS))
    parser.add_argument("--tables", action="store_true", help="4. 의미 매칭 점검 (모델당 약 40분)")
    args = parser.parse_args()

    print("1~3. 사용 가능 여부·입력 길이·동시 호출")
    avail = [await availability(m) for m in args.models]
    for a in avail:
        print(f"  {a}")
    rows: list[dict[str, Any]] = []
    tables = await search.load_tables()
    if args.tables:
        print("4. 의미 매칭 예비 점검")
        rows = await probe(tables, args.models)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = RESULTS / f"embedding_check_{datetime.now():%Y%m%d}"
    if rows:
        with open(stem.with_suffix(".csv"), "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    write_report(avail, rows, stem.with_suffix(".md"), tables)
    print(f"\n결과: {stem}.md" + (f", {stem}.csv" if rows else ""))


if __name__ == "__main__":
    asyncio.run(main())
