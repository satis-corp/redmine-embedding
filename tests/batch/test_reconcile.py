from contextlib import nullcontext

import httpx

from redmine_embedding.batch.reconcile import reconcile
from redmine_embedding.common.redmine.client import RedmineClient


class FakeRepo:
    def __init__(self, projects=None, issues=(), wiki=(), extra=None):
        self.projects = dict(projects or {})
        self.ids = {"issue": set(issues), "wiki": set(wiki),
                    **{k: (v if isinstance(v, dict) else set(v)) for k, v in (extra or {}).items()}}
        self.deleted_projects, self.deleted_sources, self.deleted_children = [], [], []

    def transaction(self):
        return nullcontext()

    def list_project_ids(self):
        return self.projects

    def list_source_ids(self, source_type):
        return set(self.ids.get(source_type, ()))

    def list_source_projects(self, source_type):
        """extra values may be {source_id: project_id}; a plain set means project 1."""
        ids = self.ids.get(source_type, ())
        return dict(ids) if isinstance(ids, dict) else {i: 1 for i in ids}

    def count_children_of(self, parent_type, parent_id):
        return 2

    def delete_project(self, project_id):
        self.deleted_projects.append(project_id)
        return self.projects.get(project_id, 0)

    def delete_children_of(self, parent_type, parent_id):
        self.deleted_children.append((parent_type, parent_id))
        return 2

    def delete_source(self, source_type, source_id):
        self.deleted_sources.append((source_type, source_id))
        return 1


def redmine(projects, issues=(), wiki=None, projects_status=200):
    """projects: [{id, identifier}]; issues: [{id, is_private?}]; wiki: {identifier: [titles] | status code}"""
    wiki = wiki or {}

    def handler(req):
        p = req.url.path
        if p == "/projects.json":
            return httpx.Response(projects_status, json={"projects": projects, "total_count": len(projects)})
        if p == "/issues.json":
            return httpx.Response(200, json={"issues": list(issues), "total_count": len(issues)})
        ident = p.split("/")[2]
        got = wiki.get(ident, 404)
        if isinstance(got, int):
            return httpx.Response(got)
        return httpx.Response(200, json={"wiki_pages": [{"title": t} for t in got]})

    return RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler), max_retries=0)


P = [{"id": 1, "identifier": "alpha"}, {"id": 2, "identifier": "beta"}]


def test_dry_run_reports_but_deletes_nothing():
    repo = FakeRepo(projects={1: 10, 2: 10, 9: 3}, issues={"1", "2", "3", "4"})
    r = reconcile(redmine(P, [{"id": 1}, {"id": 2}, {"id": 3}]), repo)
    assert r.stale_projects == {9: 3} and r.stale_records == {"issue": {"4"}}
    assert not repo.deleted_projects and not repo.deleted_sources and r.deleted_chunks == 0


def test_apply_deletes_archived_project_and_vanished_records():
    repo = FakeRepo(projects={1: 10, 2: 10, 9: 3}, issues={"1", "2", "3", "4"})
    r = reconcile(redmine(P, [{"id": 1}, {"id": 2}, {"id": 3}]), repo, apply=True)
    assert repo.deleted_projects == [9] and repo.deleted_sources == [("issue", "4")]
    assert r.deleted_chunks == 4 + 2  # + the children of the deleted issue


def test_private_issue_in_listing_is_dropped_only_when_the_flag_says_so():
    repo = FakeRepo(projects={1: 5}, issues={"1", "2"})
    r = reconcile(redmine(P, [{"id": 1, "is_private": True}, {"id": 2, "is_private": False}]), repo, apply=True)
    assert repo.deleted_sources == [("issue", "1")]
    # no is_private key in the listing: cannot tell, so keep (incremental sync handles it)
    repo = FakeRepo(projects={1: 5}, issues={"1", "2"})
    reconcile(redmine(P, [{"id": 1}, {"id": 2}]), repo, apply=True)
    assert repo.deleted_sources == []


