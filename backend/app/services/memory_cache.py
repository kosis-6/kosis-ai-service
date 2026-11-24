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
