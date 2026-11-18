"""LLM이 낸 JSON 읽기: 글자 수 한도에 잘린 응답도 온전한 앞부분은 살린다 (11/18, 세션 B)

Gemma(vLLM)에 응답 모양(json_schema)을 강제하면,
모델이 답을 다 쓴 뒤 스키마상 남은 필수 칸이 있을 때
닫는 글자 대신 공백만 이어 써서 글자 수 한도에 잘리는 일이 있다 (칸 고르기 LLM: 11/18 세 표 모두).
그런 응답도 이미 다 쓴 칸(예: fits · picks)은 온전하므로, 잘린 꼬리를 버리고 앞부분만 읽는다.
"""

import json
import re
from typing import Any

FENCE = re.compile(r"```(?:json)?")


def _closers(text: str) -> list[tuple[int, str]]:
    """문자열 밖의 쉼표 · 닫는 괄호 위치마다 (자를 위치, 그때 열린 괄호를 닫는 글자들)"""
    stack: list[str] = []
    out: list[tuple[int, str]] = []
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            out.append((i + 1, "".join(reversed(stack))))
        elif ch == ",":
            out.append((i, "".join(reversed(stack))))
    return out


def loads(raw: str | None) -> tuple[dict[str, Any] | None, bool]:
    """(JSON 객체 또는 None, 잘린 응답을 살렸는가)"""
    text = FENCE.sub("", raw or "")
    start = text.find("{")
    if start < 0:
        return None, False
    end = text.rfind("}")
    if end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj, False
        except json.JSONDecodeError:
            pass
    body = text[start:]
    for cut, close in reversed(_closers(body)):  # 뒤에서부터: 가장 많이 살리는 자리
        try:
            obj = json.loads(body[:cut] + close)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, True
    return None, False
