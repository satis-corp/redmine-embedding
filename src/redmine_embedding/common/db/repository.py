from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from .models import Chunk, SearchHit
from .schema import TABLE

logger = logging.getLogger(__name__)

_ITERATIVE_SCAN_MODES = {"relaxed_order", "strict_order"}
_ITERATIVE_SCAN_MIN_VERSION = (0, 8)


def connect(database_url: str, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection with the pgvector type registered.

    On a brand-new database the ``vector`` extension does not exist yet, so
    registration is skipped here; ``init_schema`` creates the extension and
    registers the type afterwards.
    """
    conn = psycopg.connect(database_url, autocommit=autocommit)
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        conn.rollback()
    return conn


def create_pool(database_url: str, *, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    """Connection pool for the MCP server; every connection gets the vector type.

    Call ``init_schema`` (once, e.g. from the batch) before opening the pool,
    otherwise the ``vector`` type does not exist yet and registration fails.
    """
    return ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        configure=register_vector,
        open=True,
    )


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split(".") if p.isdigit())


_UPSERT = f"""
INSERT INTO {TABLE} (
    source_type, source_id, chunk_no, project_id, tracker_id, parent_type, parent_id,
    author_id, assigned_to_id, is_private, title, url_path, updated_on, content, embedding
) VALUES (
    %(source_type)s, %(source_id)s, %(chunk_no)s, %(project_id)s, %(tracker_id)s,
    %(parent_type)s, %(parent_id)s, %(author_id)s, %(assigned_to_id)s, %(is_private)s, %(title)s,
    %(url_path)s, %(updated_on)s, %(content)s, %(embedding)s
)
ON CONFLICT (source_type, source_id, chunk_no) DO UPDATE SET
    project_id = EXCLUDED.project_id, tracker_id = EXCLUDED.tracker_id,
    parent_type = EXCLUDED.parent_type, parent_id = EXCLUDED.parent_id,
    author_id = EXCLUDED.author_id, assigned_to_id = EXCLUDED.assigned_to_id,
    is_private = EXCLUDED.is_private,
    title = EXCLUDED.title, url_path = EXCLUDED.url_path,
    updated_on = EXCLUDED.updated_on, content = EXCLUDED.content,
    embedding = EXCLUDED.embedding
"""


class ChunkRepository:
    """Chunk storage and search. Does not commit: the caller owns the transaction."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def transaction(self):
        """Context manager for one atomic unit of writes.

        Commits on exit only if the connection is in autocommit mode (the batch
        opens it that way); otherwise it is a savepoint and the caller commits.
        """
        return self._conn.transaction()

    # -- write side (batch) ------------------------------------------------
    def upsert_chunks(self, chunks: Iterable[Chunk]) -> int:
        rows = [
            {
                "source_type": c.source_type, "source_id": c.source_id, "chunk_no": c.chunk_no,
                "project_id": c.project_id, "tracker_id": c.tracker_id,
                "parent_type": c.parent_type, "parent_id": c.parent_id,
                "author_id": c.author_id, "assigned_to_id": c.assigned_to_id, "is_private": c.is_private,
                "title": c.title, "url_path": c.url_path, "updated_on": c.updated_on,
                "content": c.content, "embedding": np.asarray(c.embedding, dtype=np.float32),
            }
            for c in chunks
        ]
        if rows:
            with self._conn.cursor() as cur:
                cur.executemany(_UPSERT, rows)
        return len(rows)

    def delete_source(self, source_type: str, source_id: str) -> int:
        """Delete every chunk of one record (deleted / made private upstream)."""
        cur = self._conn.execute(
            f"DELETE FROM {TABLE} WHERE source_type = %s AND source_id = %s", (source_type, source_id)
        )
        return cur.rowcount

    def delete_children_of(self, parent_type: str, parent_id: str) -> int:
        """Delete attachment chunks whose owner is gone."""
        cur = self._conn.execute(
            f"DELETE FROM {TABLE} WHERE parent_type = %s AND parent_id = %s", (parent_type, parent_id)
        )
        return cur.rowcount

    def count_children_of(self, parent_type: str, parent_id: str) -> int:
        """How many chunks ``delete_children_of`` would delete (for dry-run reports)."""
        row = self._conn.execute(
            f"SELECT count(*) FROM {TABLE} WHERE parent_type = %s AND parent_id = %s", (parent_type, parent_id)
        ).fetchone()
        return row[0]

    def delete_children_except(
        self, source_type: str, parent_type: str, parent_id: str, keep_source_ids: Collection[str]
    ) -> int:
        """Delete ``source_type`` chunks owned by one parent whose id is not in ``keep_source_ids``."""
        cur = self._conn.execute(
            f"DELETE FROM {TABLE} WHERE source_type = %s AND parent_type = %s AND parent_id = %s "
            "AND NOT (source_id = ANY(%s))",
            (source_type, parent_type, parent_id, list(keep_source_ids)),
        )
        return cur.rowcount

    def delete_surplus_chunks(self, source_type: str, source_id: str, keep: int) -> int:
        """Drop chunk_no >= keep after a record got shorter."""
        cur = self._conn.execute(
            f"DELETE FROM {TABLE} WHERE source_type = %s AND source_id = %s AND chunk_no >= %s",
            (source_type, source_id, keep),
        )
        return cur.rowcount

    def delete_project(self, project_id: int) -> int:
        """Drop everything of a project (e.g. archived)."""
        cur = self._conn.execute(f"DELETE FROM {TABLE} WHERE project_id = %s", (project_id,))
        return cur.rowcount

    def list_source_ids(self, source_type: str) -> set[str]:
        """All indexed ids of a type, for reconciliation against Redmine."""
        rows = self._conn.execute(
            f"SELECT DISTINCT source_id FROM {TABLE} WHERE source_type = %s", (source_type,)
        ).fetchall()
        return {r[0] for r in rows}

    def list_source_projects(self, source_type: str) -> dict[str, int]:
        """Indexed ids of a type with the project that owns them, for per-project reconciliation."""
        rows = self._conn.execute(
            f"SELECT DISTINCT source_id, project_id FROM {TABLE} WHERE source_type = %s", (source_type,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def list_project_ids(self) -> dict[int, int]:
        """Indexed project ids with their chunk counts, for archive reconciliation."""
        rows = self._conn.execute(
            f"SELECT project_id, count(*) FROM {TABLE} GROUP BY project_id"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_last_synced(self, source_type: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT last_synced_on FROM sync_state WHERE source_type = %s", (source_type,)
        ).fetchone()
        return row[0] if row else None

    def set_last_synced(self, source_type: str, when: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (source_type, last_synced_on) VALUES (%s, %s)
            ON CONFLICT (source_type) DO UPDATE SET last_synced_on = EXCLUDED.last_synced_on
            """,
            (source_type, when),
        )

    # -- read side (MCP) ---------------------------------------------------
    def search(
        self,
        query_embedding: Sequence[float],
        *,
        project_ids: Sequence[int],
        source_types: Sequence[str] | None = None,
        limit: int = 50,
        iterative_scan: str | None = "relaxed_order",
        max_scan_tuples: int | None = None,
    ) -> list[SearchHit]:
        """Cosine-similarity search restricted to ``project_ids`` (prefilter).

        ``limit`` should be generous: the caller still runs the Redmine
        visibility check (postfilter) and may drop some hits.

        ``iterative_scan`` ("relaxed_order" / "strict_order" / None) needs
        pgvector 0.8+; it keeps the HNSW scan going until enough rows pass the
        project filter, up to ``max_scan_tuples`` (pgvector default 20000). On an
        older pgvector it is skipped with a warning, and filtered searches may
        return fewer than ``limit`` rows.
        """
        if iterative_scan is not None and iterative_scan not in _ITERATIVE_SCAN_MODES:
            raise ValueError(f"invalid iterative_scan: {iterative_scan!r}")
        if not project_ids:
            return []
        vec = np.asarray(query_embedding, dtype=np.float32)
        where = "project_id = ANY(%(project_ids)s)"
        params: dict[str, object] = {"vec": vec, "project_ids": list(project_ids), "limit": limit}
        if source_types:
            where += " AND source_type = ANY(%(source_types)s)"
            params["source_types"] = list(source_types)
        # relaxed_order may return slightly out-of-order rows, so re-sort outside.
        sql = f"""
            WITH nearest AS MATERIALIZED (
                SELECT source_type, source_id, chunk_no, project_id, title, content, url_path,
                       parent_type, parent_id, embedding <=> %(vec)s AS distance
                FROM {TABLE}
                WHERE {where}
                ORDER BY embedding <=> %(vec)s
                LIMIT %(limit)s
            )
            SELECT source_type, source_id, chunk_no, project_id, title, content, url_path,
                   parent_type, parent_id, 1 - distance AS score
            FROM nearest ORDER BY distance
        """
        with self._conn.transaction():
            if iterative_scan and self._iterative_scan_supported():
                self._conn.execute(f"SET LOCAL hnsw.iterative_scan = {iterative_scan}")
                if max_scan_tuples is not None:
                    self._conn.execute(
                        "SELECT set_config('hnsw.max_scan_tuples', %s, true)", (str(int(max_scan_tuples)),)
                    )
            rows = self._conn.execute(sql, params).fetchall()
        return [
            SearchHit(
                source_type=r[0], source_id=r[1], chunk_no=r[2], project_id=r[3], title=r[4],
                content=r[5], url_path=r[6], parent_type=r[7], parent_id=r[8], score=float(r[9]),
            )
            for r in rows
        ]

    def pgvector_version(self) -> str | None:
        row = self._conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        return row[0] if row else None

    def _iterative_scan_supported(self) -> bool:
        # Checked up front rather than by trying SET: on an old pgvector whose
        # library is not loaded in this session yet, an unknown hnsw.* setting
        # is accepted silently and then has no effect.
        version = self.pgvector_version()
        ok = version is not None and _parse_version(version) >= _ITERATIVE_SCAN_MIN_VERSION
        if not ok:
            logger.warning(
                "pgvector %s does not support hnsw.iterative_scan (needs 0.8+); "
                "filtered searches may return fewer results than requested",
                version,
            )
        return ok
