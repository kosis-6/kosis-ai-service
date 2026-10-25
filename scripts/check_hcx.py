import argparse
import asyncio

from app.services.hcx_client import HcxClient

# 프로젝트에서 쓸 후보 모델 (NOTES: 질의 분석 HCX-007, 요약 HCX-DASH-002)
DEFAULT_MODELS = ["HCX-007", "HCX-DASH-002"]
REASONING_MODELS = {"HCX-007"}


async def check(models: list[str], prompt: str) -> bool:
    client = HcxClient()
    if not client.api_key:
        print(".env에 HCX_API_KEY가 없습니다.")
        return False

    ok = True
    for model in models:
        # 추론 모델(HCX-007)은 maxTokens 대신 maxCompletionTokens를 받고,
        # 추론에도 토큰을 쓰므로 한도를 넉넉히 줘야 답변이 잘리지 않음
        if model in REASONING_MODELS:
            options = {"maxCompletionTokens": 1024}
        else:
            options = {"maxTokens": 256}
        try:
            result = await client.chat(model, [{"role": "user", "content": prompt}], **options)
        except Exception as e:
            ok = False
            print(f"[실패] {model}: {e}")
            continue
        if result.finish_reason == "length":
            ok = False
            print(f"[잘림] {model}: 토큰 한도 초과 ({result.usage})")
            continue
        print(f"[성공] {model} (토큰 {result.usage.get('totalTokens')}): {result.content.strip()}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="HCX API 연결 확인")
    parser.add_argument("models", nargs="*", default=DEFAULT_MODELS, help="확인할 모델 이름")
    parser.add_argument("--prompt", default="안녕하세요. 한 문장으로 자기소개 해주세요.")
    args = parser.parse_args()
    if not asyncio.run(check(args.models, args.prompt)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