def test_failed_project_listing_deletes_nothing():
    repo = FakeRepo(projects={9: 3})
    r = reconcile(redmine(P, projects_status=500), repo, apply=True)
    assert r.aborted and repo.deleted_projects == [] and r.skipped


def test_empty_project_listing_deletes_nothing():
    repo = FakeRepo(projects={1: 3})
    r = reconcile(redmine([]), repo, apply=True)
    assert r.aborted and repo.deleted_projects == []


def test_mass_deletion_is_refused():
    repo = FakeRepo(projects={1: 1, 8: 5, 9: 5})
    r = reconcile(redmine(P), repo, apply=True)
    assert repo.deleted_projects == [] and r.stale_projects == {}
    assert any("bad listing" in s for s in r.skipped)


def test_wiki_pages_are_reconciled_and_unreadable_projects_are_kept():
    repo = FakeRepo(projects={1: 5, 2: 5}, wiki={"1:Keep", "1:Gone", "2:Hidden", "2:Hidden2"})
    wiki = {"alpha": ["Keep"], "beta": 403}  # beta's index unreadable: keep its pages
    r = reconcile(redmine(P, wiki=wiki), repo, apply=True, source_types=("wiki",))
    assert repo.deleted_sources == [("wiki", "1:Gone")]
    assert any("unreadable" in s for s in r.skipped)


def test_wiki_module_disabled_means_no_pages():
    repo = FakeRepo(projects={1: 5}, wiki={"1:Old", "1:Old2", "1:Old3"})
    r = reconcile(redmine(P, wiki={"alpha": 404}), repo, apply=True, source_types=("wiki",), max_delete_ratio=1.0)
    assert {s for _, s in repo.deleted_sources} == {"1:Old", "1:Old2", "1:Old3"}


# -- children and the newer record types --------------------------------------
def redmine_all(projects, routes):
    """routes: path -> json | status code (404 when absent); /projects.json is built in."""

    def handler(req):
        if req.url.path == "/projects.json":
            return httpx.Response(200, json={"projects": projects, "total_count": len(projects)})
        got = routes.get(req.url.path, 404)
        return httpx.Response(got) if isinstance(got, int) else httpx.Response(200, json=got)

    return RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler), max_retries=0)


def listing(key, ids, **extra):
    return {key: [{"id": i, **extra} for i in ids], "total_count": len(ids)}


def test_deleting_an_issue_also_deletes_its_attachments_and_time_entries():
    repo = FakeRepo(projects={1: 5}, issues={"1", "2", "3"})
    reconcile(redmine(P, [{"id": 1}, {"id": 2}]), repo, apply=True, source_types=("issue",))
    assert repo.deleted_sources == [("issue", "3")] and repo.deleted_children == [("issue", "3")]


def test_private_issue_children_are_deleted_with_it_and_dry_run_deletes_nothing():
    repo = FakeRepo(projects={1: 5}, issues={"1", "2"})
    reconcile(redmine(P, [{"id": 1, "is_private": True}, {"id": 2, "is_private": False}]), repo, apply=True,
              source_types=("issue",))
    assert repo.deleted_children == [("issue", "1")]
    repo = FakeRepo(projects={1: 5}, issues={"1", "2", "3"})
    reconcile(redmine(P, [{"id": 1}, {"id": 2}]), repo, source_types=("issue",))
    assert not repo.deleted_children and not repo.deleted_sources


def test_vanished_wiki_page_deletes_its_attachments_too():
    repo = FakeRepo(projects={1: 5}, wiki={"1:Keep", "1:Gone"})
    reconcile(redmine(P, wiki={"alpha": ["Keep"], "beta": 404}), repo, apply=True, source_types=("wiki",))
    assert repo.deleted_children == [("wiki", "1:Gone")]


