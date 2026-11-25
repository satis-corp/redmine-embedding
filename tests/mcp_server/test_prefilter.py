import threading

import pytest

from redmine_embedding.mcp_server.search.prefilter import PrefilterSearcher
from redmine_embedding.mcp_server.search.visible_projects import VisibleProjectsCache, token_key


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_ttl_expiry_and_reuse():
    clk = Clock()
    c = VisibleProjectsCache(ttl=60, clock=clk)
    n = []
    f = lambda: (n.append(1), [1, 2])[1]
    assert c.get_or_fetch("tok", f) == [1, 2]
    clk.t = 59
    c.get_or_fetch("tok", f)
    assert len(n) == 1
    clk.t = 60
    c.get_or_fetch("tok", f)
    assert len(n) == 2


def test_users_are_separated():
    c = VisibleProjectsCache()
    assert c.get_or_fetch("a", lambda: [1]) == [1]
    assert c.get_or_fetch("b", lambda: [2]) == [2]
    assert c.get_or_fetch("a", lambda: [9]) == [1]


def test_exception_not_cached_and_empty_is_cached():
    c = VisibleProjectsCache()

    def boom():
        raise RuntimeError("403")

    with pytest.raises(RuntimeError):
        c.get_or_fetch("a", boom)
    assert len(c) == 0
    n = []
    f = lambda: (n.append(1), [])[1]
    assert c.get_or_fetch("a", f) == []
    assert c.get_or_fetch("a", f) == []
    assert len(n) == 1


def test_token_not_retained_and_max_entries_evicts_oldest():
    c = VisibleProjectsCache(max_entries=2)
    for t in ("secret-token-1", "t2", "t3"):
        c.get_or_fetch(t, lambda: [1])
    assert len(c) == 2
    dump = repr(vars(c)) + repr(list(c._data.items()))
    for t in ("secret-token-1", "t2", "t3"):
        assert t not in dump
    assert token_key("t3") in c._data
    n = []
    c.get_or_fetch("secret-token-1", lambda: (n.append(1), [1])[1])
    assert n  # oldest was evicted


class FakeClient:
    def __init__(self, ids):
        self.ids = ids

    def visible_project_ids(self):
        return self.ids

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Factory:
    def __init__(self, by_token):
        self.by_token, self.calls = by_token, 0

    def __call__(self, token):
        self.calls += 1
        return FakeClient(self.by_token.get(token, []))


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        return [0.1]


class FakeRepo:
    def __init__(self):
        self.calls = []

    def search(self, vec, **kw):
        self.calls.append(kw)
        return ["hit"]


def make(by_token, top_k=7):
    repo, emb, fac = FakeRepo(), FakeEmbedder(), Factory(by_token)
    return PrefilterSearcher(repo, emb, VisibleProjectsCache(), fac, top_k=top_k), repo, emb, fac


def test_intersection_and_top_k():
    s, repo, _, fac = make({"t": [1, 2, 3]})
    assert s.search_candidates("q", token="t", project_ids=[2, 3, 99]) == ["hit"]
    assert repo.calls[0]["project_ids"] == [2, 3]
    assert repo.calls[0]["limit"] == 7
    s.search_candidates("q", token="t")
    assert repo.calls[1]["project_ids"] == [1, 2, 3]
    assert fac.calls == 1  # cached


def test_empty_does_not_touch_db_or_embedder():
    s, repo, emb, _ = make({"u": [1]})
    assert s.search_candidates("q", token="t") == []
    assert s.search_candidates("q", token="u", project_ids=[5]) == []
    assert repo.calls == [] and emb.calls == 0


def test_from_settings_wires_values():
    from redmine_embedding.common.config import Settings

    st = Settings(_env_file=None, search_top_k=11, visible_projects_cache_ttl=5, visible_projects_cache_max_entries=3)
    s = PrefilterSearcher.from_settings(st, FakeRepo(), FakeEmbedder(), Factory({"t": [1]}))
    s.search_candidates("q", token="t")
    assert s._repo.calls[0]["limit"] == 11
    assert s._cache._ttl == 5 and s._cache._max == 3


def test_concurrent_users_get_own_results():
    users = {f"tok{i}": [i] for i in range(8)}
    s, repo, _, _ = make(users)
    errors = []

    def run(tok):
        try:
            for _ in range(50):
                s.search_candidates("q", token=tok)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ths = [threading.Thread(target=run, args=(f"tok{i % 8}",)) for i in range(16)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert not errors
    assert all(len(c["project_ids"]) == 1 for c in repo.calls)
    assert {c["project_ids"][0] for c in repo.calls} == set(range(8))


def test_top_k_argument_overrides_configured_value():
    from redmine_embedding.mcp_server.search.prefilter import PrefilterSearcher as P

    seen = []

    class Repo:
        def search(self, vec, **kw):
            seen.append(kw["limit"])
            return []

    class Emb:
        def embed_query(self, q):
            return [0.0]

    class Cl:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def visible_project_ids(self):
            return [1]

    p = P(Repo(), Emb(), VisibleProjectsCache(), lambda t: Cl(), top_k=50)
    p.search_candidates("q", token="t")
    p.search_candidates("q", token="t", top_k=100)
    assert seen == [50, 100] and p.top_k == 50
