"""질의응답 명령줄 실행 (API POST /ask 와 같은 처리, 중간 결과 확인용)"""

import argparse
import asyncio
import json

from app.services import qa

EXAMPLE_QUESTION = "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"


async def run(question: str, debug: bool) -> None:
    print(f"질문: {question}\n")
    parsed = await qa.understand(question)
    result = qa.compute(parsed)
    if debug:
        print("[1. 질문 분석]")
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        print("\n[2~4. 계산 결과]")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"\n[5. 답변] status={result.get('status')}")
    print(qa.summary(result))
    block = qa.render(result)
    if block:
        print(f"\n{block}")


def main() -> None:
    parser = argparse.ArgumentParser(description="지역별 인구통계 질의응답")
    parser.add_argument(
        "--question",
        "-q",
        default=EXAMPLE_QUESTION,
        help=f"질문 (생략하면 예시 질문: {EXAMPLE_QUESTION})",
    )
    parser.add_argument("--debug", action="store_true", help="질문 분석·계산 결과 JSON도 출력")
    args = parser.parse_args()
    asyncio.run(run(args.question, args.debug))


if __name__ == "__main__":
    main()
