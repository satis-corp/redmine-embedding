"""PostgreSQL + pgvector schema.

The vector dimension depends on the embedding model, so the DDL is rendered
with the dimension. The model name / dimension used to build the index are
recorded in ``index_meta`` so a model switch (which needs a full re-embed) is
detected instead of silently mixing vector spaces.

``embedding_sparse`` for hybrid search (BGE-M3) can be added later with
``ALTER TABLE ... ADD COLUMN``; nothing here depends on its absence.
"""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

TABLE = "redmine_chunks"


class IndexMismatchError(RuntimeError):
    """The existing index was built with a different model / dimension."""


def _ddl(dim: int) -> list[str]:
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            source_type    TEXT        NOT NULL,   -- issue / wiki / news / document / ...
            source_id      TEXT        NOT NULL,   -- id or page identifier (set by the connector)
            chunk_no       INTEGER     NOT NULL,
            project_id     INTEGER     NOT NULL,   -- prefilter key
            tracker_id     INTEGER,                -- issues only
            parent_type    TEXT,                   -- attachments: owner type
            parent_id      TEXT,                   -- attachments: owner id
            author_id      INTEGER,
            assigned_to_id INTEGER,
            is_private     BOOLEAN     NOT NULL DEFAULT FALSE,  -- always FALSE today (private data is not indexed)
            title          TEXT        NOT NULL DEFAULT '',
            url_path       TEXT,                   -- e.g. /issues/12#note-3
            updated_on     TIMESTAMPTZ NOT NULL,   -- of the source record
            content        TEXT        NOT NULL,   -- original text for display
            embedding      vector({int(dim)}) NOT NULL,
            PRIMARY KEY (source_type, source_id, chunk_no)
        )
        """,
        # existing DBs created before is_private was added
        f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS is_private BOOLEAN NOT NULL DEFAULT FALSE",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_project_id_idx ON {TABLE} (project_id)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_parent_idx ON {TABLE} (parent_type, parent_id)",
        f"""
        CREATE INDEX IF NOT EXISTS {TABLE}_embedding_idx
            ON {TABLE} USING hnsw (embedding vector_cosine_ops)
        """,
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            source_type    TEXT PRIMARY KEY,
            last_synced_on TIMESTAMPTZ NOT NULL
        )
        """,
    ]


def init_schema(conn: psycopg.Connection, *, model_name: str, dimension: int) -> None:
    """Create the schema if missing; refuse to continue on a model mismatch."""
    with conn.transaction():
        for stmt in _ddl(dimension):
            conn.execute(stmt)
            if stmt.startswith("CREATE EXTENSION"):
                register_vector(conn)
        row = conn.execute("SELECT value FROM index_meta WHERE key = 'embedding_model'").fetchone()
        dim_row = conn.execute("SELECT value FROM index_meta WHERE key = 'embedding_dimension'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO index_meta (key, value) VALUES ('embedding_model', %s), ('embedding_dimension', %s)",
                (model_name, str(dimension)),
            )
        elif row[0] != model_name or dim_row is None or int(dim_row[0]) != dimension:
            raise IndexMismatchError(
                f"index was built with {row[0]} (dim {dim_row[0] if dim_row else '?'}), "
                f"but {model_name} (dim {dimension}) was requested; re-embed everything to switch models"
            )
