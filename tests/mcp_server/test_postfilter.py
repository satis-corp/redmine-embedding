import threading

import httpx
import pytest

from redmine_embedding.common.db.models import SearchHit
from redmine_embedding.common.redmine.client import RedmineClient
from redmine_embedding.common.redmine.errors import RedmineAuthError
from redmine_embedding.mcp_server.search.postfilter import (
    FilterResult,
    VisibilityChecker,
    VisibleSearcher,
    _filter,
    check_paths,
    filter_visible,
)


def mk(st, sid, *, score=0.9, chunk=0, pid=1, url="/x", parent=(None, None)):
    return SearchHit(st, sid, chunk, pid, "t", "c", url, score, parent[0], parent[1])


def client(status_by_path, calls=None, default=500):
    lock = threading.Lock()

    def handler(req):
        path = req.url.raw_path.decode()
        with lock:
            if calls is not None:
                calls.append(path)
        st = status_by_path.get(path, default)
        if st == 302:
            return httpx.Response(302, headers={"Location": "/login"}, text="<html>")
        return httpx.Response(st, json={})

    return RedmineClient("https://r.example.com", access_token="t", transport=httpx.MockTransport(handler), max_retries=0)


CASES = [
    ("issue", "5", "/issues/5.json"),
    ("news", "5", "/news/5.json"),
    ("version", "5", "/versions/5.json"),
    ("project", "5", "/projects/5.json"),
    ("attachment", "5", "/attachments/5.json"),
    ("file", "5", "/attachments/5.json"),
    ("wiki", "1:A b/c", "/projects/1/wiki/A%20b%2Fc.json"),
]


@pytest.mark.parametrize("st,sid,path", CASES)
@pytest.mark.parametrize("status,ok", [(200, True), (403, False), (404, False), (500, False), (302, False)])
def test_each_type_visible_or_not(st, sid, path, status, ok):
    assert bool(filter_visible([mk(st, sid)], client({path: status})).kept) is ok


@pytest.mark.parametrize("st,sid,path", CASES)
def test_401_propagates(st, sid, path):
    with pytest.raises(RedmineAuthError):
        filter_visible([mk(st, sid)], client({path: 401}))


def test_redirect_is_not_a_json_error():
    assert filter_visible([mk("issue", "1")], client({"/issues/1.json": 302})).kept == []


def test_client_raises_redmine_error_on_3xx():
    from redmine_embedding.common.redmine.errors import RedmineError

    with pytest.raises(RedmineError) as ei:
        client({"/x.json": 302}).get("/x.json")
    assert ei.value.status_code == 302


def test_time_entry_needs_entry_and_parent_issue():
    te = mk("time_entry", "9", parent=("issue", "7"))
    ok = {"/time_entries/9.json": 200, "/issues/7.json": 200}
    assert filter_visible([te], client(ok)).kept == [te]
    assert filter_visible([te], client({**ok, "/time_entries/9.json": 403})).kept == []  # hours hidden, issue visible
    assert filter_visible([te], client({**ok, "/issues/7.json": 404})).kept == []  # parent issue hidden


def test_time_entry_without_parent_issue_is_dropped():
    for solo in (mk("time_entry", "9"), mk("time_entry", "9", parent=("project", "1"))):
        assert check_paths(solo) is None
        calls = []
        assert filter_visible([solo], client({}, calls, default=200)).kept == []
        assert calls == []


def test_unknown_type_and_missing_url_are_dropped_without_requests():
    calls = []
    c = client({}, calls, default=200)
    assert filter_visible([mk("foo", "1"), mk("issue", "1", url=None), mk("wiki", "nocolon")], c).kept == []
    assert check_paths(mk("wiki", "nocolon")) is None
    assert calls == []


def test_chunks_of_one_record_cost_one_request_and_order_by_score():
    calls = []
    c = client({"/issues/1.json": 200, "/issues/2.json": 403}, calls)
    hits = [mk("issue", "1", chunk=0, score=0.5), mk("issue", "1", chunk=1, score=0.8, url="/issues/1#note-1"), mk("issue", "2")]
    assert [h.score for h in filter_visible(hits, c).kept] == [0.8]  # aggregated to the best chunk
    assert calls.count("/issues/1.json") == 1


def test_limit_stops_early():
    calls = []
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 30)]
    out = filter_visible(hits, client({}, calls, default=200), limit=2, workers=2).kept
    assert [h.source_id for h in out] == ["1", "2"]
    assert len(calls) <= 4  # one window of limit*2


