"""OAuth2 authorization server + resource-server verification.

This server is the authorization server for MCP clients (DCR, authorization
code + PKCE, refresh, revoke) and uses Redmine (Doorkeeper) as the upstream
identity provider through ONE confidential OAuth app. Per user it keeps a pair:

  our access/refresh token (random; stored hashed)  <->  the user's Redmine tokens (stored encrypted)

``load_access_token`` is what the MCP SDK calls for every request. It maps our
token to the user's Redmine access token, checks it by introspection (30 s
cache), refreshes it on the server side when it is no longer active, and
returns an ``AccessToken`` whose ``token`` is the Redmine token: tools call
Redmine with it (``access_token_source``). It never reaches a log or an error text.

The structure (one upstream app, own tokens mapped to upstream tokens, server-side
refresh) follows the "oauth-proxy" idea of jztan/redmine-mcp-server (MIT); no code
is taken from it (it depends on fastmcp).
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import AUTH_REQUEST_TTL, BINDING_COOKIE, CODE_TTL, LOOPBACK_HOSTS, MAX_REDIRECT_URIS, UNUSED_CLIENT_WINDOW, OAuthConfig
from .context import client_ip
from .crypto import DecryptError, TokenCipher, hash_token
from .store import AuthRequest, CodeRecord, OAuthStore, TokenRecord
from .upstream import UpstreamOAuth, UpstreamRejected, UpstreamTokens, pkce_challenge
from .verifier import IntrospectionVerifier

logger = logging.getLogger(__name__)

_CLIENT_ERRORS = {"access_denied", "temporarily_unavailable", "server_error", "invalid_request", "invalid_scope"}


class UserAccessToken(AccessToken):
    """``token`` = the user's Redmine access token (kept out of ``repr``). ``row_id`` links back to our token row."""

    token: str = Field(repr=False)
    row_id: int | None = None


class UserRefreshToken(RefreshToken):
    token: str = Field(repr=False)
    row_id: int | None = None


def redirect_uri_problem(uri: object) -> str | None:
    """DCR policy: https, or http on a loopback host; no fragment. None = acceptable."""
    u = urlsplit(str(uri))
    if u.fragment:
        return "redirect_uri must not have a fragment"
    if u.scheme == "https" and u.hostname:
        return None
    if u.scheme == "http" and u.hostname and (f"[{u.hostname}]" if ":" in u.hostname else u.hostname) in LOOPBACK_HOSTS:
        return None
    return "redirect_uri must be https, or http on 127.0.0.1 / localhost / [::1]"


@dataclass
class _Tokens:
    row: TokenRecord
    upstream: str  # decrypted Redmine access token (memory only)


