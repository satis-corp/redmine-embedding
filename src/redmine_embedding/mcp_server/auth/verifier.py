"""Token verification by introspection, with a short cache.

``verify`` answers: is this upstream access token active (and with which scopes)?
Active results are cached for ``ttl`` seconds (never past the token's ``exp``),
keyed by the SHA-256 of the token. Inactive results are not cached (the caller
then refreshes the upstream token, which is a different string). When Redmine
cannot answer, ``UpstreamUnavailable`` propagates: that is *not* "invalid".
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable

from .crypto import hash_token
from .upstream import IntrospectionResult, UpstreamOAuth

MAX_ENTRIES = 1000


class IntrospectionVerifier:
    def __init__(self, upstream: UpstreamOAuth, ttl: float, *, clock: Callable[[], float] = time.time, max_entries: int = MAX_ENTRIES) -> None:
        self._upstream = upstream
        self._ttl = ttl
        self._clock = clock
        self._max = max_entries
        self._cache: OrderedDict[str, tuple[float, IntrospectionResult]] = OrderedDict()

    async def verify(self, token: str) -> IntrospectionResult | None:
        key = hash_token(token)
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None:
            if hit[0] > now:
                return hit[1]
            del self._cache[key]
        result = await self._upstream.introspect(token)
        if result is None:
            return None
        if result.exp is not None and result.exp <= now:
            return None  # active but past exp (clock skew / stale answer): treat as expired
        if self._ttl > 0:
            expiry = now + self._ttl
            if result.exp is not None:
                expiry = min(expiry, result.exp)
            self._cache[key] = (expiry, result)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return result

    def forget(self, token: str) -> None:
        self._cache.pop(hash_token(token), None)
