"""Shared fakes for the OAuth2 tests: a fake Redmine (Doorkeeper) as an httpx MockTransport."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs

import httpx

from redmine_embedding.common.config import Settings
from redmine_embedding.mcp_server.auth.setup import build_oauth

CLIENT_ID = "upstream-app-id-XYZ"
CLIENT_SECRET = "upstream-app-secret-SHHH"
ENC_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # valid Fernet key (urlsafe b64 of 32 bytes)
ENV = {"REDMINE_OAUTH_CLIENT_ID": CLIENT_ID, "REDMINE_OAUTH_CLIENT_SECRET": CLIENT_SECRET, "MCP_OAUTH_ENCRYPTION_KEY": ENC_KEY}
REDMINE = "https://redmine.test"
PUBLIC = "https://mcp.test"


def settings(**kw) -> Settings:
    base = dict(redmine_url=REDMINE, mcp_public_base_url=PUBLIC)
    base.update(kw)
    return Settings(_env_file=None, **base)


class FakeRedmine:
    """Handles /oauth/token, /oauth/introspect, /oauth/revoke. Tokens are numbered so tests can recognise them."""

    def __init__(self, scope="view_issues view_wiki_pages view_news view_time_entries view_files view_project", clock=time.time):
        self.scope = scope
        self.clock = clock
        self.n = 0
        self.codes: dict[str, dict] = {}  # upstream code -> {verifier-challenge...}
        self.access: dict[str, dict] = {}  # token -> {exp, scope}
        self.refresh: dict[str, bool] = {}
        self.calls: list[tuple[str, dict]] = []
        self.introspect_status = 200
        self.token_status = None  # force a status on /oauth/token
        self.expires_in = 7200
        self.raise_network = False
        self.revoke_status = 200

    def new_code(self) -> str:
        self.n += 1
        code = f"UPCODE{self.n}"
        self.codes[code] = {}
        return code

    def _issue(self) -> dict:
        self.n += 1
        a, r = f"UP-ACCESS-{self.n}", f"UP-REFRESH-{self.n}"
        self.access[a] = {"exp": self.clock() + self.expires_in, "scope": self.scope}
        self.refresh[r] = True
        return {"access_token": a, "refresh_token": r, "expires_in": self.expires_in, "scope": self.scope,
                "token_type": "Bearer", "created_at": int(self.clock())}

    def expire(self, token: str) -> None:
        self.access[token]["exp"] = self.clock() - 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectError("boom")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((request.url.path, form))
        auth = request.headers.get("authorization", "")
        import base64

        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if auth != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        path = request.url.path
        if path == "/oauth/token":
            if self.token_status:
                return httpx.Response(self.token_status, json={"error": "x"})
            if form.get("grant_type") == "authorization_code":
                if form.get("code") not in self.codes or "code_verifier" not in form:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                del self.codes[form["code"]]
                return httpx.Response(200, json=self._issue())
            if form.get("grant_type") == "refresh_token":
                if not self.refresh.pop(form.get("refresh_token", ""), False):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue())
        if path == "/oauth/introspect":
            if self.introspect_status != 200:
                return httpx.Response(self.introspect_status, text="oops")
            info = self.access.get(form.get("token", ""))
            if not info or info["exp"] <= self.clock():
                return httpx.Response(200, json={"active": False})
            return httpx.Response(200, json={"active": True, "scope": info["scope"], "exp": int(info["exp"]),
                                             "client_id": CLIENT_ID})
        if path == "/oauth/revoke":
            if self.revoke_status != 200:
                return httpx.Response(self.revoke_status, text="no")
            self.access.pop(form.get("token", ""), None)
            self.refresh.pop(form.get("token", ""), None)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def components(redmine: FakeRedmine | None = None, store=None, clock=time.time, **settings_kw):
    from redmine_embedding.mcp_server.auth.store import MemoryOAuthStore

    redmine = redmine or FakeRedmine(clock=clock)
    comp = build_oauth(settings(**settings_kw), environ=ENV, store=store or MemoryOAuthStore(), transport=redmine.transport())
    comp.provider._clock = clock
    comp.provider.verifier._clock = clock
    comp.provider.upstream._clock = clock
    return comp, redmine


def dump(obj) -> str:
    return json.dumps(obj, default=repr)
