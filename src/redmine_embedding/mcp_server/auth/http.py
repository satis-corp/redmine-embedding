"""HTTP layer of the OAuth2 server: routes under the public path, 503 mapping.

The MCP SDK registers /authorize /token /register /revoke at the root of its app.
Here they are served under the public path (``/mcp-search/authorize`` ...), which is
what the metadata advertises, so a reverse proxy can forward ``/mcp-search`` and the two
``/.well-known/...`` paths of this server without rewriting.

Error mapping done here (the SDK's middleware does the rest):
  no / invalid / expired / revoked token  -> 401, ``WWW-Authenticate: Bearer error="invalid_token", resource_metadata=...``  (SDK)
  token without the required scopes        -> 403 ``insufficient_scope``                                                    (SDK)
  Redmine cannot be asked (introspect / refresh failed) or the DB is down -> 503 ``temporarily_unavailable`` + Retry-After   (here)
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import psycopg
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp.server.transport_security import TransportSecuritySettings

from .config import OAuthConfig
from .context import client_ip
from .crypto import DecryptError
from .upstream import UpstreamUnavailable

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .provider import RedmineOAuthProvider

logger = logging.getLogger(__name__)

AS_PATHS = {"/authorize", "/token", "/register", "/revoke"}
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
RETRY_AFTER_SECONDS = 5


class ServiceUnavailableMiddleware:
    """Turn "cannot be decided" failures into 503 (never into 401: that would make clients sign in again)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except (UpstreamUnavailable, psycopg.OperationalError, DecryptError) as e:
            logger.warning("temporarily unavailable: %s", type(e).__name__)  # type only: no message, no token
            if started:
                raise
            body = json.dumps({"error": "temporarily_unavailable", "error_description": "認証サーバーが一時的に利用できません"}).encode()
            await send({"type": "http.response.start", "status": 503, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(RETRY_AFTER_SECONDS).encode()), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})


class RateLimiter:
    """Sliding window per key, in process (single instance). The unauthenticated endpoints use it."""

    MAX_KEYS = 10000

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > self.MAX_KEYS:  # drop idle keys
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                del self._hits[k]
        return True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self.app, self.limiter = app, limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") != "OPTIONS":
            client = scope.get("client")
            ip = client[0] if client else "-"
            if not self.limiter.allow(ip):
                body = json.dumps({"error": "temporarily_unavailable", "error_description": "リクエストが多すぎます。しばらくしてから再試行してください"}).encode()
                await send({"type": "http.response.start", "status": 429, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"60"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": body})
                return
        reset = client_ip.set(scope["client"][0] if scope["type"] == "http" and scope.get("client") else None)
        try:
            await self.app(scope, receive, send)
        finally:
            client_ip.reset(reset)


def transport_security(config: OAuthConfig) -> TransportSecuritySettings:
    """Allow the public host name next to loopback (behind a reverse proxy the Host header is the public one)."""
    netloc = config.public_netloc
    scheme = config.public_base_url.split("://", 1)[0]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[netloc, f"{netloc}:*", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[f"{scheme}://{netloc}", "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )


def build_http_app(server: MCPServer, provider: RedmineOAuthProvider, *, host: str = "127.0.0.1") -> Starlette:
    """The ASGI app: MCP endpoint at the public path, OAuth2 endpoints below it, metadata at /.well-known."""
    cfg = provider.config
    prefix = cfg.public_path
    app = server.streamable_http_app(streamable_http_path=prefix, transport_security=transport_security(cfg), host=host)
    routes = []
    limited = {"/authorize", "/register"}  # unauthenticated, create rows: per-IP limit (one bucket per endpoint)
    for r in app.router.routes:
        if isinstance(r, Route) and r.path in AS_PATHS:
            endpoint = r.endpoint
            if r.path in limited:
                endpoint = RateLimitMiddleware(endpoint, RateLimiter(cfg.rate_limit_per_minute))
            routes.append(Route(prefix + r.path, endpoint=endpoint, methods=sorted(r.methods or [])))
        elif isinstance(r, Route) and r.path == AS_METADATA_PATH:
            routes.append(r)
            # RFC 8414: for an issuer with a path, the metadata lives at /.well-known/<name>/<path>
            routes.append(Route(AS_METADATA_PATH + prefix, endpoint=r.endpoint, methods=sorted(r.methods or [])))
        else:
            routes.append(r)
    routes.append(Route(prefix + "/consent", endpoint=provider.handle_consent, methods=["GET", "POST"]))
    routes.append(Route(prefix + "/oauth/callback", endpoint=provider.handle_callback, methods=["GET"]))
    app.router.routes[:] = routes
    app.add_middleware(ServiceUnavailableMiddleware)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette):
        from .setup import start_cleanup

        stop = start_cleanup(provider)
        try:
            async with inner(a) as state:
                yield state
        finally:
            stop()
            await provider.upstream.aclose()

    app.router.lifespan_context = lifespan
    return app
