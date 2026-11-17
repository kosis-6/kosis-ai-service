"""서버 메모리 캐시: 최근에 쓴 것부터 maxsize개까지, ttl초 동안 (LRU + TTL)

DB가 원본이고 여기는 빠르게 읽으려는 사본이다. 서버를 다시 켜면 비고, 그때는 DB에서 다시 읽는다.
프로세스마다 따로라서 서버(프로세스)를 늘리면 Redis 같은 공용 캐시로 옮긴다.
지금 쓰는 곳: 동적 지표 계산 방법(plan_cache.py).

  cache = MemoryCache("plans", maxsize=512, ttl=600)
  cache.get(key) → 값 또는 MISSING / cache.set(key, value) / cache.delete(key) / cache.clear()
  cache.stats() → 찾은 횟수·못 찾은 횟수·크기 (성능 측정, 관리자 화면)
  MEMORY_CACHE=0 이면 끄고 늘 DB에서 읽는다 (캐시를 켤 때와 끌 때 응답 시간 비교)
"""

import os
import threading
import time
from collections import OrderedDict
from typing import Any

MISSING = object()


def enabled() -> bool:
    return os.environ.get("MEMORY_CACHE", "1") != "0"


class MemoryCache:
    def __init__(self, name: str, maxsize: int = 512, ttl: float = 600.0, clock=time.monotonic):
        self.name, self.maxsize, self.ttl, self._clock = name, maxsize, ttl, clock
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: Any) -> Any:
        if not enabled():
            return MISSING
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] <= self._clock():
                if item is not None:
                    del self._items[key]
                self.misses += 1
                return MISSING
            self._items.move_to_end(key)
            self.hits += 1
            return item[1]

    def set(self, key: Any, value: Any) -> None:
        if not enabled():
            return
        with self._lock:
            self._items[key] = (self._clock() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def delete(self, key: Any) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "name": self.name,
                "size": len(self._items),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else None,
                "enabled": enabled(),
            }
