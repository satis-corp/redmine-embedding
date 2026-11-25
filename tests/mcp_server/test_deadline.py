import threading
import time

from redmine_embedding.common.db.models import SearchHit
from redmine_embedding.mcp_server.search.embed_limit import LimitedEmbedder
from redmine_embedding.mcp_server.search.postfilter import VisibleSearcher, VisibilityChecker, _filter
from redmine_embedding.common.embedding.base import Embedder


def h(i, score=0.5):
    return SearchHit("issue", str(i), 0, 1, "t", "c", f"/issues/{i}", score)


class Client:
    def __init__(self):
        self.paths = []

    def get(self, path, params=None):
        self.paths.append(path)
        return {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_expired_deadline_marks_all_unverified_and_makes_no_requests():
    c = Client()
    chk = VisibilityChecker(c, deadline=time.monotonic() - 1)
    r = _filter([h(1), h(2), h(3)], chk, 10)
    assert r.kept == [] and r.unverified_count == 3 and r.timed_out and c.paths == []


def test_no_deadline_is_unchanged():
    c = Client()
    r = _filter([h(1), h(2)], VisibilityChecker(c), 10)
    assert len(r.kept) == 2 and not r.timed_out


class Pre:
    top_k = 2

    def __init__(self):
        self.calls = 0

    def search_candidates(self, q, **kw):
        self.calls += 1
        return [h(1), h(2)]


def test_no_new_round_after_deadline():
    c = Client()
    pre = Pre()
    s = VisibleSearcher(pre, lambda t: c, limit=10, max_rounds=3)
    # deadline in the past: the first round runs (nothing to verify), no further rounds
    r = s.search("q", token="t", deadline=time.monotonic() - 1)
    assert pre.calls == 1 and r.timed_out and c.paths == []


class SlowEmb(Embedder):
    model_name = "x"
    dimension = 1

    def __init__(self):
        self.now = self.peak = 0
        self.lock = threading.Lock()

    def embed_documents(self, texts):
        return []

    def embed_query(self, text):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(0.05)
        with self.lock:
            self.now -= 1
        return [0.0]


def test_embedder_concurrency_limited():
    inner = SlowEmb()
    e = LimitedEmbedder(inner, 2)
    ts = [threading.Thread(target=e.embed_query, args=("q",)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert inner.peak == 2


class SlowClient(Client):
    """Expires the deadline after ``n`` requests (a window that ends mid-way)."""

    def __init__(self, expire_after, clock):
        super().__init__()
        self.expire_after, self.clock = expire_after, clock

    def get(self, path, params=None):
        r = super().get(path, params)
        if len(self.paths) >= self.expire_after:
            self.clock["expired"] = True
        return r


def test_deadline_in_the_middle_of_a_window_sets_timed_out(monkeypatch):
    state = {"expired": False}
    c = SlowClient(1, state)
    chk = VisibilityChecker(c, workers=1, deadline=1.0)
    monkeypatch.setattr(VisibilityChecker, "expired", property(lambda self: state["expired"]))
    cache_before = dict(chk._verdict)
    r = _filter([h(1, 0.9), h(2, 0.8), h(3, 0.7)], chk, 10)
    assert [x.source_id for x in r.kept] == ["1"]
    assert r.unverified_count == 2 and r.timed_out
    assert chk._verdict.get("/issues/2.json") is None and cache_before == {}


class Pre2:
    top_k = 2

    def __init__(self, state):
        self.state, self.calls = state, 0

    def search_candidates(self, q, **kw):
        self.calls += 1
        if self.calls == 2:
            self.state["expired"] = True  # the deadline passes during the second search
        return [h(1), h(2)] if self.calls == 1 else [h(1), h(2), h(3), h(4)]


def test_deadline_in_round_2_keeps_verified_results(monkeypatch):
    state = {"expired": False}
    monkeypatch.setattr(VisibilityChecker, "expired", property(lambda self: state["expired"]))
    c = Client()
    pre = Pre2(state)
    s = VisibleSearcher(pre, lambda t: c, limit=10, max_rounds=3)
    r = s.search("q", token="t", deadline=1.0)
    assert pre.calls == 2
    assert [x.source_id for x in r.kept] == ["1", "2"] and r.timed_out


def test_prefilter_repo_provider_and_arguments():
    import contextlib

    from redmine_embedding.mcp_server.search.prefilter import PrefilterSearcher
    from redmine_embedding.mcp_server.search.visible_projects import VisibleProjectsCache

    class Repo:
        def search(self, vec, **kw):
            return [h(1)]

    class Emb:
        def embed_query(self, t):
            return [0.0]

    used = []

    class VClient(Client):
        def visible_project_ids(self):
            return [5]

    @contextlib.contextmanager
    def provider():
        used.append(1)
        yield Repo()

    ps = PrefilterSearcher(None, Emb(), VisibleProjectsCache(), lambda t: VClient(), repo_provider=provider)
    assert [x.source_id for x in ps.search_candidates("q", token="t", project_ids=None)] == ["1"] and used == [1]
    import pytest

    with pytest.raises(ValueError):
        PrefilterSearcher(None, Emb(), VisibleProjectsCache(), lambda t: Client())
    with pytest.raises(ValueError):
        PrefilterSearcher(Repo(), Emb(), VisibleProjectsCache(), lambda t: Client(), repo_provider=provider)
