"""OAuth2 flow end to end over HTTP: DCR, authorize, callback, token, refresh, 401/403/503, secrets."""

import base64
import re
import hashlib
import json
import logging
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")

from oauth_fakes import CLIENT_ID, CLIENT_SECRET, ENC_KEY, PUBLIC, REDMINE, FakeRedmine, components  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from redmine_embedding.mcp_server.auth.http import build_http_app  # noqa: E402
from redmine_embedding.mcp_server.auth.store import MemoryOAuthStore  # noqa: E402
from redmine_embedding.mcp_server.runtime import AppRuntime  # noqa: E402
from redmine_embedding.mcp_server.search.postfilter import FilterResult  # noqa: E402
from redmine_embedding.mcp_server.server import create_server  # noqa: E402
from oauth_fakes import settings as make_settings  # noqa: E402

REDIRECT = "http://127.0.0.1:33418/callback"
CLIENT_SECRET_DCR = {}


class Searcher:
    def __init__(self):
        self.tokens = []

    def search(self, query, *, token, **kw):
        self.tokens.append(token)
        return FilterResult()


class Env:
    def __init__(self, store=None, **kw):
        self.store = store or MemoryOAuthStore()
        self.comp, self.rm = components(store=self.store, **kw)
        self.searcher = Searcher()
        self.settings = make_settings(**kw)
        self.server = create_server(
            self.settings,
            runtime_factory=lambda s, src: AppRuntime(s, self.searcher, src),
            oauth=self.comp,
        )
        self.app = build_http_app(self.server, self.comp.provider)
        self.path = self.comp.config.public_path


@pytest.fixture
def env():
    return Env()


def client_for(env):
    return TestClient(env.app, base_url=PUBLIC, follow_redirects=False)


def after_grace(env, seconds=60):
    """Move the provider's clock past the refresh grace period (30 s)."""
    import time

    env.comp.provider._clock = lambda: time.time() + seconds


def other_browser(env):
    """A second client on the same app, no lifespan (only one client may run it): its own cookie jar."""
    return TestClient(env.app, base_url=PUBLIC, follow_redirects=False)