def test_news_version_file_time_entry_are_compared_with_redmine():
    repo = FakeRepo(projects={1: 9}, extra={"news": {"1", "2"}, "version": {"1": 1, "2": 1},
                                            "file": {"1": 1, "2": 2}, "time_entry": {"1", "2"}, "project": {"1"}})
    client = redmine_all(P, {
        "/news.json": listing("news", [1]),
        "/projects/alpha/versions.json": listing("versions", [1]), "/projects/beta/versions.json": 404,
        "/projects/alpha/files.json": listing("files", [1]), "/projects/beta/files.json": listing("files", []),
        "/time_entries.json": listing("time_entries", [1]),
    })
    r = reconcile(client, repo, apply=True, max_delete_ratio=0.5, source_types=(
        "news", "version", "file", "time_entry", "project"))
    assert sorted(repo.deleted_sources) == [("file", "2"), ("news", "2"), ("time_entry", "2"), ("version", "2")]
    assert ("news", "2") in repo.deleted_sources and repo.deleted_children == [("news", "2")]
    assert r.skipped == ["version: 1 project(s) unreadable; their records were kept"]  # beta: 404, kept


def test_unreadable_projects_keep_their_versions_and_files_but_readable_ones_are_reconciled():
    # alpha (1) readable; beta (2): versions 403 (module disabled), files 500. Nothing of beta may go.
    repo = FakeRepo(projects={1: 9}, extra={"version": {"1": 1, "2": 1, "3": 2}, "file": {"1": 1, "2": 1, "3": 2}})
    client = redmine_all(P, {"/projects/alpha/versions.json": listing("versions", [1]),
                             "/projects/beta/versions.json": 403,
                             "/projects/alpha/files.json": listing("files", [1]),
                             "/projects/beta/files.json": 500})
    r = reconcile(client, repo, apply=True, max_delete_ratio=0.9, source_types=("version", "file"))
    assert sorted(repo.deleted_sources) == [("file", "2"), ("version", "2")]  # "3" belongs to beta: kept
    assert len(r.skipped) == 2 and all("unreadable" in s for s in r.skipped)


def test_all_projects_unreadable_deletes_nothing_for_version_file():
    repo = FakeRepo(projects={1: 9}, extra={"version": {"1": 1, "2": 2}, "file": {"1": 1}})
    client = redmine_all(P, {"/projects/alpha/versions.json": 403, "/projects/beta/versions.json": 403})
    r = reconcile(client, repo, apply=True, source_types=("version", "file"))
    assert repo.deleted_sources == [] and len(r.skipped) == 2


def test_failed_news_time_entry_listings_delete_nothing():
    repo = FakeRepo(projects={1: 9}, extra={"news": {"1", "2"}, "time_entry": {"1", "2"}})
    r = reconcile(redmine_all(P, {}), repo, apply=True, source_types=("news", "time_entry"))
    assert repo.deleted_sources == [] and repo.deleted_children == [] and len(r.skipped) == 2


def test_dry_run_counts_child_chunks_of_stale_records():
    repo = FakeRepo(projects={1: 5}, issues={"1", "2", "3"})
    r = reconcile(redmine(P, [{"id": 1}, {"id": 2}]), repo, source_types=("issue",))
    assert r.stale_records == {"issue": {"3"}} and r.stale_child_chunks == 2
    assert not repo.deleted_children


def test_mass_deletion_of_news_is_refused():
    repo = FakeRepo(projects={1: 9}, extra={"news": {"1", "2", "3"}})
    r = reconcile(redmine_all(P, {"/news.json": listing("news", [1])}), repo, apply=True, source_types=("news",))
    assert repo.deleted_sources == [] and any("news" in s for s in r.skipped)


def test_default_source_types_cover_all_new_types():
    from redmine_embedding.batch.reconcile import ALL_SOURCE_TYPES

    assert {"news", "version", "file", "time_entry", "project"} <= set(ALL_SOURCE_TYPES)