def test_api_budget_drops_the_rest():
    calls = []
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 11)]
    out = filter_visible(hits, client({}, calls, default=200), max_requests=3)
    assert len(calls) == 3 and [h.source_id for h in out.kept] == ["1", "2", "3"]
    assert out.exhausted and out.unverified_count == 7


class FakeSearcher:
    def __init__(self, hits, top_k=2):
        self.top_k = top_k
        self.hits = hits
        self.asked = []

    def search_candidates(self, query, *, token, project_ids=None, source_types=None, top_k=None):
        self.asked.append(top_k)
        return self.hits[:top_k]


def vs(searcher, c, **kw):
    return VisibleSearcher(searcher, lambda token: c, **kw)


def test_refetch_doubles_top_k_until_enough_and_reuses_verdicts():
    calls = []
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 9)]
    c = client({f"/issues/{i}.json": 200 if i % 2 == 0 else 403 for i in range(1, 9)}, calls)  # even ids visible
    s = FakeSearcher(hits, top_k=2)
    out = vs(s, c, limit=3).search("q", token="t")
    assert s.asked == [2, 4, 8] and [h.source_id for h in out.kept] == ["2", "4", "6"]
    assert len(calls) == len(set(calls))  # nothing asked twice


def test_refetch_stops_at_max_rounds():
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 40)]
    s = FakeSearcher(hits, top_k=2)
    assert vs(s, client({}, default=403), limit=3, max_rounds=3).search("q", token="t").kept == []
    assert s.asked == [2, 4, 8]


def test_refetch_stops_when_candidates_run_out():
    s = FakeSearcher([mk("issue", "1")], top_k=5)
    assert len(vs(s, client({}, default=200), limit=3).search("q", token="t").kept) == 1
    assert s.asked == [5]


def test_search_401_is_an_error_not_empty():
    s = FakeSearcher([mk("issue", "1")], top_k=5)
    with pytest.raises(RedmineAuthError):
        vs(s, client({}, default=401), limit=3).search("q", token="t")


def test_refetch_stops_when_budget_exhausted():
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 20)]
    s = FakeSearcher(hits, top_k=2)
    assert vs(s, client({}, default=403), limit=3, max_requests=2).search("q", token="t").kept == []
    assert s.asked == [2]


def test_from_settings_values():
    from redmine_embedding.common.config import Settings

    v = VisibleSearcher.from_settings(Settings(_env_file=None), FakeSearcher([]), lambda t: client({}))
    assert (v._limit, v._max_rounds, v._workers, v._max_requests) == (10, 3, 8, 100)


def test_limit_zero_or_negative_returns_nothing():
    hits = [mk("issue", str(i)) for i in range(1, 4)]
    for lim in (0, -1):
        calls = []
        assert filter_visible(hits, client({}, calls, default=200), limit=lim) == FilterResult()
        s = FakeSearcher(hits, top_k=5)
        assert vs(s, client({}, default=200), limit=lim).search("q", token="t").kept == []


def test_unverified_counted_separately_from_invisible():
    hits = [mk("issue", "1", score=0.9), mk("issue", "2", score=0.8), mk("issue", "3", score=0.7), mk("issue", "4", score=0.6)]
    c = client({"/issues/1.json": 200, "/issues/2.json": 403, "/issues/3.json": 500, "/issues/4.json": 302})
    r = filter_visible(hits, c)
    assert [h.source_id for h in r.kept] == ["1"] and r.unverified_count == 2 and not r.exhausted


def test_all_checks_failing_is_empty_with_count_not_an_error():
    hits = [mk("issue", str(i)) for i in range(1, 4)]
    r = filter_visible(hits, client({}, default=500))
    assert r.kept == [] and r.unverified_count == 3
    s = FakeSearcher(hits, top_k=5)
    r = vs(s, client({}, default=500), limit=3).search("q", token="t")
    assert r.kept == [] and r.unverified_count == 3


def test_invisible_wins_over_unverified_for_time_entry():
    te = mk("time_entry", "9", parent=("issue", "7"))
    r = filter_visible([te], client({"/time_entries/9.json": 403, "/issues/7.json": 500}))
    assert r.kept == [] and r.unverified_count == 0


# --- aggregation per source ---------------------------------------


def test_aggregate_keeps_best_chunk_per_source():
    from redmine_embedding.mcp_server.search.postfilter import aggregate_sources

    hits = [mk("issue", "1", chunk=0, score=0.5), mk("issue", "1", chunk=2, score=0.9), mk("wiki", "1:A", score=0.7),
            mk("issue", "2", score=0.6)]
    out = aggregate_sources(hits)
    assert [(h.source_type, h.source_id, h.chunk_no) for h in out] == [("issue", "1", 2), ("wiki", "1:A", 0), ("issue", "2", 0)]
    assert aggregate_sources([]) == []


