"""Incremental sync: Redmine -> chunks -> embeddings -> pgvector.

Safe to re-run: chunks are keyed by (source_type, source_id, chunk_no), so a
repeated or overlapping run just rewrites the same rows. The "last synced"
time only moves forward when a source finished without failures, so a failed
run is retried from the same point next time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..common.db import Chunk, ChunkRepository
from ..common.embedding import Embedder
from ..common.redmine.errors import RedmineError
from .chunking import ChunkDraft, chunk_document
from .sources.base import ChildrenReset, Connector, Deletion, SourceDocument

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    source_type: str
    documents: int = 0
    chunks: int = 0
    failures: int = 0


def sync_source(
    connector: Connector,
    *,
    repo: ChunkRepository,
    embedder: Embedder,
    full: bool = False,
    overlap: timedelta = timedelta(minutes=10),
    chunk_max_chars: int = 1000,
    flush_size: int = 64,
) -> SyncResult:
    result = SyncResult(connector.source_type)
    run_started = datetime.now(timezone.utc)
    last = None if full else repo.get_last_synced(connector.source_type)
    since = None if last is None else last - overlap
    logger.info("sync %s: since=%s", connector.source_type, since or "beginning")

    pending: list[tuple[SourceDocument | Deletion | ChildrenReset, list[ChunkDraft]]] = []
    pending_chunks = 0

    def flush() -> None:
        nonlocal pending, pending_chunks
        batch, pending, pending_chunks = pending, [], 0
        if not batch:
            return
        try:
            texts = [d.embed_text for _, drafts in batch for d in drafts]
            vectors = iter(embedder.embed_documents(texts) if texts else [])
            with repo.transaction():
                for doc, drafts in batch:
                    if isinstance(doc, Deletion):
                        repo.delete_source(doc.source_type, doc.source_id)
                        if doc.delete_children:
                            repo.delete_children_of(doc.source_type, doc.source_id)
                    elif isinstance(doc, ChildrenReset):
                        repo.delete_children_except(
                            doc.source_type, doc.parent_type, doc.parent_id, doc.keep_source_ids
                        )
                    elif drafts:
                        repo.upsert_chunks(
                            Chunk(
                                source_type=doc.source_type,
                                source_id=doc.source_id,
                                chunk_no=d.chunk_no,
                                project_id=doc.project_id,
                                updated_on=doc.updated_on,
                                content=d.content,
                                embedding=next(vectors),
                                title=doc.title,
                                url_path=doc.url_path + d.url_fragment,
                                tracker_id=doc.tracker_id,
                                parent_type=doc.parent_type,
                                parent_id=doc.parent_id,
                                author_id=doc.author_id,
                                assigned_to_id=doc.assigned_to_id,
                            )
                            for d in drafts
                        )
                        # The record got shorter: drop the chunks beyond the new end.
                        repo.delete_surplus_chunks(doc.source_type, doc.source_id, len(drafts))
                    else:
                        # Nothing left to index (e.g. description emptied).
                        repo.delete_source(doc.source_type, doc.source_id)
            result.documents += len(batch)
            result.chunks += sum(len(drafts) for _, drafts in batch)
        except Exception:
            logger.exception("failed to store %d document(s) of %s", len(batch), connector.source_type)
            result.failures += len(batch)

    try:
        for doc in connector.iter_changed(since):
            drafts = [] if isinstance(doc, (Deletion, ChildrenReset)) else chunk_document(doc, chunk_max_chars)
            pending.append((doc, drafts))
            pending_chunks += len(drafts)
            if pending_chunks >= flush_size:
                flush()
    except RedmineError:
        # Network / HTTP trouble: stop this source and retry it next run.
        logger.exception("reading %s from Redmine failed", connector.source_type)
        result.failures += 1
    flush()

    if result.failures == 0:
        repo.set_last_synced(connector.source_type, run_started)
    else:
        logger.warning(
            "sync %s: %d failure(s); last-synced time not advanced", connector.source_type, result.failures
        )
    logger.info("sync %s: %d document(s), %d chunk(s)", connector.source_type, result.documents, result.chunks)
    return result
