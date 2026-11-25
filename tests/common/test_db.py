"""Needs PostgreSQL with pgvector: set TEST_DATABASE_URL (a scratch database whose name ends with "_test")."""

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest

from redmine_embedding.common.db import (
    Chunk, ChunkRepository, IndexMismatchError, connect, init_schema,
)

pytestmark = pytest.mark.db
URL = os.environ.get("TEST_DATABASE_URL")
DIM = 4


@pytest.fixture
def conn():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    # Guard: these tests DROP the index tables, so only run on a *_test database.
    dbname = urlparse(URL).path.lstrip("/")
    if not dbname.endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL must point to a database whose name ends with '_test' (got {dbname!r})")
    c = connect(URL)
    c.execute("DROP TABLE IF EXISTS redmine_chunks, index_meta, sync_state")
    c.commit()
    init_schema(c, model_name="test-model", dimension=DIM)
    c.commit()
    yield c
    c.rollback()
    c.close()


def chunk(sid, no, project, vec):
    return Chunk(
        source_type="issue", source_id=sid, chunk_no=no, project_id=project,
        updated_on=datetime(2026, 1, 1, tzinfo=timezone.utc), content=f"{sid}-{no}",
        embedding=vec, title=f"t{sid}",
    )


def test_upsert_is_idempotent_and_search_respects_project_filter(conn):
    repo = ChunkRepository(conn)
    data = [chunk("1", 0, 10, [1, 0, 0, 0]), chunk("2", 0, 20, [1, 0, 0, 0]), chunk("3", 0, 10, [0, 1, 0, 0])]
    repo.upsert_chunks(data)
    repo.upsert_chunks(data)  # rerun: no duplicates
    assert conn.execute("SELECT count(*) FROM redmine_chunks").fetchone()[0] == 3

    hits = repo.search([1, 0, 0, 0], project_ids=[10], limit=10)
    assert [h.source_id for h in hits] == ["1", "3"]  # project 20 never appears
    assert hits[0].score == pytest.approx(1.0)
    assert repo.search([1, 0, 0, 0], project_ids=[], limit=10) == []


def test_deletes_and_surplus(conn):
    repo = ChunkRepository(conn)
    repo.upsert_chunks([chunk("1", 0, 10, [1, 0, 0, 0]), chunk("1", 1, 10, [0, 1, 0, 0])])
    assert repo.delete_surplus_chunks("issue", "1", keep=1) == 1
    assert repo.list_source_ids("issue") == {"1"}
    assert repo.delete_source("issue", "1") == 1


def test_model_mismatch_is_refused(conn):
    with pytest.raises(IndexMismatchError):
        init_schema(conn, model_name="other-model", dimension=DIM)


def test_search_without_iterative_scan_and_invalid_mode(conn):
    repo = ChunkRepository(conn)
    repo.upsert_chunks([chunk("1", 0, 10, [1, 0, 0, 0])])
    assert len(repo.search([1, 0, 0, 0], project_ids=[10], iterative_scan=None)) == 1
    with pytest.raises(ValueError):
        repo.search([1, 0, 0, 0], project_ids=[10], iterative_scan="bogus")


def test_iterative_scan_degrades_on_old_pgvector(conn, caplog):
    repo = ChunkRepository(conn)
    repo.upsert_chunks([chunk("1", 0, 10, [1, 0, 0, 0])])
    version = tuple(int(p) for p in repo.pgvector_version().split(".")[:2])
    hits = repo.search([1, 0, 0, 0], project_ids=[10], max_scan_tuples=5000)
    assert [h.source_id for h in hits] == ["1"]
    assert ("does not support hnsw.iterative_scan" in caplog.text) == (version < (0, 8))


def test_delete_children_except_only_touches_one_source_type_of_one_parent(conn):
    repo = ChunkRepository(conn)

    def child(source_type, sid, parent_id):
        c = chunk(sid, 0, 10, [1, 0, 0, 0])
        c.source_type, c.parent_type, c.parent_id = source_type, "issue", parent_id
        return c

    repo.upsert_chunks([
        child("attachment", "a1", "7"), child("attachment", "a2", "7"), child("attachment", "a3", "8"),
        child("time_entry", "t1", "7"),
    ])
    assert repo.delete_children_except("attachment", "issue", "7", {"a1"}) == 1
    left = {(r[0], r[1]) for r in conn.execute("SELECT source_type, source_id FROM redmine_chunks")}
    assert left == {("attachment", "a1"), ("attachment", "a3"), ("time_entry", "t1")}
    repo.delete_children_except("attachment", "issue", "7", set())  # parent has no attachments any more
    assert conn.execute("SELECT count(*) FROM redmine_chunks WHERE source_id = 'a1'").fetchone()[0] == 0


def test_list_source_projects_and_count_children(conn):
    repo = ChunkRepository(conn)
    a, b = chunk("v1", 0, 10, [1, 0, 0, 0]), chunk("v2", 0, 11, [1, 0, 0, 0])
    a.source_type = b.source_type = "version"
    c = chunk("a1", 0, 10, [1, 0, 0, 0])
    c.source_type, c.parent_type, c.parent_id = "attachment", "issue", "7"
    repo.upsert_chunks([a, b, c])
    assert repo.list_source_projects("version") == {"v1": 10, "v2": 11}
    assert repo.count_children_of("issue", "7") == 1


def test_iterative_scan_fills_limit_when_filter_is_selective(conn):
    """Few rows of project 10 are far from the query; project 20 crowds the HNSW candidates."""
    if tuple(int(p) for p in ChunkRepository(conn).pgvector_version().split(".")[:2]) < (0, 8):
        pytest.skip("hnsw.iterative_scan needs pgvector 0.8+")
    repo = ChunkRepository(conn)
    crowd = [chunk(f"c{i}", 0, 20, [1, 0.001 * i, 0, 0]) for i in range(400)]
    target = [chunk(f"t{i}", 0, 10, [0, 1, 0.01 * i, 0]) for i in range(5)]
    repo.upsert_chunks(crowd + target)
    conn.commit()
    conn.execute("ANALYZE redmine_chunks")
    # Make the planner use the HNSW index (post-index filter), as it does on large tables.
    conn.execute("DROP INDEX redmine_chunks_project_id_idx")
    conn.execute("SET enable_seqscan = off")
    conn.execute("SET enable_bitmapscan = off")
    query = [1, 0, 0, 0]
    without = repo.search(query, project_ids=[10], limit=5, iterative_scan=None)
    with_it = repo.search(query, project_ids=[10], limit=5)
    assert len(with_it) == 5
    assert {h.project_id for h in with_it} == {10}
    assert len(without) < 5  # ef_search (40) candidates are all project 20


def test_is_private_column_defaults_false_and_is_stored(conn):
    repo = ChunkRepository(conn)
    c = chunk("1", 0, 10, [1, 0, 0, 0])
    repo.upsert_chunks([c])
    assert conn.execute("SELECT is_private FROM redmine_chunks").fetchone()[0] is False
    c.is_private = True
    repo.upsert_chunks([c])
    assert conn.execute("SELECT is_private FROM redmine_chunks").fetchone()[0] is True
