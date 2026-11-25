import httpx
import pytest

from redmine_embedding.batch.sources.issues import IssueConnector, PrivacyUnknownError, build_issue_document
from redmine_embedding.batch.sources.wiki import WikiConnector, split_wiki_sections
from redmine_embedding.common.redmine.client import RedmineClient


def client(handler):
    return RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler))


def issue(**over):
    base = {
        "id": 7, "project": {"id": 3}, "tracker": {"id": 2}, "author": {"id": 5}, "assigned_to": {"id": 6},
        "subject": "ログインできない", "description": "再現手順", "is_private": False,
        "updated_on": "2026-10-26T01:02:31Z",
        "journals": [
            {"id": 1, "notes": "", "private_notes": False},
            {"id": 2, "notes": "公開コメント", "private_notes": False},
            {"id": 3, "notes": "秘密のコメント", "private_notes": True},
        ],
    }
    return {**base, **over}


def test_issue_document_skips_private_notes_and_numbers_notes_by_journal_index():
    d = build_issue_document(issue())
    assert [s.text for s in d.sections] == ["ログインできない\n\n再現手順", "公開コメント"]
    assert d.sections[1].url_fragment == "#note-2"
    assert (d.project_id, d.tracker_id, d.author_id, d.assigned_to_id) == (3, 2, 5, 6)
    assert d.url_path == "/issues/7"


def test_private_issue_is_not_indexed():
    assert build_issue_document(issue(is_private=True)) is None


def test_missing_is_private_field_fails_closed():
    bad = issue()
    del bad["is_private"]
    with pytest.raises(PrivacyUnknownError):
        build_issue_document(bad)


def test_issue_connector_queries_incrementally_and_fetches_journals():
    seen = []

    def handler(req):
        seen.append(req.url)
        if req.url.path == "/issues.json":
            return httpx.Response(200, json={"issues": [{"id": 7}], "total_count": 1})
        assert req.url.path == "/issues/7.json" and req.url.params["include"] == "journals"
        return httpx.Response(200, json={"issue": issue()})

    from datetime import datetime, timezone
    docs = list(IssueConnector(client(handler)).iter_changed(datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc)))
    assert [d.source_id for d in docs] == ["7"]
    params = seen[0].params
    assert params["updated_on"] == ">=2026-10-25T00:00:00Z"
    assert params["status_id"] == "*" and params["sort"] == "id:asc"


def test_wiki_sections_split_on_headings_not_inside_code_fences():
    text = "前文\n\nh1. 概要\n本文\n\n# 手順\n```\n# これはコメント\n```\nおわり"
    sections = [s.text for s in split_wiki_sections(text)]
    assert len(sections) == 3
    assert sections[2].startswith("# 手順") and "# これはコメント" in sections[2]


