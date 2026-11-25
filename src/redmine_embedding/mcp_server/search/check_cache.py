"""Opt-in cache of postfilter verdicts shared across searches.

Key: (SHA-256 of the access token, checked path), so the token itself is never
stored or logged and users never share entries. Only settled verdicts are kept
(True visible, False invisible = 403/404). Unverified results (None) and
errors such as 401 are never stored: callers must not call ``put`` with them.

Entries expire after the TTL, so permission changes show up late (at most TTL
seconds). When full, the oldest-inserted entry is dropped (FIFO, reads do not
refresh it). ``ttl <= 0`` disables the cache: nothing is read or written.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from .visible_projects import token_key


class CheckCache:
    def __init__(
        self,
        *,
        ttl: float = 0.0,
        max_entries: int = 10000,  # 暫定
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max(1, max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[tuple[str, str], tuple[float, bool]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, token: str, path: str) -> bool | None:
        """Cached verdict, or ``None`` on a miss / expiry / when disabled."""
        if not self.enabled:
            return None
        key = (token_key(token), path)
        now = self._clock()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if now - entry[0] >= self._ttl:
                del self._data[key]
                return None
            return entry[1]

    def put(self, token: str, path: str, verdict: bool | None) -> None:
        """Store a settled verdict. ``None`` (unverified) is ignored."""
        if not self.enabled or verdict is None:
            return
        key = (token_key(token), path)
        with self._lock:
            self._data[key] = (self._clock(), verdict)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
