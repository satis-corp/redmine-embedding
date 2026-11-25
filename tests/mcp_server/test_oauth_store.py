"""Store contract: the in-memory store and PostgreSQL behave the same.

The PostgreSQL variant needs TEST_DATABASE_URL (a scratch database whose name ends with "_test").
"""

import os
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest

pytest.importorskip("mcp")

from oauth_fakes import ENV, components  # noqa: E402

from redmine_embedding.mcp_server.auth.schema import T_CLIENTS, T_CODES, T_REQUESTS, T_TOKENS, T_USED, init_oauth_schema  # noqa: E402
from redmine_embedding.mcp_server.auth.store import (  # noqa: E402
    AuthRequest, CodeRecord, MemoryOAuthStore, PgOAuthStore, TokenRecord,
)

URL = os.environ.get("TEST_DATABASE_URL")
NOW = 1_800_000_000.0


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.db)])
def store(request):
    if request.param == "memory":
        yield MemoryOAuthStore()
        return
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg

    dbname = urlparse(URL).path.lstrip("/")
    if not dbname.endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL must point to a database whose name ends with '_test' (got {dbname!r})")
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"DROP TABLE IF EXISTS {T_CLIENTS}, {T_REQUESTS}, {T_CODES}, {T_TOKENS}, {T_USED}")
        init_oauth_schema(c)

    @contextmanager
    def conn():
        with psycopg.connect(URL) as c:  # commits on exit
            yield c

    yield PgOAuthStore(conn)


def req(h="s1", exp=NOW + 600, client="c1", state=None):
    return AuthRequest(h, client, "http://127.0.0.1/cb", True, "chal", "st", ("view_issues",), None, exp, state_hash=state)


def code(h="k1", exp=NOW + 300, client="c1"):
    return CodeRecord(h, client, "http://127.0.0.1/cb", True, "chal", ("view_issues", "view_wiki_pages"), "res", "ENC-A", "ENC-R", NOW + 7200, exp)


def token(a="a1", r="r1", client="c1", a_exp=NOW + 3600, r_exp=NOW + 86400):
    return TokenRecord(a, r, client, ("view_issues",), None, "ENC-A", "ENC-R", NOW + 7200, a_exp, r_exp)


def test_clients(store):
    store.save_client("c1", "ENC", NOW)
    assert store.get_client("c1") == "ENC" and store.get_client("zz") is None
    store.save_client("c1", "ENC2", NOW + 5)  # re-save keeps it usable
    assert store.get_client("c1") == "ENC2"


def test_auth_request_consent_steps(store):
    store.save_auth_request(req("r1"))
    assert store.get_auth_request("r1", NOW).state_hash is None
    assert store.get_auth_request("r1", NOW + 601) is None
    assert store.bind_auth_request("r1", "B", "C", NOW) is True
    assert store.bind_auth_request("zz", "B", "C", NOW) is False
    assert store.bind_auth_request("r1", "B2", "C2", NOW) is False  # the first browser keeps it
    # approval needs the matching csrf + binder, and works once
    assert store.approve_auth_request("r1", "WRONG", "B", "S", "V", NOW) is None
    assert store.approve_auth_request("r1", "C", "WRONG", "S", "V", NOW) is None
    got = store.approve_auth_request("r1", "C", "B", "S", "V", NOW)
    assert got.state_hash == "S" and got.binder_hash == "B" and got.upstream_verifier_enc == "V" and got.csrf_hash is None
    assert store.approve_auth_request("r1", "C", "B", "S2", "V", NOW) is None
    assert store.bind_auth_request("r1", "B2", "C2", NOW) is False  # cannot be re-bound after approval
    assert store.pop_auth_request("S", NOW).request_hash == "r1"
    assert store.pop_auth_request("S", NOW) is None  # single use


