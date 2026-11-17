"""대화 상태 읽기: 서버 메모리 캐시가 있을 때와 없을 때 (대화 기록 보관, 산출물 "성능 측정 결과")

/ask는 턴마다 대화 상태(app.chat_sessions)를 읽는다. 서버 메모리 사본(ask_session.STATE_CACHE)이
있으면 DB를 읽지 않는다. 임시 대화 상태 하나를 만들어 읽기 시간을 재고 지운다.

실행 (API 컨테이너, DB 필요): python -m scripts.measure_chat_state [--n 500]
결과는 data/measure/chat_state.json에도 남긴다.
"""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine
from app.services import ask_session

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "measure" / "chat_state.json"
SID = "A_measure00000"
STATE = {
    "conditions": {"type": "trend", "indicator": "출생아 수", "regions": ["서울", "부산"]},
    "sources": {"regions": "user"},
    "last": {"status": "ok", "answer": "답 " * 200, "sources": [], "chart": None},
    "recent": ["서울 출생아 추이", "부산도 같이"],
    "turn_no": 2,
    "user_id": None,
}


async def measure(n: int) -> dict:
    await ask_session.save(SID, STATE)
    try:
        out = {}
        for label, clear in (("db_ms", True), ("memory_ms", False)):
            times = []
            await ask_session.load(SID, ask_session.now())
            for _ in range(n):
                if clear:
                    ask_session.STATE_CACHE.clear()
                start = time.perf_counter()
                await ask_session.load(SID, ask_session.now())
                times.append((time.perf_counter() - start) * 1000)
            out[label] = {
                "median": round(statistics.median(times), 3),
                "p95": round(sorted(times)[int(n * 0.95) - 1], 3),
            }
        out["n"] = n
        out["memory"] = ask_session.STATE_CACHE.stats()
        return out
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("delete from app.chat_sessions where session_id = :k"),
                {"k": ask_session._key(SID)},
            )
        ask_session.STATE_CACHE.delete(SID)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=500)
    args = parser.parse_args()
    result = asyncio.run(measure(args.n))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"대화 상태 읽기 {result['n']}회")
    print(f"  DB에서      중앙값 {result['db_ms']['median']}ms, p95 {result['db_ms']['p95']}ms")
    print(
        f"  메모리에서  중앙값 {result['memory_ms']['median']}ms,"
        f" p95 {result['memory_ms']['p95']}ms"
    )


if __name__ == "__main__":
    main()
