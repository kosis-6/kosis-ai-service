import json
import re
from typing import Any

FENCE = re.compile(r"```(?:json)?")


def _closers(text: str) -> list[tuple[int, str]]:
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
    for cut, close in reversed(_closers(body)):
        try:
            obj = json.loads(body[:cut] + close)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, True
    return None, False
