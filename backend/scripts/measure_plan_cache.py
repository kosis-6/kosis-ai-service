"""동적 지표 계산 방법 캐시의 효과 측정 (동적 지표 4단계, 산출물 "성능 측정 결과")

질문 해석(HCX)은 공용 키의 한도에 따라 시간이 크게 흔들리므로 빼고, 해석 결과를 고정해 잰다.

  캐시 없음   통계표 찾기(HCX 3회) + 표의 칸 고르기·계산
              (table_guide.find_tables + dynamic_indicator.answer)
  DB 캐시     저장한 표로 칸 고르기·계산 (dynamic_indicator.cached_answer, 메모리 비움)
  메모리 캐시  같은 것, 계획을 서버 메모리에서 읽음
  계획 읽기    plan_cache.find 한 번 (DB / 메모리), 반복 평균

실행 (API 컨테이너, 적재된 DB와 HCX 키 필요. 캐시 없음 단계는 HCX를 질문마다 3번 부른다):
  python -m scripts.measure_plan_cache
  python -m scripts.measure_plan_cache --rounds 3 --skip-search   캐시 없음 단계 빼기
결과는 표로 출력하고 data/measure/plan_cache.json에 남긴다.
"""

import argparse
import asyncio
import json
import logging
import statistics
import time
from pathlib import Path

from app.services import dynamic_indicator, plan_cache, table_guide

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "measure" / "plan_cache.json"
# (질문, 질문 해석 결과). 해석은 /ask에서 실제로 나온 값을 고정해 둔다
CASES = [
    (
        "2020년 서울 2인 가구 수",
        {"type": "value", "indicator": "2인 가구 수", "term": "2인 가구 수",
         "regions": ["서울"], "year": 2020, "sex": "계"},
    ),
    (
        "2020년 서울 1인 가구 중 여성 비율",
        {"type": "value", "indicator": "1인 가구 중 여성 비율", "term": "1인 가구 중 여성 비율",
         "regions": ["서울"], "year": 2020, "sex": "여"},
    ),
    (
        "1인 가구가 가장 많은 시도",
        {"type": "rank", "indicator": "1인 가구 수", "term": "1인 가구 수",
         "regions": [], "level": "시도", "top": 3, "sex": "계"},
    ),
]  # fmt: skip


async def _timed(coro) -> tuple[float, object]:
    start = time.perf_counter()
    out = await coro
    return (time.perf_counter() - start) * 1000, out


async def measure(rounds: int, skip_search: bool, pause: float) -> dict:
    rows = []
    for question, parsed in CASES:
        row = {"question": question}
        if not skip_search:
            times, status = [], None
            for _ in range(rounds):
                sid = table_guide.new_search_session()

                async def miss(question=question, parsed=parsed, sid=sid):
                    found = await table_guide.find_tables(question, sid, None, None)
                    return await dynamic_indicator.answer(question, parsed, found, {})

                ms, res = await _timed(miss())
                times.append(ms)
                status = (res or {}).get("status")
                await asyncio.sleep(pause)  # HCX 한도 (분당 60회)
            row["no_cache_ms"], row["no_cache_status"] = statistics.median(times), status
        for label, clear in (("db_cache_ms", True), ("memory_cache_ms", False)):
            times, status = [], None
            for _ in range(rounds):
                if clear:
                    plan_cache.CACHE.clear()
                ms, res = await _timed(dynamic_indicator.cached_answer(question, parsed, {}))
                times.append(ms)
                status = (res or {}).get("status")
            row[label], row[label.replace("_ms", "_status")] = statistics.median(times), status
        rows.append(row)

    found = plan_cache.key_of(CASES[0][1])
    reads = {}
    if found:
        for label, clear in (("db", True), ("memory", False)):
            n, total = 200, 0.0
            await plan_cache.find(found[0])
            for _ in range(n):
                if clear:
                    plan_cache.CACHE.clear()
                ms, _ = await _timed(plan_cache.find(found[0]))
                total += ms
            reads[label] = round(total / n, 3)
    return {
        "rounds": rounds,
        "cases": rows,
        "plan_read_ms": reads,
        "memory": plan_cache.CACHE.stats(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--pause", type=float, default=8.0, help="캐시 없음 반복 사이 쉬는 초")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 주소(키)를 찍지 않는다
    result = asyncio.run(measure(args.rounds, args.skip_search, args.pause))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"{'질문':<28} {'캐시 없음':>10} {'DB 캐시':>9} {'메모리':>9}")
    for r in result["cases"]:
        no = f"{r['no_cache_ms']:.0f}ms" if "no_cache_ms" in r else "-"
        db, mem = r["db_cache_ms"], r["memory_cache_ms"]
        print(f"{r['question']:<28} {no:>10} {db:>7.0f}ms {mem:>7.0f}ms")
    print("계획 읽기 (평균):", result["plan_read_ms"], "| 메모리 캐시:", result["memory"])


if __name__ == "__main__":
    main()
