import functools
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_calls", default=None)
_stage: ContextVar[str] = ContextVar("llm_stage", default="unknown")
MAX_CALLS = 100


def start() -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    _calls.set(calls)
    return calls


def current() -> list[dict[str, Any]] | None:
    return _calls.get()


@contextmanager
def stage(name: str) -> Iterator[None]:
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def staged(name: str) -> Callable[[F], F]:

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
