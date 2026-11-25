"""Request-scoped values the provider needs but the SDK handlers do not pass."""

from __future__ import annotations

from contextvars import ContextVar

#: Client IP of the request being handled by /register or /authorize (set by ``RateLimitMiddleware``).
client_ip: ContextVar[str | None] = ContextVar("oauth_client_ip", default=None)