def test_auth_request_delete_and_expiry_on_pop(store):
    store.save_auth_request(req("r2"))
    store.delete_auth_request("r2")
    assert store.get_auth_request("r2", NOW) is None
    store.save_auth_request(req("r3", exp=NOW + 1))
    store.bind_auth_request("r3", "B", "C", NOW)
    store.approve_auth_request("r3", "C", "B", "S3", "V", NOW)
    assert store.pop_auth_request("S3", NOW + 2) is None


def test_counts(store):
    store.save_auth_request(req("a"))
    store.save_auth_request(req("b", exp=NOW - 1))
    assert store.count_pending_requests(NOW) == 1
    store.save_client("fresh", "E", NOW)
    store.save_client("old", "E", NOW - 100 * 86400)
    store.save_client("busy", "E", NOW)
    store.create_token(token(client="busy"))
    assert store.count_unused_clients(NOW, NOW - 90 * 86400) == 1  # only "fresh"


def test_every_used_refresh_hash_is_remembered(store):
    t = store.create_token(token())
    assert store.rotate_token(t.id, "r1", token("a2", "r2"), NOW)
    assert store.rotate_token(t.id, "r2", token("a3", "r3"), NOW)
    assert store.rotate_token(t.id, "r3", token("a4", "r4"), NOW)
    for old in ("r1", "r2", "r3"):  # not only the last one
        assert store.get_token_by_used_refresh(old, NOW)[0].id == t.id
    assert store.get_token_by_used_refresh("r4", NOW) is None  # the current one is not "used"
    assert store.get_token_by_refresh("r1", NOW) is None
    assert store.rotate_token(t.id, "r1", token("x", "y"), NOW) is False  # a used-up hash cannot rotate again
    assert store.get_token_by_used_refresh("r1", NOW)[1] == NOW  # when it was used up
    store.delete_token(t.id)
    assert store.get_token_by_used_refresh("r1", NOW) is None  # forgotten with the grant


def test_used_refresh_hashes_are_cleaned_up(store):
    t = store.create_token(token(r_exp=NOW + 100))
    store.rotate_token(t.id, "r1", token("a2", "r2", r_exp=NOW + 100), NOW)
    assert store.cleanup(NOW, NOW - 90 * 86400, apply=False)["used_refresh"] == 0
    assert store.cleanup(NOW + 101, NOW - 90 * 86400, apply=True)["used_refresh"] == 1
    assert store.get_token_by_used_refresh("r1", NOW) is None


def test_counts_by_source_and_window(store):
    store.save_client("a1", "E", NOW, "1.1.1.1")
    store.save_client("a2", "E", NOW, "1.1.1.1")
    store.save_client("b1", "E", NOW, "2.2.2.2")
    store.save_client("old", "E", NOW - 2 * 86400, "1.1.1.1")  # outside the window: does not count
    cut = NOW - 86400
    assert store.count_unused_clients(NOW, cut) == 3
    assert store.count_unused_clients(NOW, cut, "1.1.1.1") == 2
    assert store.count_unused_clients(NOW, cut, "9.9.9.9") == 0
    store.save_auth_request(AuthRequest("p1", "a1", "http://127.0.0.1/cb", True, "c", None, ("v",), None, NOW + 9, source="1.1.1.1"))
    store.save_auth_request(AuthRequest("p2", "a1", "http://127.0.0.1/cb", True, "c", None, ("v",), None, NOW + 9, source="2.2.2.2"))
    store.save_auth_request(AuthRequest("p3", "a1", "http://127.0.0.1/cb", True, "c", None, ("v",), None, NOW - 1, source="1.1.1.1"))
    assert store.count_pending_requests(NOW) == 2 and store.count_pending_requests(NOW, "1.1.1.1") == 1


def test_code_get_pop_expiry(store):
    store.save_code(code())
    assert store.get_code("k1", NOW) == code()
    assert store.get_code("k1", NOW + 301) is None
    assert store.pop_code("k1", NOW) == code()
    assert store.pop_code("k1", NOW) is None and store.get_code("k1", NOW) is None


