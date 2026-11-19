"""LLM 호출 기록: 질문 하나가 어느 단계에서 어느 LLM을 몇 번, 몇 토큰, 몇 초 불렀는지

질문을 받을 때 start()로 빈 목록을 만들고, HCX · Gemma 클라이언트가 호출을 마칠 때마다 record()로
한 줄씩 쌓는다. 부르는 곳은 `@staged("cell_pick")` 또는 `with stage(...)`로 단계 이름만 붙인다.
목록은 요청마다 따로라(ContextVar) 후보 표를 동시에 볼 때도 같은 질문의 목록에 쌓이고,
다른 질문과 섞이지 않는다 (flow.py와 같은 방식).
start()를 부르지 않은 곳(단위 테스트, 스크립트)에서는 record()가 아무것도 하지 않는다.

한 줄 (클라이언트의 chat 한 번 = 한 줄. 다시 시도한 횟수는 attempts에 센다):
  stage                  understand(질문 이해) · follow_up(이어 묻는 말) · slots(조건 추출) ·
                         plan(계획) · llm_pick(후보 검토) · together(함께 볼 표) ·
                         cell_pick(칸 고르기) · alternative(대안 고르기). 이름이 없으면 unknown
  llm · model            hcx / gemma, 보낸 모델 이름
  ms                     기다린 시간까지 합친 걸린 시간
  prompt_tokens · completion_tokens   응답의 usage (실패하면 없음)
  max_completion_tokens  요청에 넣은 응답 최대 길이. HCX는 분당 한도에서 이만큼을 미리 잡는다
  attempts · waited_ms   보낸 횟수(다시 시도 포함), 다시 시도 전에 쉰 시간 합
  outcome                ok / rate_limit / transient(연결 실패 · 서버 오류) / error
  error                  실패했을 때 오류 코드

쓰는 곳: 요청 기록(requests.jsonl의 llm_calls), 개발셋 평가 CSV · 요약의 LLM 호출 칸
"""

import functools
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_calls", default=None)
_stage: ContextVar[str] = ContextVar("llm_stage", default="unknown")
MAX_CALLS = 100  # 기록이 너무 길어지지 않게 (평소 한 질문에 10번 안쪽)


def start() -> list[dict[str, Any]]:
    """이 요청의 호출 목록을 새로 만든다 (/ask 한 턴의 처음)"""
    calls: list[dict[str, Any]] = []
    _calls.set(calls)
    return calls


def current() -> list[dict[str, Any]] | None:
    return _calls.get()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """이 블록 안의 LLM 호출에 단계 이름을 붙인다"""
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def staged(name: str) -> Callable[[F], F]:
    """async 함수 안의 LLM 호출에 단계 이름을 붙이는 데코레이터 (`@staged("cell_pick")`)"""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            with stage(name):
                return await fn(*args, **kwargs)

        return inner  # type: ignore[return-value]

    return wrap


def outcome_of(error: Exception | None) -> str:
    if error is None:
        return "ok"
    if getattr(error, "is_rate_limit", False):
        return "rate_limit"
    if getattr(error, "is_transient", False):
        return "transient"
    return "error"


def record(
    *,
    llm: str,
    model: str,
    ms: float,
    options: dict[str, Any],
    attempts: int,
    waited_ms: float,
    usage: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    """호출 한 번을 기록한다. start()가 없으면 아무것도 하지 않는다"""
    calls = _calls.get()
    if calls is None or len(calls) >= MAX_CALLS:
        return
    usage = usage or {}
    row: dict[str, Any] = {
        "stage": _stage.get(),
        "llm": llm,
        "model": model,
        "ms": round(ms),
        "prompt_tokens": usage.get("promptTokens"),
        "completion_tokens": usage.get("completionTokens"),
        "max_completion_tokens": options.get("maxCompletionTokens") or options.get("maxTokens"),
        "attempts": attempts,
        "waited_ms": round(waited_ms),
        "outcome": outcome_of(error),
    }
    if error is not None:
        row["error"] = str(getattr(error, "code", "") or type(error).__name__)
    calls.append(row)


def totals(calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    """호출 목록 → 합계 (평가 CSV 한 문항 · 요약에 쓴다)"""
    calls = calls or []
    by_llm = Counter(c["llm"] for c in calls)
    by_stage: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "ms": 0, "tokens": 0})
    for c in calls:
        s = by_stage[c["stage"]]
        s["calls"] += 1
        s["ms"] += c.get("ms") or 0
        s["tokens"] += (c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0)
    return {
        "calls": len(calls),
        "hcx": by_llm.get("hcx", 0),
        "gemma": by_llm.get("gemma", 0),
        "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
        "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls),
        "reserved_tokens": sum(c.get("max_completion_tokens") or 0 for c in calls),
        "retries": sum(max((c.get("attempts") or 1) - 1, 0) for c in calls),
        "waited_ms": sum(c.get("waited_ms") or 0 for c in calls),
        "failed": sum(c.get("outcome") != "ok" for c in calls),
        "by_stage": dict(by_stage),
    }
