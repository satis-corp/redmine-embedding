from datetime import datetime, timezone

import httpx
import pytest

from redmine_embedding.batch.sources import CONNECTORS, build_connectors
from redmine_embedding.batch.sources.files import FileConnector
from redmine_embedding.batch.sources.projects import ProjectConnector
from redmine_embedding.batch.sources.versions import VersionConnector
from redmine_embedding.common.redmine.client import RedmineClient

BASE = "https://r.example.com"
PROJECTS = [
    {"id": 1, "identifier": "alpha", "name": "アルファ", "description": "説明A", "homepage": "https://a.example.com",
     "updated_on": "2026-10-01T00:00:00Z"},
    {"id": 2, "identifier": "beta", "name": "ベータ", "description": "", "homepage": "",
     "updated_on": "2026-10-20T00:00:00Z"},
]
OLD, NEW = "2026-09-01T00:00:00Z", "2026-10-25T00:00:00Z"


def server(routes, downloads=None):
    """routes: path -> json | status code. downloads: attachment id -> bytes."""
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/projects.json":
            return httpx.Response(200, json={"projects": PROJECTS, "total_count": len(PROJECTS)})
        if req.url.path.startswith("/attachments/download/"):
            seen_dl.append(int(req.url.path.split("/")[3]))
            return httpx.Response(200, content=(downloads or {})[seen_dl[-1]])
        got = routes.get(req.url.path, 404)
        return httpx.Response(got) if isinstance(got, int) else httpx.Response(200, json=got)

    seen_dl: list[int] = []
    c = RedmineClient(BASE, transport=httpx.MockTransport(handler), max_retries=0)
    return c, seen, seen_dl


def ver(i, owner=1, updated=NEW, description="説明"):
    return {"id": i, "name": f"v{i}", "description": description, "status": "closed", "project": {"id": owner},
            "updated_on": updated}


# -- version -------------------------------------------------------------------
def test_versions_are_filtered_on_the_client_and_shared_ones_deduplicated():
    shared = ver(3)
    c, seen, _ = server({"/projects/alpha/versions.json": {"versions": [ver(1), ver(2, updated=OLD), shared]},
                         "/projects/beta/versions.json": {"versions": [shared, ver(4, owner=2)]}})
    docs = list(VersionConnector(c).iter_changed(datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert [(d.source_id, d.project_id) for d in docs] == [("1", 1), ("3", 1), ("4", 2)]
    assert docs[0].url_path == "/versions/1" and docs[0].sections[0].text == "v1\n\n説明"
    assert all("updated_on" not in r.url.params for r in seen)  # no server-side filter exists


def test_version_with_empty_description_is_still_yielded_by_name():
    c, _, _ = server({"/projects/alpha/versions.json": {"versions": [ver(1, description="")]}})
    d = list(VersionConnector(c, {1}).iter_changed(None))
    assert [s.text for s in d[0].sections] == ["v1"]


def test_version_modules_disabled_or_projects_out_of_scope_are_skipped():
    c, seen, _ = server({"/projects/alpha/versions.json": {"versions": [ver(1), ver(9, owner=2)]},
                         "/projects/beta/versions.json": 403})
    assert [d.source_id for d in VersionConnector(c, {1}).iter_changed(None)] == ["1"]
    assert all("beta" not in r.url.path for r in seen)


# -- project -------------------------------------------------------------------
def test_project_document_and_client_side_since():
    c, _, _ = server({})
    docs = list(ProjectConnector(c).iter_changed(datetime(2026, 10, 10, tzinfo=timezone.utc)))
    assert [d.source_id for d in docs] == ["2"]
    assert docs[0].sections[0].text == "ベータ" and docs[0].url_path == "/projects/beta"
    d = list(ProjectConnector(c, {1}).iter_changed(None))[0]
    assert d.sections[0].text == "アルファ\n\n説明A\n\nhttps://a.example.com" and d.project_id == 1


# -- file ----------------------------------------------------------------------
def f(i, name, ctype=None, size=10, description=""):
    return {"id": i, "filename": name, "filesize": size, "content_type": ctype, "description": description,
            "content_url": f"{BASE}/attachments/download/{i}/{name}", "author": {"id": 5},
            "created_on": NEW}


def test_text_file_body_is_indexed_and_other_files_by_name_only():
    c, _, dl = server({"/projects/alpha/files.json": {"files": [
        f(1, "readme.txt", "text/plain", description="説明"), f(2, "a.zip", "application/zip"),
        f(3, ".env", "text/plain"), f(4, "big.log", "text/plain", size=10_000_000)]},
        "/projects/beta/files.json": 404}, downloads={1: "本文".encode()})
    docs = list(FileConnector(c).iter_changed(datetime(2030, 1, 1, tzinfo=timezone.utc)))  # since is ignored
    assert [d.source_id for d in docs] == ["1", "2", "3", "4"]
    assert [s.text for s in docs[0].sections] == ["readme.txt\n説明", "本文"]
    assert [len(d.sections) for d in docs[1:]] == [1, 1, 1] and dl == [1]
    assert (docs[0].parent_type, docs[0].parent_id, docs[0].url_path, docs[0].author_id) == (
        "project", "1", "/attachments/1", 5)


def test_file_is_deduplicated_and_limits_are_configurable():
    c, _, dl = server({"/projects/alpha/files.json": {"files": [f(1, "a.txt", "text/plain", size=5)]},
                       "/projects/beta/files.json": {"files": [f(1, "a.txt", "text/plain", size=5)]}},
                      downloads={1: b"abcdefghij"})
    docs = list(FileConnector(c, max_chars=4).iter_changed(None))
    assert len(docs) == 1 and docs[0].sections[1].text == "abcd"


def test_new_types_are_registered_for_the_cli():
    for t in ("version", "project", "file"):
        assert t in CONNECTORS
    c, _, _ = server({})
    assert [x.source_type for x in build_connectors(c, ["version", "project", "file"], project_ids={1})] == [
        "version", "project", "file"]
    with pytest.raises(ValueError):
        build_connectors(c, ["nope"])


def test_whole_pipeline_for_files_and_excluded_names():
    from test_sync import FakeEmbedder, FakeRepo, run

    c, _, _ = server({"/projects/alpha/files.json": {"files": [f(1, "ok.txt", "text/plain"),
                                                              f(2, "secret.txt", "text/plain")]},
                      "/projects/beta/files.json": 404}, downloads={1: b"PUBLIC", 2: b"SECRET-BODY"})
    emb, repo = FakeEmbedder(), FakeRepo()
    run(FileConnector(c), repo, embedder=emb)
    stored = " ".join(ch.content for ch in repo.chunks.values()) + " ".join(t for x in emb.calls for t in x)
    assert "PUBLIC" in stored and "SECRET-BODY" not in stored