def test_same_id_different_type_not_merged():
    from redmine_embedding.mcp_server.search.postfilter import aggregate_sources

    assert len(aggregate_sources([mk("issue", "1"), mk("news", "1")])) == 2


def test_filter_visible_limit_counts_sources_and_checks_once():
    hits = [mk("issue", "1", chunk=i, score=0.9 - i * 0.01) for i in range(5)] + [mk("issue", "2", score=0.5)]
    calls = []
    r = filter_visible(hits, client({"/issues/1.json": 200, "/issues/2.json": 200}, calls), limit=2)
    assert [(h.source_id, h.chunk_no) for h in r.kept] == [("1", 0), ("2", 0)]
    assert sorted(calls) == ["/issues/1.json", "/issues/2.json"]


def test_invisible_source_dropped_with_all_chunks_and_takes_no_slot():
    hits = [mk("issue", "1", chunk=0, score=0.9), mk("issue", "1", chunk=1, score=0.8), mk("issue", "2", score=0.7),
            mk("issue", "3", score=0.6)]
    r = filter_visible(hits, client({"/issues/1.json": 403, "/issues/2.json": 200, "/issues/3.json": 200}), limit=2)
    assert [h.source_id for h in r.kept] == ["2", "3"]


def test_zero_hits_and_fewer_sources_than_limit():
    assert filter_visible([], client({}), limit=5).kept == []
    r = filter_visible([mk("issue", "1"), mk("issue", "1", chunk=1)], client({"/issues/1.json": 200}), limit=5)
    assert len(r.kept) == 1


def test_visible_searcher_refetches_when_chunks_collapse_and_stops_at_max_rounds():
    class Fake:
        top_k = 4

        def __init__(self):
            self.ks = []

        def search_candidates(self, query, *, token, project_ids, source_types, top_k):
            self.ks.append(top_k)
            # every candidate is another chunk of issue 1 (no new source appears), so aggregation keeps 1
            return [mk("issue", "1", chunk=i, score=0.9 - i * 0.001) for i in range(top_k)]

    fake = Fake()
    vs = VisibleSearcher(fake, lambda t: client({"/issues/1.json": 200}), limit=3, max_rounds=3)
    r = vs.search("q", token="t")
    assert fake.ks == [4, 8, 16]  # capped by max_rounds, not unbounded
    assert [h.source_id for h in r.kept] == ["1"]  # returned as is (fewer than limit)
    assert r.unverified_count == 0 and not r.exhausted  # stopping at max_rounds is not flagged (spec undecided)


def test_unverified_counted_per_source_not_per_chunk():
    hits = [mk("issue", "1", chunk=i, score=0.9 - i / 100) for i in range(3)]
    r = filter_visible(hits, client({"/issues/1.json": 500}))
    assert r.kept == [] and r.unverified_count == 1  # 3 chunks of one issue -> 1 source


def test_timed_out_counts_sources_not_chunks():
    hits = [mk("issue", "1", chunk=i) for i in range(3)] + [mk("issue", "2", chunk=0, score=0.5)]
    checker = VisibilityChecker(client({}, default=200), deadline=0.0)  # already expired
    r = _filter(hits, checker, 10)
    assert r.timed_out and r.unverified_count == 2 and r.kept == []


def test_tie_breaks_deterministically_regardless_of_input_order():
    a = [mk("issue", "1", chunk=2, score=0.5), mk("issue", "1", chunk=1, score=0.5), mk("issue", "0", chunk=0, score=0.5)]
    for hits in (a, list(reversed(a))):
        out = filter_visible(hits, client({}, default=200)).kept
        assert [(h.source_id, h.chunk_no) for h in out] == [("0", 0), ("1", 1)]


def test_limit_reached_is_not_flagged_exhausted_even_when_budget_is_used_up():
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 11)]
    r = filter_visible(hits, client({}, default=200), limit=2, workers=2, max_requests=4)  # window 4 = budget
    assert len(r.kept) == 2 and not r.exhausted


def test_budget_short_of_limit_is_still_exhausted():
    hits = [mk("issue", str(i), score=1 - i / 100) for i in range(1, 11)]
    r = filter_visible(hits, client({}, default=200), limit=5, max_requests=3)
    assert len(r.kept) == 3 and r.exhausted