def register(c, env, scope=None, method="client_secret_post"):
    body = {"redirect_uris": [REDIRECT], "client_name": "t", "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    if scope:
        body["scope"] = scope
    r = c.post(env.path + "/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")


def start_authorize(c, env, reg, challenge, state="client-state", redirect=REDIRECT):
    """GET /authorize: answers with a redirect to OUR consent page (nothing goes to Redmine yet)."""
    r = c.get(env.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect,
                                               "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    assert r.status_code == 302, r.text
    return r.headers["location"]


def show_consent(c, consent_url):
    r = c.get(consent_url)
    assert r.status_code == 200, r.text
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    rid = parse_qs(urlsplit(consent_url).query)["request_id"][0]
    return r, {"request_id": rid, "csrf": m.group(1)}


def authorize(c, env, reg, challenge, state="client-state"):
    """The whole browser side up to Redmine: consent page, approve. Returns the Redmine authorize URL."""
    _, form = show_consent(c, start_authorize(c, env, reg, challenge, state))
    r = c.post(env.path + "/consent", data={**form, "action": "approve"})
    assert r.status_code == 303, r.text
    return r.headers["location"]


def login_and_callback(c, env, location, **extra):
    q = parse_qs(urlsplit(location).query)
    code = env.rm.new_code()
    params = {"state": q["state"][0], "code": code}
    params.update(extra)
    return c.get(env.path + "/oauth/callback", params=params), q


def full_login(c, env, reg=None):
    reg = reg or register(c, env)
    verifier, challenge = pkce()
    loc = authorize(c, env, reg, challenge)
    r, _ = login_and_callback(c, env, loc)
    assert r.status_code == 302
    back = parse_qs(urlsplit(r.headers["location"]).query)
    assert back["state"] == ["client-state"]
    tok = c.post(env.path + "/token", data={"grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": REDIRECT,
                                            "client_id": reg["client_id"], "client_secret": reg["client_secret"], "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return reg, tok.json()


def mcp_call(c, env, token, name="search_redmine", args=None):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = c.post(env.path, json=init, headers=h)
    assert r.status_code == 200, r.text
    h["mcp-session-id"] = r.headers["mcp-session-id"]
    c.post(env.path, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {"query": "x"}}}
    r = c.post(env.path, json=call, headers=h)
    assert r.status_code == 200, r.text
    data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
    return json.loads(data[-1]) if data else r.json()


def test_metadata_advertises_endpoints_under_the_public_path(env):
    with client_for(env) as c:
        for url in ("/.well-known/oauth-authorization-server" + env.path, "/.well-known/oauth-authorization-server"):
            m = c.get(url).json()
            assert m["issuer"] == PUBLIC + "/mcp-search"
            assert m["authorization_endpoint"] == PUBLIC + "/mcp-search/authorize"
            assert m["token_endpoint"] == PUBLIC + "/mcp-search/token"
            assert m["registration_endpoint"] == PUBLIC + "/mcp-search/register"
            assert "S256" in m["code_challenge_methods_supported"]
        pr = c.get("/.well-known/oauth-protected-resource/mcp-search").json()
        assert pr["resource"] == PUBLIC + "/mcp-search" and pr["authorization_servers"] == [PUBLIC + "/mcp-search"]
        assert pr["scopes_supported"] == ["view_issues", "view_wiki_pages", "view_news", "view_time_entries", "view_files", "view_project"]
        # the SDK's root-level copies are gone: everything is served below the public path
        assert c.post("/token").status_code == 404 and c.post("/register").status_code == 404


def test_host_and_path_come_from_settings():
    e = Env(mcp_public_base_url="https://elsewhere.example", mcp_public_path="/search")
    with client_for(e) as c:
        m = c.get("/.well-known/oauth-authorization-server/search").json()
        assert m["authorization_endpoint"] == "https://elsewhere.example/search/authorize"
        assert c.get("/.well-known/oauth-protected-resource/search").status_code == 200
        r = c.post("/search", json={})
        assert r.status_code == 401
        assert 'resource_metadata="https://elsewhere.example/.well-known/oauth-protected-resource/search"' in r.headers["www-authenticate"]
        assert c.post("/mcp-search", json={}).status_code == 404


def test_dcr_stores_the_client_encrypted(env):
    with client_for(env) as c:
        reg = register(c, env)
        assert reg["client_id"] and reg["client_secret"]
        stored = env.store.clients[reg["client_id"]][0]
        assert reg["client_secret"] not in stored and "127.0.0.1" not in stored  # encrypted blob, not JSON
        # unknown redirect uri is refused by the SDK at /authorize
        _, ch = pkce()
        r = c.get(env.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "http://evil.example/cb",
                                                    "code_challenge": ch, "code_challenge_method": "S256"})
        assert r.status_code == 400


def test_authorize_redirects_to_redmine_with_pkce_and_our_callback(env):
    with client_for(env) as c:
        reg = register(c, env)
        _, ch = pkce()
        loc = authorize(c, env, reg, ch)
        u = urlsplit(loc)
        q = parse_qs(u.query)
        assert f"{u.scheme}://{u.netloc}{u.path}" == REDMINE + "/oauth/authorize"
        assert q["client_id"] == [CLIENT_ID] and q["response_type"] == ["code"]
        assert q["redirect_uri"] == [PUBLIC + "/mcp-search/oauth/callback"]
        assert q["scope"] == ["view_issues view_wiki_pages view_news view_time_entries view_files view_project"] and q["code_challenge_method"] == ["S256"]
        assert q["state"] != ["client-state"] and q["code_challenge"] != [ch]  # own state / own PKCE towards Redmine
        assert CLIENT_SECRET not in loc


def test_full_flow_and_tool_gets_the_users_redmine_token(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        assert tok["token_type"] == "Bearer" and tok["refresh_token"] and tok["expires_in"] == 3600
        assert tok["access_token"] not in env.rm.access  # ours, not Redmine's
        res = mcp_call(c, env, tok["access_token"])
        assert "error" not in res and not res["result"].get("isError"), res
        assert len(env.searcher.tokens) == 1
        used = env.searcher.tokens[0]
        assert used in env.rm.access and used != tok["access_token"]  # the user's Redmine token reaches the tool


def test_code_is_single_use_and_bound_to_client_and_pkce(env):
    with client_for(env) as c:
        reg = register(c, env)
        other = register(c, env)
        verifier, ch = pkce()
        loc = authorize(c, env, reg, ch)
        r, _ = login_and_callback(c, env, loc)
        code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": reg["client_id"],
                "client_secret": reg["client_secret"]}
        assert c.post(env.path + "/token", data={**form, "code_verifier": "wrong" * 10}).status_code == 400
        assert c.post(env.path + "/token", data={**form, "client_id": other["client_id"], "client_secret": other["client_secret"],
                                                  "code_verifier": verifier}).status_code == 400
        assert c.post(env.path + "/token", data={**form, "code_verifier": verifier}).status_code == 200
        assert c.post(env.path + "/token", data={**form, "code_verifier": verifier}).status_code == 400  # replay


def test_callback_unknown_state_and_replay(env):
    with client_for(env) as c:
        assert c.get(env.path + "/oauth/callback", params={"state": "nope", "code": "x"}).status_code == 400
        assert c.get(env.path + "/oauth/callback").status_code == 400
        reg = register(c, env)
        _, ch = pkce()
        loc = authorize(c, env, reg, ch)
        r, q = login_and_callback(c, env, loc)
        assert r.status_code == 302
        r2 = c.get(env.path + "/oauth/callback", params={"state": q["state"][0], "code": env.rm.new_code()})
        assert r2.status_code == 400  # state is single use


def test_callback_user_denied_goes_back_to_the_client(env):
    with client_for(env) as c:
        reg = register(c, env)
        _, ch = pkce()
        q = parse_qs(urlsplit(authorize(c, env, reg, ch)).query)
        r = c.get(env.path + "/oauth/callback", params={"state": q["state"][0], "error": "access_denied"})
        back = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith(REDIRECT) and back["error"] == ["access_denied"] and back["state"] == ["client-state"]


def test_callback_redmine_rejects_or_is_down(env):
    with client_for(env) as c:
        reg = register(c, env)
        _, ch = pkce()
        q = parse_qs(urlsplit(authorize(c, env, reg, ch)).query)
        r = c.get(env.path + "/oauth/callback", params={"state": q["state"][0], "code": "not-a-valid-code"})
        assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]
        q = parse_qs(urlsplit(authorize(c, env, reg, ch)).query)
        env.rm.token_status = 503
        r = c.get(env.path + "/oauth/callback", params={"state": q["state"][0], "code": env.rm.new_code()})
        assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["temporarily_unavailable"]


def test_no_token_and_invalid_token_are_401_with_resource_metadata(env):
    with client_for(env) as c:
        for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}):
            r = c.post(env.path, json={}, headers=headers)
            assert r.status_code == 401
            www = r.headers["www-authenticate"]
            assert www.startswith("Bearer ") and 'error="invalid_token"' in www
            assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp-search"' in www
        assert "nonsense" not in r.text


