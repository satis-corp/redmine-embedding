"""Common shape of the per-type data connectors.

A connector knows how to read one kind of Redmine record through the REST API
and turns it into a ``SourceDocument``: metadata plus ordered text sections.
Splitting sections into embeddable chunks is the chunker's job, so connectors
stay free of size and model concerns and new types can be added one by one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class Section:
    """A logical unit of text (description, one comment, one wiki heading...)."""

    text: str
    url_fragment: str = ""  # appended to the document URL, e.g. "#note-3"


@dataclass(slots=True)
class SourceDocument:
    source_type: str
    source_id: str
    project_id: int
    title: str
    url_path: str
    updated_on: datetime
    sections: list[Section] = field(default_factory=list)
    tracker_id: int | None = None
    author_id: int | None = None
    assigned_to_id: int | None = None
    parent_type: str | None = None
    parent_id: str | None = None


@dataclass(slots=True)
class Deletion:
    """The record is no longer indexable (made private, ...): drop its chunks."""

    source_type: str
    source_id: str
    #: Also drop the chunks of records owned by this one (e.g. the attachments
    #: of an issue that was made private).
    delete_children: bool = False


@dataclass(slots=True)
class ChildrenReset:
    """A parent was re-read: drop its ``source_type`` children not in ``keep_source_ids``.

    ``source_type`` is part of the key because different connectors can share
    a parent (attachments and time entries of one issue).
    """

    source_type: str
    parent_type: str
    parent_id: str
    keep_source_ids: frozenset[str] = frozenset()


class Connector(Protocol):
    #: Value stored in ``source_type`` and keyed in ``sync_state``.
    source_type: str

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument | Deletion | ChildrenReset]:
        """Yield documents changed at or after ``since`` (everything if None).

        Private records and private comments must never be yielded as documents.
        A changed record that must no longer be indexed (e.g. it was made
        private) is yielded as a ``Deletion`` so its old chunks get removed.
        """
        ...


def parse_redmine_datetime(value: str) -> datetime:
    """Parse Redmine's ISO-8601 timestamps (``2026-10-26T01:02:31Z``) as aware datetimes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
