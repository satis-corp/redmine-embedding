from datetime import datetime, timezone

import httpx
import pytest

from redmine_embedding.batch.sources.attachments import AttachmentConnector
from redmine_embedding.batch.sources.base import ChildrenReset, SourceDocument
from redmine_embedding.batch.sources.issues import PrivacyUnknownError
from redmine_embedding.common.redmine.client import RedmineClient

BASE = "https://r.example.com"


def att(i, name, ctype="text/plain", size=10, **over):
    return {"id": i, "filename": name, "filesize": size, "content_type": ctype, "description": "",
            "content_url": f"{BASE}/attachments/download/{i}/{name}", "author": {"id": 5},
            "created_on": "2026-10-20T00:00:00Z", **over}


def listed(i, attachments=(), private=False, project=3):
    return {"id": i, "project": {"id": project}, "is_private": private, "updated_on": "2026-10-26T00:00:00Z",
            "attachments": list(attachments)}


class Server:
    """Fake Redmine: records every request and serves bodies by attachment id."""

    def __init__(self, issues=(), bodies=None, journals=None, wiki=None, projects=None, news=None,
                 private_in_detail=()):
        self.private_in_detail = set(private_in_detail)
        self.issues, self.bodies, self.journals = list(issues), bodies or {}, journals or {}
        self.wiki, self.projects = wiki or {}, projects if projects is not None else []
        self.news = news or {}  # news id -> (project id, attachments)
        self.requests, self.downloads = [], []

    def __call__(self, req):
        self.requests.append(req)
        path = req.url.path
        if path == "/issues.json":
            return httpx.Response(200, json={"issues": self.issues, "total_count": len(self.issues)})
        if path == "/news.json":
            items = [{"id": n, "project": {"id": p}} for n, (p, _) in self.news.items()]
            return httpx.Response(200, json={"news": items, "total_count": len(items)})
        if path.startswith("/news/"):
            nid = int(path.split("/")[2].split(".")[0])
            assert req.url.params["include"] == "attachments"
            return httpx.Response(200, json={"news": {"id": nid, "created_on": "2026-10-20T00:00:00Z",
                                                     "attachments": self.news[nid][1]}})
        if path.startswith("/issues/"):
            iid = int(path.split("/")[2].split(".")[0])
            return httpx.Response(200, json={"issue": {
                "id": iid, "is_private": iid in self.private_in_detail, "journals": self.journals.get(iid, [])}})
        if path.startswith("/attachments/download/"):
            aid = int(path.split("/")[3])
            self.downloads.append(aid)
            body = self.bodies[aid]
            return httpx.Response(body) if isinstance(body, int) else httpx.Response(200, content=body)
        if path == "/projects.json":
            return httpx.Response(200, json={"projects": self.projects, "total_count": len(self.projects)})
        if path.endswith("/wiki/index.json"):
            ident = path.split("/")[2]
            return httpx.Response(200, json={"wiki_pages": [{"title": t} for t in self.wiki.get(ident, {})]})
        if "/wiki/" in path:
            ident, title = path.split("/")[2], path.split("/")[4].removesuffix(".json")
            assert req.url.params["include"] == "attachments"
            return httpx.Response(200, json={"wiki_page": {
                "title": title, "updated_on": "2026-10-25T00:00:00Z", "attachments": self.wiki[ident][title]}})
        raise AssertionError(path)

    def connector(self, project_ids=None, **kw):
        c = RedmineClient(BASE, api_key="SECRET-KEY", transport=httpx.MockTransport(self), max_retries=0)
        return AttachmentConnector(c, project_ids, **kw)


def run(server, since=None, **kw):
    return list(server.connector(**kw).iter_changed(since))


def docs_of(items):
    return [i for i in items if isinstance(i, SourceDocument)]


def test_text_attachment_gets_metadata_and_body_sections():
    s = Server([listed(7, [att(11, "メモ.txt", description="手順メモ")])], bodies={11: "本文です".encode()})
    items = run(s)
    d = docs_of(items)[0]
    assert (d.source_type, d.source_id, d.title, d.project_id) == ("attachment", "11", "メモ.txt", 3)
    assert (d.parent_type, d.parent_id, d.author_id, d.url_path) == ("issue", "7", 5, "/attachments/11")
    assert [x.text for x in d.sections] == ["メモ.txt\n手順メモ", "本文です"]
    assert items[-1] == ChildrenReset("attachment", "issue", "7", frozenset({"11"}))


def test_listing_is_incremental_and_asks_for_attachments():
    s = Server([])
    run(s, since=datetime(2026, 10, 25, tzinfo=timezone.utc))
    p = s.requests[0].url.params
    assert p["include"] == "attachments" and p["updated_on"] == ">=2026-10-25T00:00:00Z" and p["status_id"] == "*"


def test_private_issue_attachments_are_never_yielded_and_old_ones_are_reset():
    s = Server([listed(7, [att(11, "secret-plan.txt")], private=True)], bodies={11: b"SECRET-BODY"})
    items = run(s)
    assert items == [ChildrenReset("attachment", "issue", "7", frozenset())]
    assert s.downloads == []