def test_insufficient_scope_is_403(env):
    env.rm.scope = "view_issues"  # the user granted less than required
    with client_for(env) as c:
        _, tok = full_login(c, env)
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 403
        assert 'error="insufficient_scope"' in r.headers["www-authenticate"] and "resource_metadata=" in r.headers["www-authenticate"]


def test_token_with_old_two_scopes_is_403_until_reauthorized(env):
    # the default scopes grew to five; a token granted only the old two must be re-authorized.
    env.rm.scope = "view_issues view_wiki_pages"
    with client_for(env) as c:
        _, tok = full_login(c, env)
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 403 and 'error="insufficient_scope"' in r.headers["www-authenticate"]


OLD_SCOPES = ["view_issues", "view_wiki_pages"]
NEW_SCOPES = "view_issues view_wiki_pages view_news view_time_entries view_files view_project"


def test_client_registered_with_old_scopes_can_reauthorize_with_new_scopes():
    # a DCR client registered under the old 2-scope default must reach consent when it asks for 6 scopes.
    old = Env(mcp_oauth_scopes=OLD_SCOPES)
    with client_for(old) as c:
        reg = register(c, old)
    assert reg["scope"] == "view_issues view_wiki_pages"
    new = Env(store=old.store)  # same store, new (6 scope) config
    with client_for(new) as c:
        _, challenge = pkce()
        r = c.get(new.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                                   "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
                                                   "scope": NEW_SCOPES})
        assert r.status_code == 302 and "error=" not in r.headers["location"], r.headers["location"]
        page, form = show_consent(c, r.headers["location"])
        assert "view_files" in page.text and "view_project" in page.text
        r = c.post(new.path + "/consent", data={**form, "action": "approve"})
        assert r.status_code == 303 and "scope=" in r.headers["location"]
        assert set(parse_qs(urlsplit(r.headers["location"]).query)["scope"][0].split()) == set(NEW_SCOPES.split())


def test_get_client_does_not_widen_scopes_beyond_config():
    # The scope returned for a stored client is always the configured one, never what the client registered/asks for.
    e = Env()
    with client_for(e) as c:
        reg = register(c, e, scope="view_issues")
    import asyncio

    got = asyncio.run(e.comp.provider.get_client(reg["client_id"]))
    assert got.scope == NEW_SCOPES


