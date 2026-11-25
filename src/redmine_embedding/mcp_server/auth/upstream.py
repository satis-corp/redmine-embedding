"""The upstream authorization server: Redmine (Doorkeeper).

Calls /oauth/authorize (redirect), /oauth/token (code exchange, refresh),
/oauth/introspect and /oauth/revoke, authenticated as our one confidential
OAuth app. Error handling rules:

* nothing from a response body, header or request is put into an exception
  message or a log line (they can hold tokens / the client secret);
* "the token / grant is not valid" (``UpstreamRejected``) and "we cannot tell"
  (``UpstreamUnavailable``: network, 5xx, our own app credentials refused) are
  different exceptions, so an outage is never treated as an invalid token.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from .config import OAuthConfig

logger = logging.getLogger(__name__)


class UpstreamUnavailable(Exception):
    """Redmine did not give a usable answer (HTTP layer: 503)."""


class UpstreamRejected(Exception):
    """Redmine says the grant / token is invalid (expired code, revoked refresh token, ...)."""


@dataclass(frozen=True)
class UpstreamTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntrospectionResult:
    scopes: tuple[str, ...]
    exp: float | None = None
    sub: str | None = None
    client_id: str | None = None


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class UpstreamOAuth:
    def __init__(self, config: OAuthConfig, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.time) -> None:
        self._cfg = config
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=config.redmine_url,
            timeout=config.upstream_timeout,
            transport=transport,
            follow_redirects=False,
            auth=(config.upstream_client_id, config.upstream_client_secret),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def authorize_url(self, state: str, code_challenge: str) -> str:
        q = urlencode(
            {
                "response_type": "code",
                "client_id": self._cfg.upstream_client_id,
                "redirect_uri": self._cfg.callback_url,
                "scope": " ".join(self._cfg.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._cfg.redmine_url}/oauth/authorize?{q}"

    async def _post(self, path: str, data: dict[str, str]) -> httpx.Response:
        try:
            return await self._http.post(path, data=data)
        except httpx.HTTPError as e:
            logger.warning("upstream %s failed: %s", path, type(e).__name__)
            raise UpstreamUnavailable(f"upstream {path} unreachable") from None

    def _check_status(self, path: str, resp: httpx.Response) -> None:
        """Raise for everything but a 2xx / a 400 (the caller decides on 400)."""
        if resp.status_code >= 500:
            logger.warning("upstream %s returned %d", path, resp.status_code)
            raise UpstreamUnavailable(f"upstream {path} returned {resp.status_code}")
        if resp.status_code in (401, 403):
            # our own app credentials were refused (misconfiguration): not the user's fault
            logger.error("upstream %s refused the OAuth app credentials (%d); check REDMINE_OAUTH_CLIENT_ID/SECRET and the app", path, resp.status_code)
            raise UpstreamUnavailable(f"upstream {path} refused the app credentials")

    def _parse_tokens(self, resp: httpx.Response) -> UpstreamTokens:
        try:
            body = resp.json()
            access = body["access_token"]
            if not isinstance(access, str) or not access:
                raise ValueError
            refresh = body.get("refresh_token")
            expires_in = body.get("expires_in")
            created_at = body.get("created_at")
            scope = body.get("scope") or ""
        except (ValueError, KeyError, TypeError):
            logger.warning("upstream token response was not understood")
            raise UpstreamUnavailable("upstream token response was not understood") from None
        base = float(created_at) if isinstance(created_at, (int, float)) else self._clock()
        expires_at = base + float(expires_in) if isinstance(expires_in, (int, float)) else None
        return UpstreamTokens(access, refresh if isinstance(refresh, str) and refresh else None, expires_at, tuple(str(scope).split()))

    async def exchange_code(self, code: str, code_verifier: str) -> UpstreamTokens:
        resp = await self._post(
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._cfg.callback_url,
                "code_verifier": code_verifier,
            },
        )
        self._check_status("/oauth/token", resp)
        if resp.status_code != 200:
            raise UpstreamRejected("upstream rejected the authorization code")
        return self._parse_tokens(resp)

    async def refresh(self, refresh_token: str) -> UpstreamTokens:
        resp = await self._post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": refresh_token})
        self._check_status("/oauth/token", resp)
        if resp.status_code != 200:
            raise UpstreamRejected("upstream rejected the refresh token")
        return self._parse_tokens(resp)

    async def introspect(self, token: str) -> IntrospectionResult | None:
        """None = not active. ``UpstreamUnavailable`` = could not be decided."""
        resp = await self._post("/oauth/introspect", {"token": token})
        self._check_status("/oauth/introspect", resp)
        if resp.status_code != 200:
            logger.warning("upstream introspect returned %d", resp.status_code)
            raise UpstreamUnavailable("upstream introspect returned an unexpected status")
        try:
            body = resp.json()
            if not isinstance(body, dict) or not isinstance(body.get("active"), bool):
                raise ValueError
        except ValueError:
            logger.warning("upstream introspect response was not understood")
            raise UpstreamUnavailable("upstream introspect response was not understood") from None
        if not body["active"]:
            return None
        exp = body.get("exp")
        sub = body.get("sub")
        cid = body.get("client_id")
        return IntrospectionResult(
            scopes=tuple(str(body.get("scope") or "").split()),
            exp=float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None,
            sub=str(sub) if sub is not None else None,
            client_id=str(cid) if cid is not None else None,
        )

    async def revoke(self, token: str) -> None:
        """Best effort: a failure is logged (type only) and ignored."""
        try:
            resp = await self._http.post("/oauth/revoke", data={"token": token})
            if resp.status_code >= 400:  # status only: no body, no token
                logger.warning("upstream revoke returned %d", resp.status_code)
        except httpx.HTTPError as e:
            logger.warning("upstream revoke failed: %s", type(e).__name__)