def test_missing_is_private_fails_closed():
    bad = listed(7, [att(11, "a.txt")])
    del bad["is_private"]
    with pytest.raises(PrivacyUnknownError):
        run(Server([bad], bodies={11: b"x"}))


def test_attachment_added_by_a_private_note_is_excluded():
    journals = {7: [
        {"id": 1, "private_notes": True, "details": [{"property": "attachment", "name": "11", "new_value": "a.txt"}]},
        {"id": 2, "private_notes": False, "details": [{"property": "attachment", "name": "12", "new_value": "b.txt"}]},
    ]}
    s = Server([listed(7, [att(11, "a.txt"), att(12, "b.txt")])], bodies={11: b"PRIVATE", 12: b"public"},
               journals=journals)
    items = run(s)
    assert [d.source_id for d in docs_of(items)] == ["12"]
    assert s.downloads == [12]
    assert items[-1].keep_source_ids == frozenset({"12"})


def test_issue_without_attachments_still_resets_its_children():
    items = run(Server([listed(7)]))
    assert items == [ChildrenReset("attachment", "issue", "7", frozenset())]


def test_projects_outside_the_allowed_list_are_skipped():
    s = Server([listed(1, project=3), listed(2, project=99)])
    assert [i.parent_id for i in run(s, project_ids={3})] == ["1"]


@pytest.mark.parametrize("name,ctype", [("img.png", "image/png"), ("a.docx", "application/msword"),
                                        ("x.html", "text/html"), ("blob", None), (".env", "text/plain"),
                                        ("server.key", "text/plain"), ("MySecret.TXT", "text/plain")])
def test_files_that_are_not_ingested_as_text_are_not_downloaded(name, ctype):
    s = Server([listed(7, [att(11, name, ctype)])], bodies={11: b"x"})
    d = docs_of(run(s))
    assert [len(x.sections) for x in d] == [1] and s.downloads == []


def test_file_over_the_size_limit_is_not_downloaded_but_stays_searchable_by_name():
    s = Server([listed(7, [att(11, "big.log", size=3 * 1024 * 1024)])], bodies={11: b"x"})
    d = docs_of(run(s, max_bytes=2 * 1024 * 1024))
    assert [x.text for x in d[0].sections] == ["big.log"] and s.downloads == []


def test_limits_are_configurable_and_text_is_truncated():
    s = Server([listed(7, [att(11, "a.txt", size=100), att(12, "b.txt", size=5)])],
               bodies={11: b"x" * 100, 12: b"abcdefghij"})
    d = docs_of(run(s, max_bytes=50, max_chars=4))
    assert s.downloads == [12] and d[1].sections[1].text == "abcd"


def test_unknown_filesize_is_still_bounded_by_the_download_limit():
    a = att(11, "a.txt")
    del a["filesize"]
    s = Server([listed(7, [a])], bodies={11: b"x" * 100})
    assert len(docs_of(run(s, max_bytes=50))[0].sections) == 1  # too large while streaming: metadata only


def test_binary_and_undecodable_bodies_become_metadata_only():
    s = Server([listed(7, [att(11, "a.txt"), att(12, "b.log")])], bodies={11: b"a\x00b", 12: b"\x89\xff\xfe\x80" * 4})
    assert [len(d.sections) for d in docs_of(run(s))] == [1, 1]


def test_shift_jis_body_is_decoded():
    s = Server([listed(7, [att(11, "log.txt")])], bodies={11: "日本語".encode("cp932")})
    assert docs_of(run(s))[0].sections[1].text == "日本語"


def test_extractor_is_replaceable():
    s = Server([listed(7, [att(11, "a.txt")])], bodies={11: b"raw"})
    assert docs_of(run(s, extract=lambda b: b.decode().upper()))[0].sections[1].text == "RAW"


def test_download_from_another_host_is_refused_without_failing_the_run():
    a = att(11, "a.txt", content_url="https://evil.example.net/attachments/download/11/a.txt")
    s = Server([listed(7, [a])], bodies={11: b"x"})
    assert [len(d.sections) for d in docs_of(run(s))] == [1]
    assert s.downloads == []  # the key never left for the other host
    assert all(r.url.host == "r.example.com" for r in s.requests)


def test_wiki_attachments_are_read_for_every_page_regardless_of_since():
    s = Server(projects=[{"id": 1, "identifier": "alpha"}], wiki={"alpha": {"Design_Notes": [att(21, "spec.md", "text/markdown")]}},
               bodies={21: "# 仕様".encode()})
    items = run(s, since=datetime(2030, 1, 1, tzinfo=timezone.utc))
    d = docs_of(items)[0]
    assert (d.source_id, d.parent_type, d.parent_id, d.project_id) == ("21", "wiki", "1:Design_Notes", 1)
    assert d.sections[1].text == "# 仕様"
    assert items[-1] == ChildrenReset("attachment", "wiki", "1:Design_Notes", frozenset({"21"}))


