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
        print("\n[5. 답변]")
    text, dropped = await qa.write_text(question, result)
    if text:
        print(text)
    block = qa.render(result)
    if block:
        print(f"\n{block}")
    if dropped:
        print(
            f"\n[숫자 확인] 설명 문장에 근거 없는 숫자({', '.join(dropped)})가 있어 "
            "문장을 빼고 수치만 표시"
        )
    else:
        print("\n[숫자 확인] 이상 없음")


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
