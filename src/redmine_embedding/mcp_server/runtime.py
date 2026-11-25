"""Objects built once at server start and shared by all requests."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from ..common.config import Settings
from ..common.db.repository import ChunkRepository
from ..common.redmine.client import RedmineClient
from .auth.token import TokenSource
from .search.postfilter import FilterResult, VisibleSearcher

logger = logging.getLogger(__name__)

DB_BORROW_TIMEOUT = 5.0  # 暫定: seconds to wait for a pooled connection


class Searcher(Protocol):
    def search(
        self,
        query: str,
        *,
        token: str,
        limit: int | None = None,
        project_ids: Sequence[int] | None = None,
        source_types: Sequence[str] | None = None,
        deadline: float | None = None,
    ) -> FilterResult: ...


@dataclass
class AppRuntime:
    settings: Settings
    searcher: Searcher
    token_source: TokenSource
    close: object = None  # optional callable run at shutdown


def source_types() -> list[str]:
    from ..batch.sources import CONNECTORS

    return list(CONNECTORS)


def build_runtime(settings: Settings, token_source: TokenSource) -> AppRuntime:
    """Open the DB pool, load the embedding model and build the searcher."""
    from ..common.db.repository import create_pool
    from ..common.embedding.ruri import RuriV3Embedder
    from .search.embed_limit import LimitedEmbedder
    from .search.prefilter import PrefilterSearcher

    if not settings.redmine_url or not settings.database_url:
        raise RuntimeError("REDMINE_URL と DATABASE_URL が必要です")
    pool = create_pool(settings.database_url, max_size=settings.mcp_db_pool_max)
    try:
        embedder = LimitedEmbedder(
            RuriV3Embedder(
                settings.embedding_model,
                device=settings.embedding_device,
                batch_size=settings.embedding_batch_size,
            ),
            settings.mcp_embed_concurrency,
        )
        embedder.embed_query("warmup")

        @contextmanager
        def repo_provider() -> Iterator[ChunkRepository]:
            with pool.connection(timeout=DB_BORROW_TIMEOUT) as conn:
                yield ChunkRepository(conn)

        def client_factory(token: str) -> RedmineClient:
            return RedmineClient(
                settings.redmine_url,
                access_token=token,
                timeout=settings.mcp_redmine_timeout,
                max_retries=settings.mcp_redmine_max_retries,
            )

        prefilter = PrefilterSearcher.from_settings(settings, None, embedder, client_factory, repo_provider=repo_provider)
        searcher = VisibleSearcher.from_settings(settings, prefilter, client_factory)
    except BaseException:
        pool.close()
        raise
    return AppRuntime(settings, searcher, token_source, close=pool.close)


__all__ = ["AppRuntime", "Searcher", "build_runtime", "source_types"]
