"""Short-TTL cache of the project ids a user can see (prefilter input).

The key is a SHA-256 of the access token, so the token itself is never stored
or logged. Errors (401/403, network...) propagate and are never cached; an
empty list is a valid answer and is cached like any other.

Entries expire after the TTL, so permission changes show up late (at most TTL
seconds): callers must still run the postfilter. When full, the oldest-inserted
entry is dropped (FIFO, reads do not refresh it).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class VisibleProjectsCache:
    def __init__(
        self,
        *,
        ttl: float = 60.0,
        max_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, list[int]]] = OrderedDict()

    def get_or_fetch(self, token: str, fetch: Callable[[], list[int]]) -> list[int]:
        key = token_key(token)
        now = self._clock()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None and now - entry[0] < self._ttl:
                return list(entry[1])
            self._data.pop(key, None)
        ids = list(fetch())  # outside the lock; exceptions propagate uncached
        with self._lock:
            self._data[key] = (self._clock(), ids)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return list(ids)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
