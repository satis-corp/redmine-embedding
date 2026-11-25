"""Prefilter search: visible projects (cached) -> embed query -> pgvector.

Returns candidates only. The postfilter (``postfilter.filter_visible``)
is mandatory: a user whose permissions were revoked still gets the old project
list for up to the cache TTL, so the prefilter alone must never be trusted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager

from ...common.config import Settings
from ...common.db.models import SearchHit
from ...common.db.repository import ChunkRepository
from ...common.embedding.base import Embedder
from ...common.redmine.client import RedmineClient
from .visible_projects import VisibleProjectsCache


class PrefilterSearcher:
    def __init__(
        self,
        repo: ChunkRepository | None,
        embedder: Embedder,
        cache: VisibleProjectsCache,
        client_factory: Callable[[str], RedmineClient],
        *,
        repo_provider: Callable[[], AbstractContextManager[ChunkRepository]] | None = None,
        top_k: int = 50,
        iterative_scan: str | None = "relaxed_order",
        max_scan_tuples: int | None = None,
    ) -> None:
        # A fixed repo (batch / tests), or a provider that lends a repo per search
        # (MCP server: a pooled connection is borrowed only while querying).
        if (repo is None) == (repo_provider is None):
            raise ValueError("pass exactly one of repo / repo_provider")
        self._repo = repo
        self._repo_provider = repo_provider
        self._embedder = embedder
        self._cache = cache
        self._client_factory = client_factory
        self._top_k = top_k
        self._iterative_scan = iterative_scan
        self._max_scan_tuples = max_scan_tuples

    @property
    def top_k(self) -> int:
        return self._top_k

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        repo: ChunkRepository | None,
        embedder: Embedder,
        client_factory: Callable[[str], RedmineClient],
        *,
        repo_provider: Callable[[], AbstractContextManager[ChunkRepository]] | None = None,
    ) -> PrefilterSearcher:
        cache = VisibleProjectsCache(
            ttl=settings.visible_projects_cache_ttl,
            max_entries=settings.visible_projects_cache_max_entries,
        )
        return cls(
            repo, embedder, cache, client_factory, top_k=settings.search_top_k, repo_provider=repo_provider
        )

    def search_candidates(
        self,
        query: str,
        *,
        token: str,
        project_ids: Sequence[int] | None = None,
        source_types: Sequence[str] | None = None,
        top_k: int | None = None,
    ) -> list[SearchHit]:
        """``token`` is the caller's own OAuth2 token. The Redmine client is built
        from it by ``client_factory``, so the cache key and the data source can
        never belong to different users.

        ``project_ids`` narrows the search to (visible ∩ requested); ids the
        user cannot see are ignored, never added. ``top_k`` overrides the
        configured candidate count (used by the postfilter's re-fetch rounds).
        """
        def fetch() -> list[int]:
            with self._client_factory(token) as client:
                return client.visible_project_ids()

        visible = self._cache.get_or_fetch(token, fetch)
        allowed = visible if project_ids is None else sorted(set(visible) & set(project_ids))
        if not allowed:
            return []
        vec = self._embedder.embed_query(query)
        kwargs = dict(
            project_ids=allowed,
            source_types=source_types,
            limit=self._top_k if top_k is None else top_k,
            iterative_scan=self._iterative_scan,
            max_scan_tuples=self._max_scan_tuples,
        )
        if self._repo is not None:
            return self._repo.search(vec, **kwargs)
        assert self._repo_provider is not None
        with self._repo_provider() as repo:
            return repo.search(vec, **kwargs)
