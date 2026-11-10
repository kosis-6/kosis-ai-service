"""로그인 시도 횟수 제한: 같은 IP에서 같은 이메일로 일정 시간 안에 여러 번 틀리면 잠시 막는다

누구나 가입할 수 있어서 비밀번호를 계속 넣어 보는 공격을 막으려는 것이다.
- 이메일만 기준으로 막으면 남이 내 이메일로 일부러 틀려서 나를 못 들어오게 할 수 있다.
  그래서 (IP, 이메일) 묶음으로 센다
- 기록은 서버 메모리에 둔다. 서버를 다시 켜면 초기화되고, 서버 프로세스가 여러 개면 각자 센다.
  지금은 uvicorn 프로세스 하나라 충분하다. 여러 대로 늘리면 Redis·DB로 옮긴다
- 프록시(nginx, ALB) 뒤에 두면 IP가 모두 프록시 주소로 보인다.
  배포 때 uvicorn --proxy-headers --forwarded-allow-ips로 진짜 IP를 받게 한다
"""

import time
from collections import deque
from collections.abc import Callable

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
# 이만큼 쌓이면 시간이 지난 기록을 한 번에 지운다 (메모리가 계속 늘지 않게)
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
        """막혀 있으면 다시 시도할 수 있을 때까지 남은 초, 아니면 0"""
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
