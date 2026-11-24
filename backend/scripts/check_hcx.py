import argparse
import asyncio

from app.services.hcx_client import HcxClient

DEFAULT_MODELS = ["HCX-007"]
REASONING_MODELS = {"HCX-007"}


async def check(models: list[str], prompt: str) -> bool:
    client = HcxClient()
    if not client.api_key:
        print(".env에 HCX_API_KEY가 없습니다.")
        return False

    ok = True
    for model in models:
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
