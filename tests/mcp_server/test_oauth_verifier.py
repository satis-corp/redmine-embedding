"""Introspection verifier: active / inactive / 5xx / expired / cache TTL."""

import logging

import httpx
import pytest

pytest.importorskip("mcp")

from oauth_fakes import CLIENT_SECRET, FakeRedmine, components  # noqa: E402

from redmine_embedding.mcp_server.auth.upstream import UpstreamUnavailable  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def setup(**kw):
    clock = Clock()
    comp, rm = components(FakeRedmine(clock=clock), clock=clock, **kw)
    return comp.provider.verifier, rm, clock


def introspects(rm):
    return [c for c in rm.calls if c[0] == "/oauth/introspect"]


async def test_active_token_returns_scopes_and_exp():
    v, rm, clock = setup()
    rm.access["T"] = {"exp": clock() + 100, "scope": "view_issues view_wiki_pages"}
    res = await v.verify("T")
    assert res is not None and res.scopes == ("view_issues", "view_wiki_pages") and res.exp == clock() + 100


async def test_unknown_or_revoked_token_is_inactive():
    v, rm, clock = setup()
    assert await v.verify("nope") is None


async def test_expired_token_is_inactive():
    v, rm, clock = setup()
    rm.access["T"] = {"exp": clock() - 5, "scope": "view_issues"}
    assert await v.verify("T") is None


async def test_active_answer_past_exp_is_treated_as_expired():
    v, rm, clock = setup()
    rm.access["T"] = {"exp": clock() + 10, "scope": "view_issues"}
    clock.t += 20  # our clock is ahead: the reported exp is in the past
    rm.access["T"]["exp"] = clock() - 1
    rm.clock = lambda: clock() - 100  # Redmine's own clock disagrees
    assert await v.verify("T") is None


@pytest.mark.parametrize("status", [500, 502, 503, 400, 404])
async def test_introspect_failure_is_unavailable_not_invalid(status):
    v, rm, clock = setup()
    rm.access["T"] = {"exp": clock() + 100, "scope": "view_issues"}
    rm.introspect_status = status
    with pytest.raises(UpstreamUnavailable):
        await v.verify("T")


async def test_network_error_is_unavailable():
    v, rm, clock = setup()
    rm.raise_network = True
    with pytest.raises(UpstreamUnavailable):
        await v.verify("T")


async def test_wrong_app_credentials_is_unavailable_and_logged_without_secret(caplog):
    caplog.set_level(logging.DEBUG)
    v, rm, clock = setup()
    v._upstream._http.auth = httpx.BasicAuth("x", "wrong")
    with pytest.raises(UpstreamUnavailable) as e:
        await v.verify("TOKEN-VALUE")
    assert "TOKEN-VALUE" not in str(e.value) and CLIENT_SECRET not in caplog.text
    assert "credentials" in caplog.text  # tells the operator what to check


async def test_garbage_answer_is_unavailable():
    v, rm, clock = setup()
    rm.handler_orig = rm.handler
    v._upstream._http._transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(UpstreamUnavailable):
        await v.verify("T")


async def test_cache_hit_within_ttl_then_refetch_after():
    v, rm, clock = setup()  # ttl 30 s (default)
    rm.access["T"] = {"exp": clock() + 1000, "scope": "view_issues"}
    await v.verify("T")
    clock.t += 29
    await v.verify("T")
    assert len(introspects(rm)) == 1
    clock.t += 2  # 31 s
    await v.verify("T")
    assert len(introspects(rm)) == 2


async def test_cache_never_outlives_token_exp():
    v, rm, clock = setup()
    rm.access["T"] = {"exp": clock() + 10, "scope": "view_issues"}
    await v.verify("T")
    clock.t += 11  # past exp but within 30 s: must ask again and get "inactive"
    assert await v.verify("T") is None
    assert len(introspects(rm)) == 2


async def test_inactive_result_is_not_cached():
    v, rm, clock = setup()
    assert await v.verify("T") is None
    rm.access["T"] = {"exp": clock() + 100, "scope": "view_issues"}
    assert await v.verify("T") is not None


async def test_cache_key_is_a_hash_not_the_token():
    v, rm, clock = setup()
    rm.access["SECRET-TOKEN-ABC"] = {"exp": clock() + 100, "scope": "view_issues"}
    await v.verify("SECRET-TOKEN-ABC")
    assert all("SECRET-TOKEN-ABC" not in k for k in v._cache)


async def test_ttl_zero_disables_cache():
    v, rm, clock = setup(mcp_oauth_introspect_cache_ttl=0.0)
    rm.access["T"] = {"exp": clock() + 100, "scope": "view_issues"}
    await v.verify("T")
    await v.verify("T")
    assert len(introspects(rm)) == 2