def test_five_scope_token_is_403_under_six_scope_config():
    # a token issued with the previous 5-scope default lacks view_project and is rejected.
    five = ["view_issues", "view_wiki_pages", "view_news", "view_time_entries", "view_files"]
    old = Env(mcp_oauth_scopes=five)
    old.rm.scope = " ".join(five)
    with client_for(old) as c:
        reg, tok = full_login(c, old)
        r = c.post(old.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code != 403  # accepted under the 5-scope config
    new = Env(store=old.store)
    new.rm = old.rm
    new.comp, _ = components(redmine=old.rm, store=old.store)
    new.server = create_server(new.settings, runtime_factory=lambda s, src: AppRuntime(s, new.searcher, src), oauth=new.comp)
    new.app = build_http_app(new.server, new.comp.provider)
    with client_for(new) as c:
        r = c.post(new.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 403 and 'error="insufficient_scope"' in r.headers["www-authenticate"]


def test_old_token_and_upstream_refresh_stay_403_until_reauthorized():
    # a token issued under the old config (Redmine granted 2 scopes) is rejected by the new config,
    # also after the server refreshes the upstream token (refresh does not add scopes).
    old = Env(mcp_oauth_scopes=OLD_SCOPES)
    old.rm.scope = "view_issues view_wiki_pages"
    with client_for(old) as c:
        reg, tok = full_login(c, old)
        r = c.post(old.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code != 403  # accepted under the old config
    new = Env(store=old.store)
    new.rm = old.rm
    new.comp, _ = components(redmine=old.rm, store=old.store)
    new.server = create_server(new.settings, runtime_factory=lambda s, src: AppRuntime(s, new.searcher, src), oauth=new.comp)
    new.app = build_http_app(new.server, new.comp.provider)
    with client_for(new) as c:
        hdr = {"Authorization": f"Bearer {tok['access_token']}"}
        r = c.post(new.path, json={}, headers=hdr)
        assert r.status_code == 403 and 'error="insufficient_scope"' in r.headers["www-authenticate"]
        up_access = next(iter(old.rm.access))
        old.rm.expire(up_access)
        new.comp.provider.verifier._cache.clear()
        after_grace(new)
        r = c.post(new.path, json={}, headers=hdr)
        assert [p for p, f in old.rm.calls if p == "/oauth/token" and f.get("grant_type") == "refresh_token"]  # refreshed upstream
        assert r.status_code == 403  # still only the old scopes


def test_introspect_outage_is_503_not_401(env):
    with client_for(env) as c:
        _, tok = full_login(c, env)
        env.comp.provider.verifier._cache.clear()
        env.rm.introspect_status = 500
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 503 and r.headers["retry-after"] and r.json()["error"] == "temporarily_unavailable"
        env.rm.introspect_status = 200
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code != 503 and r.status_code != 401  # recovered; the same token still works


def test_expired_upstream_token_is_refreshed_on_the_server(env):
    with client_for(env) as c:
        _, tok = full_login(c, env)
        first = mcp_call(c, env, tok["access_token"])
        up1 = env.searcher.tokens[-1]
        env.rm.expire(up1)
        env.comp.provider.verifier._cache.clear()
        mcp_call(c, env, tok["access_token"])
        up2 = env.searcher.tokens[-1]
        assert up2 != up1 and up2 in env.rm.access
        assert [p for p, f in env.rm.calls if p == "/oauth/token" and f.get("grant_type") == "refresh_token"]


def test_revoked_upstream_refresh_means_401_and_forgets_the_row(env):
    with client_for(env) as c:
        _, tok = full_login(c, env)
        up = next(iter(env.rm.access))
        env.rm.expire(up)
        env.rm.refresh.clear()
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 401
        assert env.store.tokens == {}


def test_refresh_grant_rotates_both_tokens(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"],
                "client_secret": reg["client_secret"]}
        r = c.post(env.path + "/token", data=form)
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["access_token"] != tok["access_token"] and new["refresh_token"] != tok["refresh_token"]
        assert c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401  # old one is gone
        assert mcp_call(c, env, new["access_token"])["result"]
        assert c.post(env.path + "/token", data=form).status_code == 400  # old refresh token: single use


def test_refresh_grant_is_bound_to_the_client_and_to_scopes(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        other = register(c, env)
        r = c.post(env.path + "/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                              "client_id": other["client_id"], "client_secret": other["client_secret"]})
        assert r.status_code == 400
        r = c.post(env.path + "/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "scope": "admin",
                                              "client_id": reg["client_id"], "client_secret": reg["client_secret"]})
        assert r.status_code == 400


def test_refresh_when_redmine_is_down_is_503_and_keeps_the_grant(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        env.rm.token_status = 502
        assert c.post(env.path + "/token", data=form).status_code == 503
        env.rm.token_status = None
        assert c.post(env.path + "/token", data=form).status_code == 200  # not consumed by the outage


def test_our_access_token_expires(env):
    with client_for(env) as c:
        _, tok = full_login(c, env)
        for row_id, t in list(env.store.tokens.items()):
            from dataclasses import replace

            env.store.tokens[row_id] = replace(t, access_expires_at=1.0)
        assert c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401


def test_revoke_deletes_the_grant_and_revokes_upstream(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        up = next(iter(env.rm.access))
        r = c.post(env.path + "/revoke", data={"token": tok["refresh_token"], "token_type_hint": "refresh_token",
                                               "client_id": reg["client_id"], "client_secret": reg["client_secret"]})
        assert r.status_code == 200
        assert env.store.tokens == {} and up not in env.rm.access
        assert c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401


def test_stored_data_has_no_plaintext_secrets(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        up_access = next(iter(env.rm.access))
        up_refresh = next(iter(env.rm.refresh))
        blob = repr(env.store.tokens) + repr(env.store.codes) + repr(env.store.requests) + repr(env.store.clients)
        for secret in (up_access, up_refresh, tok["access_token"], tok["refresh_token"], reg["client_secret"], CLIENT_SECRET, ENC_KEY):
            assert secret not in blob
        row = next(iter(env.store.tokens.values()))
        assert env.comp.provider.cipher.decrypt(row.upstream_access_enc) == up_access


def test_nothing_secret_reaches_the_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        mcp_call(c, env, tok["access_token"])
        env.rm.expire(next(iter(env.rm.access)))
        env.comp.provider.verifier._cache.clear()
        mcp_call(c, env, tok["access_token"])
        env.rm.introspect_status = 500
        env.comp.provider.verifier._cache.clear()
        c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        env.rm.introspect_status = 200
        env.rm.token_status = 401  # our own app credentials refused
        c.post(env.path + "/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                          "client_id": reg["client_id"], "client_secret": reg["client_secret"]})
    secrets_ = [CLIENT_SECRET, ENC_KEY, reg["client_secret"], tok["access_token"], tok["refresh_token"]] + list(env.rm.access) + list(env.rm.refresh)
    secrets_ += [t for t in env.searcher.tokens]
    for s in secrets_:
        assert s not in caplog.text, s[:12]


def test_cleanup_is_dry_run_by_default_and_applies_only_on_request():
    import time

    e = Env()
    with client_for(e) as c:
        reg, tok = full_login(c, e)
        reg2 = register(c, e)
    now = time.time()
    e.comp.provider._clock = lambda: now + 40 * 86400  # 40 days later: the refresh token (30 d) and the codes are dead
    import asyncio

    counts = asyncio.run(e.comp.provider.cleanup())
    assert counts["tokens"] == 1 and len(e.store.tokens) == 1  # dry-run: counted, kept
    e.comp.provider.config = type(e.comp.provider.config)(**{**e.comp.provider.config.__dict__, "cleanup_apply": True})
    counts = asyncio.run(e.comp.provider.cleanup())
    assert counts["tokens"] == 1 and e.store.tokens == {}
    assert reg["client_id"] in e.store.clients  # not yet past the 90-day retention
    e.comp.provider._clock = lambda: now + 100 * 86400
    asyncio.run(e.comp.provider.cleanup())
    assert e.store.clients == {}


def test_live_token_protects_its_client_from_cleanup():
    import asyncio
    import time

    e = Env()
    with client_for(e) as c:
        reg, tok = full_login(c, e)
    e.comp.provider.config = type(e.comp.provider.config)(**{**e.comp.provider.config.__dict__, "cleanup_apply": True, "client_retention_days": 1})
    e.store.clients[reg["client_id"]][1] = 0.0  # last used long ago, but the token is alive
    asyncio.run(e.comp.provider.cleanup())
    assert reg["client_id"] in e.store.clients


# --- consent, browser binding, DCR limits, reuse detection, rate limit (review 20261105) --------

def test_authorize_alone_sends_nothing_to_redmine_and_shows_consent(env):
    with client_for(env) as c:
        reg = register(c, env)
        loc = start_authorize(c, env, reg, pkce()[1])
        assert loc.startswith(PUBLIC + "/mcp-search/consent?request_id=")  # not Redmine
        r, _ = show_consent(c, loc)
        assert env.rm.calls == [] and REDMINE not in loc
        assert "t" in r.text and REDIRECT in r.text and "view_issues" in r.text
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["cache-control"] == "no-store"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-mcp_oauth_binding=") and "Secure" in cookie and "HttpOnly" in cookie
        assert "SameSite=lax" in cookie and "Path=/" in cookie and "Domain" not in cookie


def test_consent_page_escapes_the_client_name(env):
    with client_for(env) as c:
        r = c.post(env.path + "/register", json={"redirect_uris": [REDIRECT], "client_name": "<script>alert(1)</script>",
                                                  "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        reg = r.json()
        page, _ = show_consent(c, start_authorize(c, env, reg, pkce()[1]))
        assert "<script>" not in page.text and "&lt;script&gt;" in page.text


def test_no_callback_without_consent(env):
    """Without the approval POST there is no state at all, so a callback cannot complete anything."""
    with client_for(env) as c:
        reg = register(c, env)
        loc = start_authorize(c, env, reg, pkce()[1])
        rid = parse_qs(urlsplit(loc).query)["request_id"][0]
        for state in (rid, "x"):  # even the consent request id is not a state
            r = c.get(env.path + "/oauth/callback", params={"state": state, "code": env.rm.new_code()})
            assert r.status_code == 400 and "location" not in r.headers
        assert env.store.codes == {}


def test_approval_needs_csrf_and_the_binding_cookie(env):
    with client_for(env) as c:
        reg = register(c, env)
        _, form = show_consent(c, start_authorize(c, env, reg, pkce()[1]))
        assert c.post(env.path + "/consent", data={**form, "csrf": "wrong", "action": "approve"}).status_code == 400
        other = other_browser(env)  # another browser: no cookie, then a guessed one
        assert other.post(env.path + "/consent", data={**form, "action": "approve"}).status_code == 400
        other.cookies.set("__Host-mcp_oauth_binding", "guess", domain="mcp.test")
        assert other.post(env.path + "/consent", data={**form, "action": "approve"}).status_code == 400
        assert c.post(env.path + "/consent", data={**form, "action": "maybe"}).status_code == 400
        assert c.post(env.path + "/consent", data={**form, "action": "approve"}).status_code == 303
        assert c.post(env.path + "/consent", data={**form, "action": "approve"}).status_code == 400  # approval is single use


def test_callback_from_another_browser_is_refused(env):
    """The authorize URL forwarded to a victim: their browser has no binding cookie, so nothing is issued."""
    victim = other_browser(env)
    with client_for(env) as attacker:
        reg = register(attacker, env)
        redmine_url = authorize(attacker, env, reg, pkce()[1])
        state = parse_qs(urlsplit(redmine_url).query)["state"][0]
        r = victim.get(env.path + "/oauth/callback", params={"state": state, "code": env.rm.new_code()})
        assert r.status_code == 400 and "location" not in r.headers
        assert env.store.codes == {}
        # and the state is burnt: even the real browser cannot reuse it
        r = attacker.get(env.path + "/oauth/callback", params={"state": state, "code": env.rm.new_code()})
        assert r.status_code == 400


def test_callback_with_wrong_cookie_is_refused(env):
    with client_for(env) as c:
        reg = register(c, env)
        state = parse_qs(urlsplit(authorize(c, env, reg, pkce()[1])).query)["state"][0]
        c.cookies.set("__Host-mcp_oauth_binding", "tampered", domain="mcp.test")
        r = c.get(env.path + "/oauth/callback", params={"state": state, "code": env.rm.new_code()})
        assert r.status_code == 400 and env.store.codes == {}


def test_deny_goes_back_to_the_client_and_never_to_redmine(env):
    with client_for(env) as c:
        reg = register(c, env)
        _, form = show_consent(c, start_authorize(c, env, reg, pkce()[1]))
        r = c.post(env.path + "/consent", data={**form, "action": "deny"})
        back = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith(REDIRECT) and back["error"] == ["access_denied"] and back["state"] == ["client-state"]
        assert env.rm.calls == [] and env.store.requests == {}


@pytest.mark.parametrize("uri", ["http://attacker.example/cb", "ftp://x/cb", "myapp://cb", "https://x.example/cb#frag", "javascript:alert(1)"])
def test_dcr_rejects_unsafe_redirect_uris(env, uri):
    with client_for(env) as c:
        r = c.post(env.path + "/register", json={"redirect_uris": [uri], "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert r.status_code == 400 and env.store.clients == {}


@pytest.mark.parametrize("uri", ["https://claude.ai/api/mcp/auth_callback", "http://127.0.0.1:5000/cb", "http://localhost/cb", "http://[::1]:8080/cb"])
def test_dcr_accepts_https_and_loopback(env, uri):
    with client_for(env) as c:
        r = c.post(env.path + "/register", json={"redirect_uris": [uri], "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert r.status_code == 201, r.text


def test_dcr_limits_the_number_of_redirect_uris(env):
    with client_for(env) as c:
        body = {"grant_types": ["authorization_code"], "response_types": ["code"]}
        ok = c.post(env.path + "/register", json={**body, "redirect_uris": [f"https://a.example/{i}" for i in range(5)]})
        too_many = c.post(env.path + "/register", json={**body, "redirect_uris": [f"https://a.example/{i}" for i in range(6)]})
        assert ok.status_code == 201 and too_many.status_code == 400


def test_unused_client_cap():
    e = Env(mcp_oauth_max_clients=2, mcp_oauth_rate_limit_per_minute=1000)
    with client_for(e) as c:
        register(c, e)
        register(c, e)
        r = c.post(e.path + "/register", json={"redirect_uris": [REDIRECT], "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert r.status_code == 400 and len(e.store.clients) == 2


def test_pending_authorization_cap():
    e = Env(mcp_oauth_max_pending_requests=2, mcp_oauth_rate_limit_per_minute=1000)
    with client_for(e) as c:
        reg = register(c, e)
        start_authorize(c, e, reg, pkce()[1])
        start_authorize(c, e, reg, pkce()[1])
        r = c.get(e.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                                  "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s"})
        assert r.status_code == 302 and "error=temporarily_unavailable" in r.headers["location"]
        assert len(e.store.requests) == 2


def test_rate_limit_on_register_and_authorize():
    e = Env(mcp_oauth_rate_limit_per_minute=3)
    with client_for(e) as c:
        body = {"redirect_uris": [REDIRECT], "grant_types": ["authorization_code"], "response_types": ["code"]}
        codes = [c.post(e.path + "/register", json=body).status_code for _ in range(5)]
        assert codes == [201, 201, 201, 429, 429]
        assert c.post(e.path + "/register", json=body).headers["retry-after"]
        reg = {"client_id": next(iter(e.store.clients))}
        auth = [c.get(e.path + "/authorize", params={"client_id": reg["client_id"]}).status_code for _ in range(5)]
        assert auth[:3] != [429] * 3 and auth[3:] == [429, 429]  # a separate bucket per endpoint


def test_rate_limiter_window():
    from redmine_embedding.mcp_server.auth.http import RateLimiter

    now = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    now[0] = 61
    assert rl.allow("a")


def test_refresh_token_reuse_revokes_the_whole_grant(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        new = c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]}).json()
        up_now = list(env.rm.access)[-1]
        after_grace(env)
        # the OLD refresh token shows up again (stolen copy): grant is killed, thief's new tokens die too
        r = c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        assert r.status_code == 400 and env.store.tokens == {}
        assert c.post(env.path, json={}, headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 401
        assert c.post(env.path + "/token", data={**form, "refresh_token": new["refresh_token"]}).status_code == 400
        assert up_now not in env.rm.access  # revoked at Redmine too


def test_reuse_by_another_client_does_not_revoke(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        other = register(c, env)
        c.post(env.path + "/token", data={"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"],
                                          "refresh_token": tok["refresh_token"]})
        r = c.post(env.path + "/token", data={"grant_type": "refresh_token", "client_id": other["client_id"], "client_secret": other["client_secret"],
                                              "refresh_token": tok["refresh_token"]})
        assert r.status_code == 400 and len(env.store.tokens) == 1


def test_repr_of_tokens_has_no_secret(env):
    from redmine_embedding.mcp_server.auth.provider import UserAccessToken, UserRefreshToken

    a = UserAccessToken(token="UPSTREAM-SECRET", client_id="c", scopes=["s"], row_id=1)
    r = UserRefreshToken(token="OUR-REFRESH-SECRET", client_id="c", scopes=["s"], row_id=1)
    assert "UPSTREAM-SECRET" not in repr(a) and "UPSTREAM-SECRET" not in str(a)
    assert "OUR-REFRESH-SECRET" not in repr(r)
    assert a.token == "UPSTREAM-SECRET"  # still usable


def test_undecryptable_row_is_401_not_500(env):
    from cryptography.fernet import Fernet

    from redmine_embedding.mcp_server.auth.crypto import TokenCipher

    with client_for(env) as c:
        _, tok = full_login(c, env)
        env.comp.provider.cipher = TokenCipher(Fernet.generate_key().decode())  # the key was replaced
        r = c.post(env.path, json={}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        assert r.status_code == 401


# --- second review ------------------------------------------------------------------------------

def test_reuse_is_detected_for_any_earlier_refresh_token(env):
    """A thief refreshes twice (R1 -> R2 -> R3); the owner then shows R1: the grant must still die."""
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        r1 = tok["refresh_token"]
        r2 = c.post(env.path + "/token", data={**form, "refresh_token": r1}).json()
        r3 = c.post(env.path + "/token", data={**form, "refresh_token": r2["refresh_token"]}).json()
        assert env.store.tokens  # the thief's grant is alive
        after_grace(env)
        assert c.post(env.path + "/token", data={**form, "refresh_token": r1}).status_code == 400
        assert env.store.tokens == {} and env.store.used == {}
        assert c.post(env.path, json={}, headers={"Authorization": f"Bearer {r3['access_token']}"}).status_code == 401
        assert c.post(env.path + "/token", data={**form, "refresh_token": r3["refresh_token"]}).status_code == 400


def test_expired_and_old_rows_do_not_block_registration():
    import time

    e = Env(mcp_oauth_max_clients=2, mcp_oauth_max_clients_per_ip=2, mcp_oauth_rate_limit_per_minute=1000)
    for i in range(5):  # long-idle clients that cleanup (dry-run) never removed
        e.store.save_client(f"old{i}", "E", time.time() - 5 * 86400, "1.2.3.4")
    with client_for(e) as c:
        register(c, e)
        register(c, e)
        assert c.post(e.path + "/register", json={"redirect_uris": [REDIRECT], "grant_types": ["authorization_code"],
                                                   "response_types": ["code"]}).status_code == 400  # fresh ones do count


def test_per_ip_client_cap_does_not_use_up_the_global_quota():
    e = Env(mcp_oauth_max_clients=100, mcp_oauth_max_clients_per_ip=2, mcp_oauth_rate_limit_per_minute=1000)
    body = {"redirect_uris": [REDIRECT], "grant_types": ["authorization_code"], "response_types": ["code"]}
    with client_for(e) as c:
        assert [c.post(e.path + "/register", json=body).status_code for _ in range(3)] == [201, 201, 400]
        other = TestClient(e.app, base_url=PUBLIC, client=("203.0.113.9", 4000), follow_redirects=False)
        assert other.post(e.path + "/register", json=body).status_code == 201  # another address is not locked out


def test_per_ip_pending_authorization_cap():
    e = Env(mcp_oauth_max_pending_requests=100, mcp_oauth_max_pending_per_ip=2, mcp_oauth_rate_limit_per_minute=1000)
    with client_for(e) as c:
        reg = register(c, e)
        for _ in range(2):
            start_authorize(c, e, reg, pkce()[1])
        r = c.get(e.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                                  "code_challenge": pkce()[1], "code_challenge_method": "S256"})
        assert "error=temporarily_unavailable" in r.headers["location"]
        other = TestClient(e.app, base_url=PUBLIC, client=("203.0.113.9", 4000), follow_redirects=False)
        r = other.get(e.path + "/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                                      "code_challenge": pkce()[1], "code_challenge_method": "S256"})
        assert r.headers["location"].startswith(PUBLIC + "/mcp-search/consent")


def test_expired_pending_requests_do_not_count():
    import time
    from dataclasses import replace

    e = Env(mcp_oauth_max_pending_requests=1, mcp_oauth_max_pending_per_ip=1, mcp_oauth_rate_limit_per_minute=1000)
    with client_for(e) as c:
        reg = register(c, e)
        start_authorize(c, e, reg, pkce()[1])
        for k, v in list(e.store.requests.items()):
            e.store.requests[k] = replace(v, expires_at=time.time() - 1)  # expired but still stored (dry-run cleanup)
        assert start_authorize(c, e, reg, pkce()[1]).startswith(PUBLIC)


def test_consent_reload_keeps_the_cookie_and_the_first_page_still_works(env):
    with client_for(env) as c:
        reg = register(c, env)
        loc = start_authorize(c, env, reg, pkce()[1])
        first, form = show_consent(c, loc)
        assert "set-cookie" in first.headers
        again = c.get(loc)  # reload / second tab of the same browser
        assert again.status_code == 200 and "set-cookie" not in again.headers
        assert f'value="{form["csrf"]}"' in again.text  # same CSRF token: the first tab stays valid
        assert c.post(env.path + "/consent", data={**form, "action": "approve"}).status_code == 303
    # a different browser opening the same request is refused and does not disturb the first one
    e2 = Env()
    with client_for(e2) as c:
        reg = register(c, e2)
        loc = start_authorize(c, e2, reg, pkce()[1])
        _, form = show_consent(c, loc)
        assert other_browser(e2).get(loc).status_code == 400
        assert c.post(e2.path + "/consent", data={**form, "action": "approve"}).status_code == 303


def test_consent_page_headers(env):
    with client_for(env) as c:
        reg = register(c, env)
        r, _ = show_consent(c, start_authorize(c, env, reg, pkce()[1]))
        csp = r.headers["content-security-policy"]
        assert f"form-action 'self' {REDMINE} http://127.0.0.1:33418;" in csp + ";"
        assert "frame-ancestors 'none'" in csp and r.headers["x-content-type-options"] == "nosniff"
        err = c.get(env.path + "/consent?request_id=nope")
        assert err.status_code == 400 and "form-action 'none'" in err.headers["content-security-policy"]


@pytest.mark.anyio
async def test_upstream_revoke_failure_is_logged_without_the_token(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.rm.revoke_status = 500
    await env.comp.provider.upstream.revoke("SECRET-UPSTREAM-TOKEN")
    assert "revoke returned 500" in caplog.text and "SECRET-UPSTREAM-TOKEN" not in caplog.text


def test_refresh_retry_within_the_grace_period_keeps_the_grant(env):
    """A lost answer / parallel refresh right after a rotation: invalid_grant, but nobody is logged out."""
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        new = c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]}).json()
        again = c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        assert len(env.store.tokens) == 1
        assert mcp_call(c, env, new["access_token"])["result"]  # the pair issued first still works
        assert c.post(env.path + "/token", data={**form, "refresh_token": new["refresh_token"]}).status_code == 200


def test_grace_period_ends_after_the_configured_seconds(env):
    with client_for(env) as c:
        reg, tok = full_login(c, env)
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        after_grace(env, 29)
        assert c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]}).status_code == 400
        assert env.store.tokens  # 29 s: still within the grace period
        after_grace(env, 31)
        c.post(env.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        assert env.store.tokens == {}  # 31 s: reuse, grant revoked


def test_grace_zero_means_immediate_revocation():
    e = Env(mcp_oauth_refresh_grace_seconds=0)
    with client_for(e) as c:
        reg, tok = full_login(c, e)
        form = {"grant_type": "refresh_token", "client_id": reg["client_id"], "client_secret": reg["client_secret"]}
        c.post(e.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        after_grace(e, 0.001)
        c.post(e.path + "/token", data={**form, "refresh_token": tok["refresh_token"]})
        assert e.store.tokens == {}


def test_decided_retention_defaults():
    from redmine_embedding.common.config import Settings

    s = Settings(_env_file=None)
    assert (s.mcp_oauth_access_token_ttl, s.mcp_oauth_refresh_token_ttl, s.mcp_oauth_client_retention_days) == (3600, 30 * 86400, 90)
    assert s.mcp_oauth_scopes == ["view_issues", "view_wiki_pages", "view_news", "view_time_entries", "view_files", "view_project"] and s.mcp_oauth_refresh_grace_seconds == 30
    assert s.mcp_oauth_cleanup_apply is False
