"""Limit concurrent query embeddings (CPU bound)."""

from __future__ import annotations

import threading
from collections.abc import Sequence

from ...common.embedding.base import Embedder


class LimitedEmbedder(Embedder):
    """Wraps an embedder so at most ``concurrency`` calls run at once."""

    def __init__(self, inner: Embedder, concurrency: int = 2) -> None:
        self._inner = inner
        self._sem = threading.Semaphore(max(1, concurrency))
        self.model_name = inner.model_name

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        with self._sem:
            return self._inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        with self._sem:
            return self._inner.embed_query(text)
