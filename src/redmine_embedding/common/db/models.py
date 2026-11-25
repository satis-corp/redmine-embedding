from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Chunk:
    source_type: str
    source_id: str
    chunk_no: int
    project_id: int
    updated_on: datetime
    content: str
    embedding: list[float]
    title: str = ""
    url_path: str | None = None
    tracker_id: int | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    author_id: int | None = None
    assigned_to_id: int | None = None
    # Private data is not indexed today; kept for future extension.
    is_private: bool = False


@dataclass(slots=True)
class SearchHit:
    source_type: str
    source_id: str
    chunk_no: int
    project_id: int
    title: str
    content: str
    url_path: str | None
    score: float  # cosine similarity, higher is closer
    parent_type: str | None = None
    parent_id: str | None = None
