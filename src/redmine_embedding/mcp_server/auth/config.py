"""OAuth2 configuration of the MCP server.

Non-secret items come from ``Settings`` (settings file). The secrets come from
environment variables only and are never put into a log line, an exception
message or ``repr``:

  (set them in an env file outside git, e.g. ~/.reenv, passed via EnvironmentFile or export;
   the code does not depend on the file name)

  REDMINE_OAUTH_CLIENT_ID      client id of the upstream Redmine OAuth app (confidential)
  REDMINE_OAUTH_CLIENT_SECRET  its secret
  MCP_OAUTH_ENCRYPTION_KEY     Fernet key(s) that encrypt the stored upstream tokens
                               (comma separated; the first one encrypts, all decrypt = key rotation).
                               Create one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ...common.config import Settings

ENV_CLIENT_ID = "REDMINE_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "REDMINE_OAUTH_CLIENT_SECRET"
ENV_ENCRYPTION_KEY = "MCP_OAUTH_ENCRYPTION_KEY"

#: Not settings: protocol-level lifetimes (暫定).
AUTH_REQUEST_TTL = 600.0  # state of an in-flight authorization (user is on the Redmine login page)
CODE_TTL = 300.0  # our authorization code, until the client exchanges it
MAX_REDIRECT_URIS = 5  # per DCR client (暫定)
BINDING_COOKIE = "__Host-mcp_oauth_binding"  # Secure, HttpOnly, SameSite=Lax, Path=/ : binds consent page -> callback to one browser
UNUSED_CLIENT_WINDOW = 86400.0  # a client without a token counts against the caps only this long after it was used (暫定)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


class OAuthConfigError(RuntimeError):
    """Missing / invalid OAuth configuration. Names the item, never its value."""


@dataclass(frozen=True)
class OAuthConfig:
    public_base_url: str
    public_path: str
    redmine_url: str
    scopes: tuple[str, ...]
    upstream_client_id: str = field(repr=False)
    upstream_client_secret: str = field(repr=False)
    encryption_key: str = field(repr=False)
    introspect_cache_ttl: float = 30.0
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    client_retention_days: int = 90
    cleanup_apply: bool = False
    cleanup_interval: int = 3600
    max_clients: int = 1000
    max_pending_requests: int = 1000
    max_clients_per_ip: int = 50
    refresh_grace_seconds: int = 30
    max_pending_per_ip: int = 20
    rate_limit_per_minute: int = 20
    upstream_timeout: float = 10.0

    @property
    def issuer_url(self) -> str:
        """Also the resource URL: the MCP endpoint is ``public_base_url + public_path``."""
        return self.public_base_url + self.public_path

    @property
    def resource_url(self) -> str:
        return self.issuer_url

    @property
    def consent_url(self) -> str:
        return self.issuer_url + "/consent"

    @property
    def callback_url(self) -> str:
        return self.issuer_url + "/oauth/callback"

    @property
    def public_netloc(self) -> str:
        return urlsplit(self.public_base_url).netloc

    @classmethod
    def from_settings(cls, settings: Settings, environ: Mapping[str, str] | None = None) -> OAuthConfig:
        env = os.environ if environ is None else environ
        missing = [n for n in (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_ENCRYPTION_KEY) if not env.get(n, "").strip()]
        if not settings.mcp_public_base_url:
            raise OAuthConfigError("mcp_public_base_url が未設定です（OAuth2 を使う場合に設定します）")
        if not settings.redmine_url:
            raise OAuthConfigError("REDMINE_URL が必要です")
        if missing:
            raise OAuthConfigError("環境変数が未設定です: " + ", ".join(missing))
        return cls(
            public_base_url=settings.mcp_public_base_url.rstrip("/"),
            public_path=settings.mcp_public_path,
            redmine_url=settings.redmine_url.rstrip("/"),
            scopes=tuple(settings.mcp_oauth_scopes),
            upstream_client_id=env[ENV_CLIENT_ID].strip(),
            upstream_client_secret=env[ENV_CLIENT_SECRET].strip(),
            encryption_key=env[ENV_ENCRYPTION_KEY].strip(),
            introspect_cache_ttl=settings.mcp_oauth_introspect_cache_ttl,
            access_token_ttl=settings.mcp_oauth_access_token_ttl,
            refresh_token_ttl=settings.mcp_oauth_refresh_token_ttl,
            client_retention_days=settings.mcp_oauth_client_retention_days,
            cleanup_apply=settings.mcp_oauth_cleanup_apply,
            cleanup_interval=settings.mcp_oauth_cleanup_interval,
            max_clients=settings.mcp_oauth_max_clients,
            max_pending_requests=settings.mcp_oauth_max_pending_requests,
            max_clients_per_ip=settings.mcp_oauth_max_clients_per_ip,
            refresh_grace_seconds=settings.mcp_oauth_refresh_grace_seconds,
            max_pending_per_ip=settings.mcp_oauth_max_pending_per_ip,
            rate_limit_per_minute=settings.mcp_oauth_rate_limit_per_minute,
            upstream_timeout=settings.mcp_redmine_timeout,
        )
