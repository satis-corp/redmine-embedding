import pytest

from redmine_embedding.common.config import Settings
from redmine_embedding.common.redmine.errors import RedmineAuthError
from redmine_embedding.mcp_server.search.check_cache import CheckCache
from redmine_embedding.mcp_server.search.postfilter import VisibilityChecker, VisibleSearcher, filter_visible
from test_postfilter import client, mk


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run(c, cache, paths, token="tok", **kw):
    ck = VisibilityChecker(c, cache=cache, token=token, **kw)
    ck.prefetch(paths)
    return ck


def test_disabled_never_reads_or_writes():
    cache = CheckCache(ttl=0)
    calls = []
    run(client({"/a": 200}, calls), cache, ["/a"])
    run(client({"/a": 200}, calls), cache, ["/a"])
    assert calls == ["/a", "/a"] and len(cache) == 0


def test_hit_skips_api_and_not_counted_in_budget():
    cache = CheckCache(ttl=10)
    calls = []
    run(client({"/a": 200, "/b": 403}, calls), cache, ["/a", "/b"])
    ck = run(client({"/a": 200, "/b": 403, "/c": 200}, calls), cache, ["/a", "/b", "/c"], max_requests=1)
    assert sorted(calls) == ["/a", "/b", "/c"]  # a, b from cache; c used the single budget
    assert ck.judge(["/a"]) is True and ck.judge(["/b"]) is False and ck.judge(["/c"]) is True
    assert ck.exhausted


def test_ttl_expiry_rechecks():
    clk = Clock()
    cache = CheckCache(ttl=10, clock=clk)
    calls = []
    run(client({"/a": 200}, calls), cache, ["/a"])
    clk.t = 9.9
    run(client({"/a": 200}, calls), cache, ["/a"])
    assert calls == ["/a"]
    clk.t = 10.0
    run(client({"/a": 200}, calls), cache, ["/a"])
    assert calls == ["/a", "/a"]


def test_different_token_not_shared():
    cache = CheckCache(ttl=10)
    calls = []
    run(client({"/a": 200}, calls), cache, ["/a"], token="u1")
    run(client({"/a": 403}, calls), cache, ["/a"], token="u2")
    assert calls == ["/a", "/a"]
    assert cache.get("u1", "/a") is True and cache.get("u2", "/a") is False


def test_unverified_and_401_not_stored():
    cache = CheckCache(ttl=10)
    run(client({"/a": 500, "/b": 302}), cache, ["/a", "/b"])
    assert len(cache) == 0
    with pytest.raises(RedmineAuthError):
        run(client({"/c": 401}), cache, ["/c"])
    assert len(cache) == 0
    cache.put("t", "/x", None)
    assert len(cache) == 0


def test_over_budget_not_stored():
    cache = CheckCache(ttl=10)
    run(client({"/a": 200, "/b": 200}), cache, ["/a", "/b"], max_requests=1)
    assert len(cache) == 1


def test_fifo_limit_and_no_token_stored():
    cache = CheckCache(ttl=10, max_entries=2)
    for p in ["/a", "/b", "/c"]:
        cache.put("secret-token", p, True)
    assert cache.get("secret-token", "/a") is None and cache.get("secret-token", "/c") is True
    assert len(cache) == 2
    assert all("secret-token" not in repr(k) for k in cache._data)


def test_filter_visible_shared_across_searches_via_searcher():
    st = Settings(_env_file=None, search_check_cache_ttl=30, search_check_cache_max_entries=5)
    assert st.search_check_cache_ttl == 30
    assert Settings(_env_file=None).search_check_cache_ttl == 0.0
    s = VisibleSearcher.from_settings(st, object(), lambda t: None)
    assert s._check_cache.enabled
    assert not VisibleSearcher.from_settings(Settings(_env_file=None), object(), lambda t: None)._check_cache.enabled


def test_filter_visible_unchanged_without_cache():
    assert len(filter_visible([mk("issue", "1")], client({"/issues/1.json": 200})).kept) == 1