def test_wiki_connector_filters_by_since_and_skips_projects_without_wiki():
    def handler(req):
        p = req.url.path
        if p == "/projects.json":
            return httpx.Response(200, json={"projects": [
                {"id": 1, "identifier": "alpha"}, {"id": 2, "identifier": "nowiki"}], "total_count": 2})
        if p == "/projects/nowiki/wiki/index.json":
            return httpx.Response(404)
        if p == "/projects/alpha/wiki/index.json":
            return httpx.Response(200, json={"wiki_pages": [
                {"title": "Old", "updated_on": "2026-01-01T00:00:00Z"},
                {"title": "Design_Notes", "updated_on": "2026-10-26T00:00:00Z"}]})
        assert p == "/projects/alpha/wiki/Design_Notes.json"
        return httpx.Response(200, json={"wiki_page": {
            "title": "Design_Notes", "text": "h1. 設計\n本文", "updated_on": "2026-10-26T00:00:00Z",
            "author": {"id": 9}}})

    from datetime import datetime, timezone
    docs = list(WikiConnector(client(handler)).iter_changed(datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert len(docs) == 1
    d = docs[0]
    assert (d.source_id, d.title, d.project_id, d.author_id) == ("1:Design_Notes", "Design Notes", 1, 9)
    assert d.url_path == "/projects/alpha/wiki/Design_Notes"


def test_wiki_connector_loads_renamed_page_not_in_index_even_if_older_than_since():
    from datetime import datetime, timezone

    loaded = []

    def handler(req):
        p = req.url.path
        if p == "/projects.json":
            return httpx.Response(200, json={"projects": [{"id": 1, "identifier": "alpha"}], "total_count": 1})
        if p == "/projects/alpha/wiki/index.json":
            return httpx.Response(200, json={"wiki_pages": [
                {"title": "Known", "updated_on": "2026-01-01T00:00:00Z"},
                {"title": "New_Title", "updated_on": "2026-01-01T00:00:00Z"}]})  # rename keeps updated_on
        loaded.append(p)
        return httpx.Response(200, json={"wiki_page": {
            "title": "New_Title", "text": "本文", "updated_on": "2026-01-01T00:00:00Z"}})

    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    conn = WikiConnector(client(handler), indexed_ids=lambda: {"1:Known", "1:Old_Title"})
    docs = list(conn.iter_changed(since))
    assert [d.source_id for d in docs] == ["1:New_Title"]
    assert loaded == ["/projects/alpha/wiki/New_Title.json"]  # indexed old page is not fetched
    # full sync (since=None) loads everything and needs no index lookup
    def boom():
        raise AssertionError("not needed")
    assert len(list(WikiConnector(client(handler), indexed_ids=boom).iter_changed(None))) == 2


def test_issue_made_private_after_indexing_is_yielded_as_deletion():
    from datetime import datetime, timezone

    from redmine_embedding.batch.sources.base import Deletion

    def handler(req):
        if req.url.path == "/issues.json":
            return httpx.Response(200, json={"issues": [{"id": 7, "project": {"id": 3}}], "total_count": 1})
        return httpx.Response(200, json={"issue": issue(is_private=True)})

    docs = list(IssueConnector(client(handler)).iter_changed(datetime(2026, 10, 25, tzinfo=timezone.utc)))
    assert docs == [Deletion("issue", "7", delete_children=True)]


def test_issue_connector_skips_projects_outside_the_allowed_list():
    fetched = []

    def handler(req):
        if req.url.path == "/issues.json":
            return httpx.Response(200, json={"issues": [
                {"id": 1, "project": {"id": 3}}, {"id": 2, "project": {"id": 99}}], "total_count": 2})
        fetched.append(req.url.path)
        return httpx.Response(200, json={"issue": issue(id=1)})

    docs = list(IssueConnector(client(handler), project_ids={3}).iter_changed(None))
    assert [d.source_id for d in docs] == ["1"] and fetched == ["/issues/1.json"]


def test_journal_with_only_changes_and_no_text_is_skipped():
    d = build_issue_document(issue(journals=[
        {"id": 1, "notes": "", "details": [{"name": "status_id"}], "private_notes": False},
        {"id": 2, "notes": "   \n", "private_notes": False},
        {"id": 3, "notes": "本文あり", "private_notes": False},
    ]))
    assert [s.url_fragment for s in d.sections] == ["", "#note-3"]


def test_wiki_connector_skips_projects_outside_the_allowed_list():
    seen = []

    def handler(req):
        if req.url.path == "/projects.json":
            return httpx.Response(200, json={"projects": [
                {"id": 1, "identifier": "alpha"}, {"id": 2, "identifier": "beta"}], "total_count": 2})
        seen.append(req.url.path)
        return httpx.Response(200, json={"wiki_pages": []})

    assert list(WikiConnector(client(handler), {2}).iter_changed(None)) == []
    assert seen == ["/projects/beta/wiki/index.json"]


def test_build_connectors_passes_project_ids_and_options_to_every_type():
    from redmine_embedding.batch.sources import CONNECTORS, build_connectors

    c = client(lambda req: httpx.Response(404))
    built = build_connectors(c, project_ids=[3, 4])
    assert [b.source_type for b in built] == list(CONNECTORS)
    assert all(b._project_ids == frozenset({3, 4}) for b in built)
    assert all(b._project_ids is None for b in build_connectors(c))
    assert [b.source_type for b in build_connectors(c, ["wiki"], options={"issue": {"x": 1}})] == ["wiki"]


def test_build_connectors_rejects_unknown_type():
    from redmine_embedding.batch.sources import build_connectors

    with pytest.raises(ValueError, match="unknown source type"):
        build_connectors(client(lambda req: httpx.Response(404)), ["nope"])


def test_build_connectors_passes_attachment_options():
    from redmine_embedding.batch.sources import build_connectors

    (c,) = build_connectors(client(lambda req: httpx.Response(404)), ["attachment"],
                            options={"attachment": {"max_bytes": 5, "max_chars": 6}})
    assert (c._max_bytes, c._max_chars) == (5, 6)
