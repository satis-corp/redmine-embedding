"""Ruri v3 (cl-nagoya/ruri-v3-*) dense embedder.

Ruri v3 expects a task prefix on every input: ``検索文書: `` for the indexed
side and ``検索クエリ: `` for queries.
"""

from __future__ import annotations

from collections.abc import Sequence

from .base import Embedder

DOCUMENT_PREFIX = "検索文書: "
QUERY_PREFIX = "検索クエリ: "


class RuriV3Embedder(Embedder):
    def __init__(
        self,
        model_name: str = "cl-nagoya/ruri-v3-130m",
        *,
        device: str = "cpu",
        batch_size: int = 16,
    ) -> None:
        # Imported lazily: sentence-transformers (and torch) are heavy and only
        # installed with the ``batch`` / ``mcp`` extras.
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self._batch_size = batch_size
        self._model = SentenceTransformer(model_name, device=device)
        # Renamed in sentence-transformers 5.x; keep working on 3.x/4.x.
        get_dim = getattr(self._model, "get_embedding_dimension", None) or getattr(
            self._model, "get_sentence_embedding_dimension"
        )
        dim = get_dim()
        if dim is None:
            raise RuntimeError(f"cannot determine embedding dimension of {model_name}")
        self._dim = int(dim)

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode([DOCUMENT_PREFIX + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([QUERY_PREFIX + text])[0]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vecs.tolist()