class RedmineOAuthProvider:
    """Implements ``OAuthAuthorizationServerProvider`` of the MCP SDK."""

    def __init__(
        self,
        config: OAuthConfig,
        store: OAuthStore,
        cipher: TokenCipher,
        upstream: UpstreamOAuth,
        verifier: IntrospectionVerifier,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.cipher = cipher
        self.upstream = upstream
        self.verifier = verifier
        self._clock = clock
        self._refresh_lock = asyncio.Lock()

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # --- clients (DCR) -------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        enc = await self._db(self.store.get_client, client_id)
        if enc is None:
            return None
        try:
            client = OAuthClientInformationFull.model_validate_json(self.cipher.decrypt(enc))
        except DecryptError:
            logger.error("a stored client record could not be decrypted (was the encryption key changed?)")
            return None
        # Clients registered before the default scopes changed keep the old scope list. The SDK checks the requested
        # scope against it (validate_scope), so re-authorizing with the new scopes would fail with invalid_scope.
        # Authorization always uses config.scopes regardless of the client's request, so this widens nothing.
        client.scope = " ".join(self.config.scopes)
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id missing")
        uris = client_info.redirect_uris or []
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri", f"between 1 and {MAX_REDIRECT_URIS} redirect_uris are required")
        for uri in uris:
            problem = redirect_uri_problem(uri)
            if problem:
                raise RegistrationError("invalid_redirect_uri", problem)
        now = self._clock()
        # Only recent clients without a token count (expired / long-idle rows never block a registration),
        # in total and per source IP, so a few addresses cannot use up everyone's quota.
        cutoff, src = now - UNUSED_CLIENT_WINDOW, client_ip.get()
        unused = await self._db(self.store.count_unused_clients, now, cutoff)
        mine = await self._db(self.store.count_unused_clients, now, cutoff, src) if src else 0
        if unused >= self.config.max_clients or mine >= self.config.max_clients_per_ip:
            logger.warning("client registration refused: %d unused clients (limit %d), %d from this source (limit %d)",
                           unused, self.config.max_clients, mine, self.config.max_clients_per_ip)
            raise RegistrationError("invalid_client_metadata", "too many registered clients; try again later")
        enc = self.cipher.encrypt(client_info.model_dump_json())
        await self._db(self.store.save_client, client_info.client_id, enc, now, src)

    # --- authorization: consent page (here) -> Redmine -> callback ------------------------
    # The MCP spec (confused deputy): before anything is forwarded to Redmine, the user is
    # shown which client / redirect URI asks for access and must approve. The approval, the
    # state sent to Redmine and the callback are tied to one browser by a cookie.
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = self._clock()
        src = client_ip.get()
        total = await self._db(self.store.count_pending_requests, now)  # unexpired rows only
        mine = await self._db(self.store.count_pending_requests, now, src) if src else 0
        if total >= self.config.max_pending_requests or mine >= self.config.max_pending_per_ip:
            logger.warning("authorization refused: %d pending requests (limit %d), %d from this source (limit %d)",
                           total, self.config.max_pending_requests, mine, self.config.max_pending_per_ip)
            raise AuthorizeError("temporarily_unavailable", "too many pending authorizations; try again later")
        request_id = secrets.token_urlsafe(32)
        await self._db(
            self.store.save_auth_request,
            AuthRequest(
                request_hash=hash_token(request_id),
                client_id=client.client_id or "",
                redirect_uri=str(params.redirect_uri),
                redirect_uri_explicit=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                client_state=params.state,
                scopes=self.config.scopes,
                resource=params.resource,
                expires_at=now + AUTH_REQUEST_TTL,
                source=src,
            ),
        )
        return construct_redirect_uri(self.config.consent_url, request_id=request_id)

    def _page(self, body: str, status: int = 200, form_targets: tuple[str, ...] = ()) -> HTMLResponse:
        # form-action also covers the redirect that answers the POST: allow exactly where it goes
        # (Redmine on approval, the client's redirect URI on denial).
        form_action = " ".join(("'self'", *form_targets)) if form_targets else "'none'"
        return HTMLResponse(
            "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            "<title>アクセスの承認</title><style>body{font:16px sans-serif;max-width:36em;margin:2em auto;padding:0 1em}"
            "code{word-break:break-all}button{font-size:1em;padding:.5em 1.5em;margin-right:1em}</style></head><body>"
            + body + "</body></html>",
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",  # clickjacking
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action {form_action}",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    def _expired_page(self) -> HTMLResponse:
        return self._page("<p>認可リクエストが無効か期限切れです。クライアントからやり直してください。</p>", 400)

    async def handle_consent(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._consent_decision(request)
        request_id = request.query_params.get("request_id", "")
        now = self._clock()
        req = await self._db(self.store.get_auth_request, hash_token(request_id), now) if request_id else None
        if req is None or req.state_hash is not None:
            return self._expired_page()
        client = await self.get_client(req.client_id)
        if client is None:
            return self._expired_page()
        # The first browser to open the page is bound; a reload / second tab of that browser reuses
        # its cookie (the CSRF token is derived from it), anyone else is refused.
        presented = request.cookies.get(BINDING_COOKIE, "")
        new_cookie = req.binder_hash is None
        if new_cookie:
            binder = secrets.token_urlsafe(32)
            if not await self._db(self.store.bind_auth_request, hash_token(request_id), hash_token(binder),
                                  hash_token(self._csrf(binder, request_id)), now):
                return self._expired_page()
        elif presented and hmac.compare_digest(req.binder_hash, hash_token(presented)):
            binder = presented
        else:
            return self._expired_page()
        csrf = self._csrf(binder, request_id)
        esc = html.escape
        name = esc((client.client_name or "（名前なし）")[:100])
        body = (
            f"<h1>アクセスの承認</h1><p>次のアプリが、あなたの Redmine アカウントの権限で検索サーバーを利用しようとしています。"
            f"心当たりがなければ「拒否」を選んでください。</p><ul><li>アプリ名: <b>{name}</b>（アプリが自己申告した名前です）</li>"
            f"<li>戻り先 URL: <code>{esc(req.redirect_uri)}</code></li><li>権限: {esc(' '.join(req.scopes))}</li></ul>"
            f"<form method=\"post\" action=\"{esc(self.config.consent_url)}\">"
            f"<input type=\"hidden\" name=\"request_id\" value=\"{esc(request_id)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{esc(csrf)}\">"
            f"<button name=\"action\" value=\"approve\">承認して Redmine にログイン</button>"
            f"<button name=\"action\" value=\"deny\">拒否</button></form>"
        )
        resp = self._page(body, form_targets=(self._origin(self.config.redmine_url), self._origin(req.redirect_uri)))
        if new_cookie:
            resp.set_cookie(BINDING_COOKIE, binder, max_age=int(AUTH_REQUEST_TTL), secure=True, httponly=True, samesite="lax", path="/")
        return resp

    @staticmethod
    def _csrf(binder: str, request_id: str) -> str:
        """CSRF token of the consent form: derived from the (HttpOnly) cookie, so it is stable across reloads."""
        return hmac.new(binder.encode(), b"csrf:" + request_id.encode(), "sha256").hexdigest()

    @staticmethod
    def _origin(url: str) -> str:
        u = urlsplit(url)
        return f"{u.scheme}://{u.netloc}"

    async def _consent_decision(self, request: Request) -> Response:
        form = await request.form()
        request_id, csrf, action = (str(form.get(k, "")) for k in ("request_id", "csrf", "action"))
        binder = request.cookies.get(BINDING_COOKIE, "")
        now = self._clock()
        if not (request_id and csrf and binder) or action not in ("approve", "deny"):
            return self._expired_page()
        req_hash = hash_token(request_id)
        if action == "deny":
            req = await self._db(self.store.get_auth_request, req_hash, now)
            if (req is None or req.state_hash is not None or req.csrf_hash is None or req.binder_hash is None
                    or not hmac.compare_digest(req.csrf_hash, hash_token(csrf))
                    or not hmac.compare_digest(req.binder_hash, hash_token(binder))):
                return self._expired_page()
            await self._db(self.store.delete_auth_request, req_hash)
            return RedirectResponse(construct_redirect_uri(req.redirect_uri, state=req.client_state, error="access_denied"),
                                    status_code=303, headers={"Cache-Control": "no-store"})
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        # atomic: only the request that matches request id + CSRF token + browser cookie, and was not approved before
        req = await self._db(self.store.approve_auth_request, req_hash, hash_token(csrf), hash_token(binder), hash_token(state),
                             self.cipher.encrypt(verifier), now)
        if req is None:
            return self._expired_page()
        await self._db(self.store.touch_client, req.client_id, now)
        return RedirectResponse(self.upstream.authorize_url(state, pkce_challenge(verifier)), status_code=303, headers={"Cache-Control": "no-store"})

    async def handle_callback(self, request: Request) -> Response:
        """Redmine redirects the user's browser here after login / consent."""
        state = request.query_params.get("state")
        req = await self._db(self.store.pop_auth_request, hash_token(state), self._clock()) if state else None
        # The browser that approved is the one that comes back (cookie), otherwise a forwarded
        # authorize URL could be completed by someone else.
        binder = request.cookies.get(BINDING_COOKIE, "")
        if (req is None or not binder or req.binder_hash is None or req.upstream_verifier_enc is None
                or not hmac.compare_digest(req.binder_hash, hash_token(binder))):
            return PlainTextResponse("認可リクエストが無効か期限切れです。クライアントからやり直してください。", status_code=400, headers={"Cache-Control": "no-store"})

        def back(**params: str) -> Response:
            return RedirectResponse(
                construct_redirect_uri(req.redirect_uri, state=req.client_state, **params), status_code=302, headers={"Cache-Control": "no-store"}
            )

        err = request.query_params.get("error")
        code = request.query_params.get("code")
        if err or not code:
            return back(error=err if err in _CLIENT_ERRORS else ("access_denied" if err else "invalid_request"))
        try:
            verifier = self.cipher.decrypt(req.upstream_verifier_enc)
            tokens = await self.upstream.exchange_code(code, verifier)
        except (UpstreamRejected, DecryptError):
            return back(error="access_denied", error_description="Redmine への認可を完了できませんでした")
        except Exception as e:  # Redmine outage etc.: tell the client, not the browser's JSON
            logger.warning("authorization code exchange failed: %s", type(e).__name__)
            return back(error="temporarily_unavailable", error_description="Redmine に接続できません")
        our_code = secrets.token_urlsafe(32)
        await self._db(
            self.store.save_code,
            CodeRecord(
                code_hash=hash_token(our_code),
                client_id=req.client_id,
                redirect_uri=req.redirect_uri,
                redirect_uri_explicit=req.redirect_uri_explicit,
                code_challenge=req.code_challenge,
                scopes=req.scopes,
                resource=req.resource,
                upstream_access_enc=self.cipher.encrypt(tokens.access_token),
                upstream_refresh_enc=self.cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None,
                upstream_expires_at=tokens.expires_at,
                expires_at=self._clock() + CODE_TTL,
            ),
        )
        return back(code=our_code)

    # --- authorization code -> tokens ----------------------------------------
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        rec = await self._db(self.store.get_code, hash_token(authorization_code), self._clock())
        if rec is None or rec.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(rec.scopes),
            expires_at=rec.expires_at,
            client_id=rec.client_id,
            code_challenge=rec.code_challenge,
            redirect_uri=rec.redirect_uri,  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=rec.redirect_uri_explicit,
            resource=rec.resource,
        )

    def _issue(self, now: float) -> tuple[str, str, float, float]:
        return (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            now + self.config.access_token_ttl,
            now + self.config.refresh_token_ttl,
        )

    @staticmethod
    def _response(access: str, refresh: str, ttl: int, scopes: tuple[str, ...]) -> OAuthToken:
        return OAuthToken(access_token=access, expires_in=ttl, refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        now = self._clock()
        rec = await self._db(self.store.pop_code, hash_token(authorization_code.code), now)  # single use
        if rec is None or rec.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code does not exist")
        access, refresh, a_exp, r_exp = self._issue(now)
        await self._db(
            self.store.create_token,
            TokenRecord(
                access_hash=hash_token(access),
                refresh_hash=hash_token(refresh),
                client_id=rec.client_id,
                scopes=rec.scopes,
                resource=rec.resource,
                upstream_access_enc=rec.upstream_access_enc,
                upstream_refresh_enc=rec.upstream_refresh_enc,
                upstream_expires_at=rec.upstream_expires_at,
                access_expires_at=a_exp,
                refresh_expires_at=r_exp,
            ),
        )
        await self._db(self.store.touch_client, rec.client_id, now)
        return self._response(access, refresh, self.config.access_token_ttl, rec.scopes)

    # --- refresh ---------------------------------------------------------------
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        now = self._clock()
        row = await self._db(self.store.get_token_by_refresh, hash_token(refresh_token), now)
        if row is None:
            # A refresh token the grant has already used up (any earlier one, not only the last) is
            # presented again: it leaked
            # (or the client lost the answer). Revoke the whole grant so a thief's new tokens die too.
            hit = await self._db(self.store.get_token_by_used_refresh, hash_token(refresh_token), now)
            if hit is not None and hit[0].client_id == client.client_id:
                old, used_at = hit
                if now - used_at <= self.config.refresh_grace_seconds:
                    # A retry / parallel refresh right after the rotation (lost answer): not an attack
                    # signal. The new pair cannot be sent again (only hashes are stored), so answer
                    # invalid_grant but keep the grant; the client that got the new pair continues.
                    logger.info("refresh token presented again within the grace period (client %s): grant kept", old.client_id)
                else:
                    logger.warning("refresh token reuse detected (client %s): grant revoked", old.client_id)
                    await self._drop(old)
            return None
        if row.client_id != client.client_id:
            return None
        return UserRefreshToken(token=refresh_token, client_id=row.client_id, scopes=list(row.scopes),
                                expires_at=int(row.refresh_expires_at), resource=row.resource, row_id=row.id)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        now = self._clock()
        old_hash = hash_token(refresh_token.token)
        row = await self._db(self.store.get_token_by_refresh, old_hash, now)
        if row is None or row.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token does not exist")
        if scopes and not set(scopes) <= set(row.scopes):
            raise TokenError("invalid_scope", "requested scope exceeds the granted scope")
        # The upstream token is refreshed too (a refresh is the moment to keep it fresh); an
        # upstream outage propagates as UpstreamUnavailable (HTTP 503), not as "invalid".
        if await self._refresh_upstream(row, force=True) is None:
            raise TokenError("invalid_grant", "the authorization at Redmine is no longer valid; sign in again")
        access, refresh, a_exp, r_exp = self._issue(now)
        new = TokenRecord(hash_token(access), hash_token(refresh), row.client_id, row.scopes, row.resource,
                          "", None, None, a_exp, r_exp)
        assert row.id is not None
        if not await self._db(self.store.rotate_token, row.id, old_hash, new, now):
            raise TokenError("invalid_grant", "refresh token was already used")
        await self._db(self.store.touch_client, row.client_id, now)
        return self._response(access, refresh, self.config.access_token_ttl, row.scopes)

    async def _refresh_upstream(self, row: TokenRecord, *, force: bool) -> TokenRecord | None:
        """Refresh the user's Redmine token. None = it cannot be refreshed (row deleted)."""
        assert row.id is not None
        async with self._refresh_lock:
            cur = await self._db(self.store.get_token, row.id, self._clock())
            if cur is None:
                return None
            if not force and cur.upstream_access_enc != row.upstream_access_enc:
                return cur  # another request refreshed it meanwhile
            if not cur.upstream_refresh_enc:
                await self._db(self.store.delete_token, row.id)
                return None
            try:
                tokens = await self.upstream.refresh(self.cipher.decrypt(cur.upstream_refresh_enc))
            except (UpstreamRejected, DecryptError):
                await self._db(self.store.delete_token, row.id)
                return None
            return await self._save_upstream(cur, tokens)

    async def _save_upstream(self, cur: TokenRecord, tokens: UpstreamTokens) -> TokenRecord:
        assert cur.id is not None
        a_enc = self.cipher.encrypt(tokens.access_token)
        r_enc = self.cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else cur.upstream_refresh_enc
        await self._db(self.store.update_upstream, cur.id, a_enc, r_enc, tokens.expires_at)
        try:
            self.verifier.forget(self.cipher.decrypt(cur.upstream_access_enc))
        except DecryptError:
            pass  # the cache entry just expires on its own
        fresh = await self._db(self.store.get_token, cur.id, self._clock())
        assert fresh is not None
        return fresh

    # --- verification (every MCP request) ---------------------------------------------
    async def load_access_token(self, token: str) -> AccessToken | None:
        """None = unknown / expired / revoked (HTTP 401). ``UpstreamUnavailable`` = cannot tell (HTTP 503)."""
        now = self._clock()
        row = await self._db(self.store.get_token_by_access, hash_token(token), now)
        if row is None or row.access_expires_at <= now:
            return None
        try:
            upstream_token = self.cipher.decrypt(row.upstream_access_enc)
        except DecryptError:
            logger.error("a stored token could not be decrypted (was the encryption key changed?)")
            return None
        result = await self.verifier.verify(upstream_token)
        if result is None:  # Redmine says the access token is no longer active: refresh on the server side
            refreshed = await self._refresh_upstream(row, force=False)
            if refreshed is None:
                return None
            try:
                upstream_token = self.cipher.decrypt(refreshed.upstream_access_enc)
            except DecryptError:
                return None
            result = await self.verifier.verify(upstream_token)
            if result is None:
                return None
        return UserAccessToken(
            token=upstream_token,
            client_id=row.client_id,
            scopes=list(result.scopes),
            expires_at=int(row.access_expires_at),
            resource=row.resource,
            subject=result.sub,
            row_id=row.id,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row_id = getattr(token, "row_id", None)
        if row_id is None:
            return
        row = await self._db(self.store.get_token, row_id, self._clock())
        if row is None:
            return
        await self._drop(row)

    async def _drop(self, row: TokenRecord) -> None:
        """Delete a grant and revoke its Redmine tokens (best effort)."""
        assert row.id is not None
        await self._db(self.store.delete_token, row.id)
        for enc in (row.upstream_access_enc, row.upstream_refresh_enc):
            if enc:
                try:
                    await self.upstream.revoke(self.cipher.decrypt(enc))
                except DecryptError:
                    pass

    # --- housekeeping -------------------------------------------------------------------
    async def cleanup(self) -> dict[str, int]:
        now = self._clock()
        counts = await self._db(self.store.cleanup, now, now - self.config.client_retention_days * 86400, self.config.cleanup_apply)
        mode = "deleted" if self.config.cleanup_apply else "dry-run, would delete"
        logger.info("oauth cleanup (%s): %s", mode, counts)
        return counts