def test_wiki_page_that_lost_its_attachments_is_reset():
    s = Server(projects=[{"id": 1, "identifier": "alpha"}], wiki={"alpha": {"P": []}})
    assert run(s) == [ChildrenReset("attachment", "wiki", "1:P", frozenset())]


def test_api_key_is_not_in_any_log_message(caplog):
    caplog.set_level("DEBUG")
    a = att(11, "a.txt", content_url="https://evil.example.net/x")
    run(Server([listed(7, [a])], bodies={11: b"x"}))
    assert "SECRET-KEY" not in caplog.text and "evil.example.net" not in caplog.text


def test_whole_pipeline_keeps_private_attachment_out_of_the_index():
    from test_sync import FakeEmbedder, FakeRepo, run as sync_run

    s = Server([listed(1, [att(11, "ok.txt")]), listed(2, [att(12, "x.txt")], private=True)],
               bodies={11: b"PUBLIC-BODY", 12: b"SECRET-BODY"})
    emb, repo = FakeEmbedder(), FakeRepo()
    result = sync_run(s.connector(), repo, embedder=emb)
    stored = " ".join(c.content for c in repo.chunks.values()) + " ".join(t for c in emb.calls for t in c)
    assert "PUBLIC-BODY" in stored and "SECRET" not in stored and "x.txt" not in stored
    assert ("attachment", "issue", "2", set()) in repo.children_kept and result.failures == 0
    assert repo.chunks[("attachment", "11", 0)].parent_id == "1"


def test_news_attachments_are_read_every_run_and_reset_per_news():
    s = Server(news={5: (3, [att(31, "memo.txt")]), 6: (99, [att(32, "x.txt")]), 7: (3, [])},
               bodies={31: b"NEWS-BODY", 32: b"x"})
    items = run(s, project_ids={3}, since=datetime(2030, 1, 1, tzinfo=timezone.utc))
    d = docs_of(items)
    assert [(x.source_id, x.parent_type, x.parent_id, x.project_id) for x in d] == [("31", "news", "5", 3)]
    assert d[0].sections[1].text == "NEWS-BODY"
    assert items[-2:] == [ChildrenReset("attachment", "news", "5", frozenset({"31"})),
                          ChildrenReset("attachment", "news", "7", frozenset())]


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_download_failure_of_one_attachment_keeps_its_metadata_and_does_not_fail_the_run(status, caplog):
    s = Server([listed(7, [att(11, "bad.txt"), att(12, "ok.txt")])], bodies={11: status, 12: b"fine"})
    items = run(s)
    d = docs_of(items)
    assert [(x.source_id, len(x.sections)) for x in d] == [("11", 1), ("12", 2)]  # bad: name only
    assert items[-1].keep_source_ids == frozenset({"11", "12"})
    assert "SECRET-KEY" not in caplog.text


def test_download_unauthorized_fails_the_run():
    from redmine_embedding.common.redmine.errors import RedmineAuthError

    with pytest.raises(RedmineAuthError):
        run(Server([listed(7, [att(11, "a.txt")])], bodies={11: 401}))


def test_download_failure_does_not_stop_the_sync_and_last_synced_advances():
    from test_sync import FakeEmbedder, FakeRepo, run as sync_run

    s = Server([listed(7, [att(11, "bad.txt")])], bodies={11: 500})
    repo = FakeRepo()
    result = sync_run(s.connector(), repo, embedder=FakeEmbedder())
    assert result.failures == 0 and ("attachment", "11", 0) in repo.chunks


def test_issue_public_in_listing_but_private_in_detail_resets_its_children():
    s = Server([listed(7, [att(11, "a.txt")])], bodies={11: b"x"}, private_in_detail={7})
    items = run(s)
    assert items == [ChildrenReset("attachment", "issue", "7", frozenset())]
    assert s.downloads == []


def test_missing_attachments_key_does_not_reset_anything(caplog):
    no_key = listed(7)
    del no_key["attachments"]
    s = Server([no_key, listed(8, [att(11, "a.txt")])], bodies={11: b"x"})
    items = run(s)
    assert ChildrenReset("attachment", "issue", "7", frozenset()) not in items
    assert [d.source_id for d in docs_of(items)] == ["11"]
    assert "no 'attachments' key" in caplog.text


@pytest.mark.parametrize("name,body", [
    ("notes.txt", b"intro\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"),
    ("memo.txt", b"-----BEGIN RSA PRIVATE KEY-----\nabc"),
    ("id_ed25519", b"anything"),
    ("prod.env", b"A=1"),
])
def test_secret_looking_attachments_keep_only_their_metadata(name, body):
    items = run(Server([listed(7, [att(11, name)])], bodies={11: body}))
    d = docs_of(items)[0]
    assert len(d.sections) == 1 and "PRIVATE KEY" not in d.sections[0].text
