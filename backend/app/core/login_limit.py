import time
from collections import deque
from collections.abc import Callable

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
PRUNE_AT = 10_000


class LoginLimiter:
    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        q = self._fails.get(key, deque())
        cutoff = self.clock() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
        return q

    def retry_after(self, key: str) -> int:
        q = self._recent(key)
        if len(q) < self.max_failures:
            return 0
        return max(1, int(q[0] + self.window - self.clock()) + 1)

    def fail(self, key: str) -> None:
        if len(self._fails) >= PRUNE_AT:
            for k in list(self._fails):
                self._recent(k)
        self._fails.setdefault(key, deque()).append(self.clock())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


def key(ip: str | None, email: str) -> str:
    return f"{ip or '-'}|{email.strip().lower()}"


limiter = LoginLimiter()
