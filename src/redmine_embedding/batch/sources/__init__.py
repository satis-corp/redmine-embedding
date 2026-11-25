from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from ...common.redmine.client import RedmineClient
from .attachments import AttachmentConnector
from .base import Connector, Section, SourceDocument
from .files import FileConnector
from .issues import IssueConnector
from .news import NewsConnector
from .projects import ProjectConnector
from .time_entries import TimeEntryConnector
from .versions import VersionConnector
from .wiki import WikiConnector

#: Add new record types here; ``--type`` on the CLI picks from these keys.
CONNECTORS: dict[str, type] = {
    "issue": IssueConnector,
    "wiki": WikiConnector,
    "attachment": AttachmentConnector,
    "news": NewsConnector,
    "version": VersionConnector,
    "project": ProjectConnector,
    "file": FileConnector,
    "time_entry": TimeEntryConnector,
}


def build_connectors(
    client: RedmineClient,
    types: list[str] | None = None,
    *,
    project_ids: Collection[int] | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Connector]:
    """Create connectors; every one is built as ``(client, project_ids, **options[type])``.

    ``project_ids`` restricts reading to the projects we index (None = no restriction).
    """
    names = types or list(CONNECTORS)
    unknown = [n for n in names if n not in CONNECTORS]
    if unknown:
        raise ValueError(f"unknown source type(s): {', '.join(unknown)}; available: {', '.join(CONNECTORS)}")
    return [CONNECTORS[n](client, project_ids, **(options or {}).get(n, {})) for n in names]


__all__ = ["CONNECTORS", "Connector", "Section", "SourceDocument", "build_connectors"]
