"""CLOVA Studio Chat Completions (HCX-007) 호출

API 키는 .env의 HCX_API_KEY에서 읽고, 화면에 출력하지 않는다.
응답은 스트리밍(text/event-stream)으로 받는다.
  event:token   글자 조각 (stream=True면 받는 대로 화면에 출력)
  event:result  최종 답변 전체 + 토큰 사용량 → 이것을 반환

실행: python pipeline/clova.py --question "질문"   (--question을 생략하면 예시 질문)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007"
DEFAULT_SYSTEM = "- 고도로 체계적인 분석가이자 논리 기반 문제 해결의 전문가입니다."
EXAMPLE_QUESTION = "20대 혼인 신고율"


def _api_key():
    load_dotenv(ROOT / ".env")
    key = os.getenv("HCX_API_KEY", "").strip()
    if not key:
        raise SystemExit(".env에 HCX_API_KEY가 없습니다.")
    return key


def chat(question, system=DEFAULT_SYSTEM, stream=False, effort="low",
         max_tokens=5120, temperature=0.5, top_p=0.8):
    """질문 하나를 보내고 {"content", "thinking", "usage", "finish_reason", "request_id"}를 반환한다."""
    body = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": question}]},
        ],
        "thinking": {"effort": effort},
        "topP": top_p,
        "topK": 0,
        "maxCompletionTokens": max_tokens,
        "temperature": temperature,
        "repetitionPenalty": 1.1,
    }
    request_id = str(uuid.uuid4())  # 요청 구분 번호. 문제가 생기면 이 번호로 추적
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "X-NCP-CLOVASTUDIO-REQUEST-ID": request_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        # 응답 본문에는 오류 코드·메시지만 있고 키는 없다
        raise SystemExit(f"HTTP {e.code} (request_id={request_id}): {e.read().decode('utf-8', 'replace')}")

    # 스트리밍 응답은 "event:종류" 줄 다음에 "data:{json}" 줄이 오는 형식
    event = None
    with resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event:"):
                event = line[len("event:"):]
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):])
                if event == "token" and stream:
                    # 추론 중 토큰은 content 없이 thinkingContent만 온다 → 답변 글자만 출력
                    print(data["message"].get("content", ""), end="", flush=True)
                elif event == "result":
                    if stream:
                        print()
                    return {
                        "content": data["message"]["content"],
                        "thinking": data["message"].get("thinkingContent", ""),
                        "usage": data.get("usage"),
                        "finish_reason": data.get("finishReason"),
                        "request_id": request_id,
                    }
                elif event == "error":
                    raise SystemExit(f"CLOVA 오류 (request_id={request_id}): {data}")
    raise SystemExit(f"최종 결과(event:result) 없이 응답이 끝났습니다. (request_id={request_id})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="CLOVA Studio(HCX-007)에 질문하기")
    parser.add_argument("--question", "-q", default=EXAMPLE_QUESTION,
                        help=f"보낼 질문 (생략하면 예시 질문: {EXAMPLE_QUESTION})")
    question = parser.parse_args().question
    print(f"질문: {question}\n")
    result = chat(question, stream=True)
    usage = result["usage"] or {}
    print(f"\n[종료 이유: {result['finish_reason']}, "
          f"토큰: 질문 {usage.get('promptTokens')} / 답변 {usage.get('completionTokens')}]")


if __name__ == "__main__":
    main()
