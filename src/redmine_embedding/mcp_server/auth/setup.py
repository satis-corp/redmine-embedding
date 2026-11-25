"""Wiring of the OAuth2 pieces."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

from ...common.config import Settings
from .config import OAuthConfig
from .crypto import TokenCipher
from .provider import RedmineOAuthProvider
from .store import OAuthStore, PgOAuthStore
from .upstream import UpstreamOAuth
from .verifier import IntrospectionVerifier

logger = logging.getLogger(__name__)


@dataclass
class OAuthComponents:
    provider: RedmineOAuthProvider
    close: Callable[[], None] | None = None

    @property
    def config(self) -> OAuthConfig:
        return self.provider.config

    def auth_settings(self) -> AuthSettings:
        cfg = self.config
        return AuthSettings(
            issuer_url=cfg.issuer_url,  # type: ignore[arg-type]
            resource_server_url=cfg.resource_url,  # type: ignore[arg-type]
            required_scopes=list(cfg.scopes),
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=list(cfg.scopes), default_scopes=list(cfg.scopes)),
            revocation_options=RevocationOptions(enabled=True),
            # Tokens are ours (not bound to an RFC 8707 resource by Redmine); the provider issues them for this server only.
            validate_token_resource=False,
        )


def build_oauth(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    store: OAuthStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OAuthComponents:
    """Build from settings + environment. Without ``store`` a PostgreSQL store is opened (and its tables created)."""
    cfg = OAuthConfig.from_settings(settings, environ)
    cipher = TokenCipher(cfg.encryption_key)
    close = None
    if store is None:
        if not settings.database_url:
            from .config import OAuthConfigError

            raise OAuthConfigError("DATABASE_URL が必要です")
        from psycopg_pool import ConnectionPool

        from .schema import init_oauth_schema

        pool = ConnectionPool(settings.database_url, min_size=1, max_size=2, open=True)
        try:
            with pool.connection() as conn:
                init_oauth_schema(conn)
        except BaseException:
            pool.close()
            raise
        store, close = PgOAuthStore(pool.connection), pool.close
    upstream = UpstreamOAuth(cfg, transport=transport)
    provider = RedmineOAuthProvider(cfg, store, cipher, upstream, IntrospectionVerifier(upstream, cfg.introspect_cache_ttl))
    return OAuthComponents(provider, close)


async def cleanup_loop(provider: RedmineOAuthProvider) -> None:
    """Periodic cleanup (dry-run unless ``mcp_oauth_cleanup_apply``). Failures are logged and retried."""
    while True:
        try:
            await provider.cleanup()
        except Exception as e:
            logger.warning("oauth cleanup failed: %s", type(e).__name__)
        await asyncio.sleep(provider.config.cleanup_interval)


def start_cleanup(provider: RedmineOAuthProvider) -> Callable[[], None]:
    task = asyncio.get_running_loop().create_task(cleanup_loop(provider))

    def stop() -> None:
        task.cancel()

    return stop


__all__ = ["OAuthComponents", "build_oauth", "cleanup_loop", "start_cleanup"]
