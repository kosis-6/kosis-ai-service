"""LLM 프롬프트 모음"""


def swap(text: str, old: str, new: str) -> str:
    """프롬프트의 문장 하나를 바꾼다. 원문이 바뀌어 못 찾으면 조용히 넘어가지 않고 바로 실패한다
    (모델별 프롬프트를 원문에서 한 군데만 바꿔 만들 때 쓴다)"""
    if text.count(old) != 1:
        raise ValueError(f"바꿀 문장을 찾지 못했습니다: {old[:30]}")
    return text.replace(old, new)
