from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from redmine_embedding.batch.sources.base import Section, SourceDocument
from redmine_embedding.batch.sync import sync_source
from redmine_embedding.common.embedding.base import Embedder
from redmine_embedding.common.redmine.errors import RedmineError


class FakeEmbedder(Embedder):
    model_name = "fake"
    dimension = 2

    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class FakeRepo:
    def __init__(self, last=None):
        self.chunks = {}
        self.deleted_sources = []
        self.surplus = []
        self.children_deleted = []
        self.children_kept = []
        self.last = last

    def transaction(self):
        return nullcontext()

    def get_last_synced(self, source_type):
        return self.last

    def set_last_synced(self, source_type, when):
        self.last = when

    def upsert_chunks(self, chunks):
        for c in chunks:
            self.chunks[(c.source_type, c.source_id, c.chunk_no)] = c

    def delete_surplus_chunks(self, source_type, source_id, keep):
        self.surplus.append((source_id, keep))

    def delete_source(self, source_type, source_id):
        self.deleted_sources.append(source_id)

    def delete_children_of(self, parent_type, parent_id):
        self.children_deleted.append((parent_type, parent_id))

    def delete_children_except(self, source_type, parent_type, parent_id, keep_source_ids):
        self.children_kept.append((source_type, parent_type, parent_id, set(keep_source_ids)))


class FakeConnector:
    source_type = "issue"

    def __init__(self, docs, error=None):
        self.docs, self.error, self.since = docs, error, "unset"

    def iter_changed(self, since):
        self.since = since
        yield from self.docs
        if self.error:
            raise self.error


def doc(i, *texts):
    return SourceDocument(
        source_type="issue", source_id=str(i), project_id=1, title=f"t{i}", url_path=f"/issues/{i}",
        updated_on=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sections=[Section(t, f"#note-{n}" if n else "") for n, t in enumerate(texts)],
    )


def run(connector, repo, **kw):
    return sync_source(connector, repo=repo, embedder=kw.pop("embedder", FakeEmbedder()), **kw)


def test_first_run_reads_everything_stores_chunks_and_records_time():
    repo, conn = FakeRepo(), FakeConnector([doc(1, "説明", "コメント"), doc(2, "別の説明")])
    result = run(conn, repo)
    assert conn.since is None
    assert (result.documents, result.chunks, result.failures) == (2, 3, 0)
    assert repo.chunks[("issue", "1", 1)].url_path == "/issues/1#note-1"
    assert repo.last is not None
    assert ("1", 2) in repo.surplus  # trailing chunks beyond the new end are cleaned up


def test_next_run_starts_from_last_sync_minus_overlap():
    last = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    conn = FakeConnector([])
    run(conn, FakeRepo(last=last), overlap=timedelta(minutes=10))
    assert conn.since == last - timedelta(minutes=10)


def test_full_ignores_last_sync():
    conn = FakeConnector([])
    run(conn, FakeRepo(last=datetime(2026, 10, 26, tzinfo=timezone.utc)), full=True)
    assert conn.since is None


def test_document_with_no_text_removes_its_chunks():
    repo = FakeRepo()
    run(FakeConnector([doc(5, "   ")]), repo)
    assert repo.deleted_sources == ["5"]


def test_embedding_is_batched_across_documents():
    emb = FakeEmbedder()
    run(FakeConnector([doc(i, "x") for i in range(5)]), FakeRepo(), embedder=emb, flush_size=100)
    assert len(emb.calls) == 1 and len(emb.calls[0]) == 5


def test_redmine_error_keeps_last_sync_time_but_stores_what_was_read():
    repo = FakeRepo()
    result = run(FakeConnector([doc(1, "a")], error=RedmineError("boom")), repo)
    assert result.failures == 1 and result.documents == 1
    assert repo.last is None  # retried from the same point next run


def test_storage_failure_counts_as_failure_and_does_not_advance_time():
    class BrokenEmbedder(FakeEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("model crashed")

    repo = FakeRepo()
    result = run(FakeConnector([doc(1, "a")]), repo, embedder=BrokenEmbedder())
    assert result.failures == 1 and repo.last is None and not repo.chunks


def test_deletion_from_connector_removes_chunks():
    from redmine_embedding.batch.sources.base import Deletion

    repo = FakeRepo()
    result = run(FakeConnector([Deletion("issue", "8"), doc(9, "x")]), repo)
    assert repo.deleted_sources == ["8"] and result.failures == 0 and ("issue", "9", 0) in repo.chunks


def test_deletion_with_delete_children_also_removes_owned_chunks():
    from redmine_embedding.batch.sources.base import Deletion

    repo = FakeRepo()
    run(FakeConnector([Deletion("issue", "8", delete_children=True), Deletion("issue", "9")]), repo)
    assert repo.deleted_sources == ["8", "9"]
    assert repo.children_deleted == [("issue", "8")]  # only the one that asked for it


def test_children_reset_keeps_listed_ids_and_embeds_nothing():
    from redmine_embedding.batch.sources.base import ChildrenReset

    repo, emb = FakeRepo(), FakeEmbedder()
    result = run(FakeConnector([ChildrenReset("attachment", "issue", "7", frozenset({"a1"}))]), repo, embedder=emb)
    assert repo.children_kept == [("attachment", "issue", "7", {"a1"})]
    assert not repo.chunks and not repo.deleted_sources and emb.calls == [] and result.failures == 0


def test_private_content_never_reaches_the_index_through_the_whole_pipeline():
    import httpx

    from redmine_embedding.batch.sources.issues import IssueConnector
    from redmine_embedding.common.redmine.client import RedmineClient

    public = {"id": 1, "project": {"id": 3}, "subject": "公開", "description": "本文", "is_private": False,
              "updated_on": "2026-10-26T01:02:31Z",
              "journals": [{"id": 1, "notes": "SECRET-NOTE", "private_notes": True},
                           {"id": 2, "notes": "公開コメント", "private_notes": False}]}
    private = {**public, "id": 2, "subject": "SECRET-ISSUE", "description": "SECRET-ISSUE", "is_private": True}

    def handler(req):
        if req.url.path == "/issues.json":
            return httpx.Response(200, json={"issues": [{"id": 1, "project": {"id": 3}}, {"id": 2, "project": {"id": 3}}],
                                             "total_count": 2})
        return httpx.Response(200, json={"issue": {1: public, 2: private}[int(req.url.path.split("/")[2].split(".")[0])]})

    emb, repo = FakeEmbedder(), FakeRepo()
    run(IssueConnector(RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler))), repo, embedder=emb)
    stored = " ".join(c.content for c in repo.chunks.values())
    embedded = " ".join(t for call in emb.calls for t in call)
    assert "SECRET" not in stored and "SECRET" not in embedded
    assert repo.chunks[("issue", "1", 1)].url_path == "/issues/1#note-2"  # numbering counts the private journal
    assert repo.deleted_sources == ["2"]  # the private issue is also purged if it was indexed before