def test_tokens_lookup_update_rotate_delete(store):
    t = store.create_token(token())
    assert t.id is not None
    assert store.get_token_by_access("a1", NOW).id == t.id
    assert store.get_token_by_refresh("r1", NOW).id == t.id
    assert store.get_token(t.id, NOW).client_id == "c1"
    assert store.get_token_by_access("a1", NOW + 86401) is None  # row (refresh) expired
    store.update_upstream(t.id, "ENC-A2", None, NOW + 9000)
    got = store.get_token(t.id, NOW)
    assert got.upstream_access_enc == "ENC-A2" and got.upstream_refresh_enc is None and got.upstream_expires_at == NOW + 9000
    assert store.rotate_token(t.id, "WRONG", token("a2", "r2"), NOW) is False
    assert store.rotate_token(t.id, "r1", token("a2", "r2"), NOW) is True
    assert store.get_token_by_access("a1", NOW) is None and store.get_token_by_refresh("r2", NOW).id == t.id
    store.delete_token(t.id)
    assert store.get_token(t.id, NOW) is None


def test_cleanup_counts_then_deletes(store):
    store.save_auth_request(req("dead", exp=NOW - 1))
    store.save_auth_request(req("live", exp=NOW + 9, state="live-state"))
    store.save_code(code("dead", exp=NOW - 1))
    store.create_token(token("a-dead", "r-dead", r_exp=NOW - 1))
    store.create_token(token("a-live", "r-live", client="busy"))
    store.save_client("old", "E", NOW - 100 * 86400)
    store.save_client("busy", "E", NOW - 100 * 86400)  # old, but has a live token
    store.save_client("fresh", "E", NOW)
    cut = NOW - 90 * 86400
    assert store.cleanup(NOW, cut, apply=False) == {"auth_requests": 1, "codes": 1, "tokens": 1, "used_refresh": 0, "clients": 1}
    assert store.get_client("old") == "E"  # dry-run deleted nothing
    assert store.cleanup(NOW, cut, apply=True) == {"auth_requests": 1, "codes": 1, "tokens": 1, "used_refresh": 0, "clients": 1}
    assert store.get_client("old") is None and store.get_client("busy") and store.get_client("fresh")
    assert store.pop_auth_request("live-state", NOW) is not None
    assert store.get_token_by_access("a-live", NOW) and store.get_token_by_access("a-dead", NOW) is None


@pytest.mark.db
def test_pg_holds_only_ciphertext_and_hashes(store):
    """Through the real provider objects: no plaintext token, secret or client record in any column."""
    if not isinstance(store, PgOAuthStore):
        pytest.skip("PostgreSQL only")
    import asyncio

    import psycopg
    from starlette.testclient import TestClient

    from test_oauth_flow import Env, client_for, full_login

    e = Env.__new__(Env)  # an Env that uses the PG store
    from oauth_fakes import settings as make_settings
    from redmine_embedding.mcp_server.auth.http import build_http_app
    from redmine_embedding.mcp_server.runtime import AppRuntime
    from redmine_embedding.mcp_server.server import create_server
    from test_oauth_flow import Searcher

    e.store = store
    e.comp, e.rm = components(store=store)
    e.searcher = Searcher()
    e.settings = make_settings()
    e.server = create_server(e.settings, runtime_factory=lambda s, src: AppRuntime(s, e.searcher, src), oauth=e.comp)
    e.app = build_http_app(e.server, e.comp.provider)
    e.path = e.comp.config.public_path
    with client_for(e) as c:
        reg, tok = full_login(c, e)
        up_access, up_refresh = next(iter(e.rm.access)), next(iter(e.rm.refresh))
    with psycopg.connect(URL) as c:
        dump = ""
        for t in (T_CLIENTS, T_REQUESTS, T_CODES, T_TOKENS, T_USED):
            dump += repr(c.execute(f"SELECT * FROM {t}").fetchall())
    for secret in (up_access, up_refresh, tok["access_token"], tok["refresh_token"], reg["client_secret"], ENV["REDMINE_OAUTH_CLIENT_SECRET"]):
        assert secret not in dump
    assert "gAAAA" in dump  # Fernet ciphertext is there
