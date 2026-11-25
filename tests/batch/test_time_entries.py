from datetime import datetime, timezone

import httpx
import pytest

from redmine_embedding.batch.sources import CONNECTORS
from redmine_embedding.batch.sources.base import Deletion, SourceDocument
from redmine_embedding.batch.sources.time_entries import TimeEntryConnector
from redmine_embedding.common.redmine.client import RedmineClient
from redmine_embedding.common.redmine.errors import RedmineError

NEW, OLD = "2026-10-25T00:00:00Z", "2026-01-01T00:00:00Z"


def te(i, issue=7, comments="作業しました", updated=NEW, project=3):
    return {"id": i, "project": {"id": project}, "issue": {"id": issue} if issue else None, "user": {"id": 5},
            "activity": {"name": "開発"}, "hours": 1.0, "comments": comments, "updated_on": updated}


def make(entries, private=(1,), private_status=200, ignore_filter=False, project_ids=None):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/issues.json":
            if private_status != 200:
                return httpx.Response(private_status)
            items = [{"id": i, "is_private": True} for i in private]
            if ignore_filter:  # server ignores is_private=1 and returns everything
                items += [{"id": 99, "is_private": False}]
            return httpx.Response(200, json={"issues": items, "total_count": len(items)})
        assert req.url.path == "/time_entries.json"
        return httpx.Response(200, json={"time_entries": entries, "total_count": len(entries)})

    c = RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler), max_retries=0)
    return TimeEntryConnector(c, project_ids), seen


def run(conn, since=None):
    return list(conn.iter_changed(since))


def test_commented_entry_becomes_a_document_under_its_issue():
    d = run(make([te(1)])[0])[0]
    assert isinstance(d, SourceDocument)
    assert (d.source_id, d.title, d.url_path, d.parent_type, d.parent_id, d.author_id, d.project_id) == (
        "1", "#7 作業時間", "/issues/7", "issue", "7", 5, 3)
    assert d.sections[0].text == "開発\n作業しました"


def test_empty_comment_is_never_a_document_but_clears_any_old_chunks():
    items = run(make([te(1, comments=""), te(2, comments="  ")])[0])
    assert items == [Deletion("time_entry", "1"), Deletion("time_entry", "2")]


def test_entries_of_private_issues_are_deleted_even_when_old():
    conn, _ = make([te(5, issue=1, updated=OLD), te(6, issue=7)])
    items = run(conn, since=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert items[0] == Deletion("time_entry", "5") and items[1].source_id == "6" and len(items) == 2


def test_private_listing_failure_fails_closed():
    conn, seen = make([te(1)], private_status=500)
    with pytest.raises(RedmineError):
        run(conn)
    assert all(r.url.path != "/time_entries.json" for r in seen)  # nothing was read, nothing indexed


def test_unfiltered_private_listing_aborts():
    conn, _ = make([te(1)], ignore_filter=True)
    with pytest.raises(RedmineError, match="not filtered"):
        run(conn)


def test_since_is_applied_on_the_client_and_never_sent_to_the_server():
    conn, seen = make([te(1, updated=OLD), te(2)])
    items = run(conn, since=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [i.source_id for i in items] == ["2"]
    entries_req = [r for r in seen if r.url.path == "/time_entries.json"]
    assert entries_req and all(not any(k in r.url.params for k in ("updated_on", "created_on", "spent_on"))
                               for r in entries_req)


def test_entries_without_issue_or_out_of_scope_are_skipped():
    conn, _ = make([te(1, issue=None), te(2, project=99), te(3)], project_ids={3})
    assert [i.source_id for i in run(conn)] == ["3"]


def test_registered():
    assert CONNECTORS["time_entry"] is TimeEntryConnector


def test_whole_pipeline_keeps_private_issue_time_entries_out_of_the_index():
    from test_sync import FakeEmbedder, FakeRepo, run as sync_run

    conn, _ = make([te(1, comments="PUBLIC-WORK"), te(2, issue=1, comments="SECRET-WORK")])
    emb, repo = FakeEmbedder(), FakeRepo()
    result = sync_run(conn, repo, embedder=emb)
    stored = " ".join(c.content for c in repo.chunks.values()) + " ".join(t for x in emb.calls for t in x)
    assert "PUBLIC-WORK" in stored and "SECRET" not in stored
    assert repo.deleted_sources == ["2"] and result.failures == 0
    assert repo.chunks[("time_entry", "1", 0)].parent_id == "7"


def test_whole_pipeline_listing_failure_stores_nothing_and_keeps_last_sync_unset():
    from test_sync import FakeEmbedder, FakeRepo, run as sync_run

    conn, _ = make([te(1)], private_status=500)
    repo = FakeRepo()
    result = sync_run(conn, repo, embedder=FakeEmbedder())
    assert result.failures == 1 and not repo.chunks and repo.last is None
