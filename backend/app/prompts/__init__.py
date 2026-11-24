def swap(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"바꿀 문장을 찾지 못했습니다: {old[:30]}")
    return text.replace(old, new)
