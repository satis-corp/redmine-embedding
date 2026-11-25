"""Project connector: name, description and homepage of each project.

Per ADR 0001 non-public projects are indexed too (search narrows by project).
Archived projects are not listed; reconcile removes their chunks.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime

from ...common.redmine.client import RedmineClient
from .base import Section, SourceDocument, parse_redmine_datetime


class ProjectConnector:
    source_type = "project"

    def __init__(
        self,
        client: RedmineClient,
        project_ids: Collection[int] | None = None,
        *,
        project_statuses: tuple[int, ...] = (1, 5),
    ) -> None:
        self._client = client
        self._project_ids = None if project_ids is None else frozenset(project_ids)
        self._statuses = project_statuses

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument]:
        for p in self._client.list_projects(statuses=self._statuses):
            if self._project_ids is not None and p["id"] not in self._project_ids:
                continue
            updated = parse_redmine_datetime(p["updated_on"])
            if since is not None and updated < since:
                continue
            parts = [(p.get("name") or "").strip(), (p.get("description") or "").strip(),
                     (p.get("homepage") or "").strip()]
            yield SourceDocument(
                source_type=self.source_type,
                source_id=str(p["id"]),
                project_id=p["id"],
                title=(p.get("name") or "").strip(),
                url_path=f"/projects/{p['identifier']}",
                updated_on=updated,
                sections=[Section("\n\n".join(x for x in parts if x))],
            )
