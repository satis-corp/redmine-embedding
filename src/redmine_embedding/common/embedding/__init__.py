from __future__ import annotations

from ..config import Settings
from .base import Embedder


def create_embedder(settings: Settings) -> Embedder:
    """Build the embedder selected by settings (Ruri v3 only for now)."""
    from .ruri import RuriV3Embedder

    return RuriV3Embedder(
        settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )


__all__ = ["Embedder", "create_embedder"]
