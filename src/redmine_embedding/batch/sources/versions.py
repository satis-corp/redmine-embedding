"""Version connector: name + description of each version (milestone).

``/projects/<id>/versions.json`` has no server-side ``updated_on`` filter, so the
lists are read in full and filtered on the client. A shared version appears in
several projects' lists: it is deduplicated by id and filed under its owner.
A version whose description is empty is still yielded (name only) so that
emptying the description is a normal update.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime

from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineForbiddenError, RedmineNotFoundError
from .base import Section, SourceDocument, parse_redmine_datetime


class VersionConnector:
    source_type = "version"

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
        seen: set[int] = set()
        for project in self._client.list_projects(statuses=self._statuses):
            if self._project_ids is not None and project["id"] not in self._project_ids:
                continue
            try:
                versions = self._client.get(f"/projects/{project['identifier']}/versions.json")["versions"]
            except (RedmineNotFoundError, RedmineForbiddenError):
                continue  # module disabled for this project
            for v in versions:
                if v["id"] in seen:
                    continue
                seen.add(v["id"])
                owner = (v.get("project") or {}).get("id", project["id"])
                if self._project_ids is not None and owner not in self._project_ids:
                    continue
                updated = parse_redmine_datetime(v["updated_on"])
                if since is not None and updated < since:
                    continue
                name = (v.get("name") or "").strip()
                description = (v.get("description") or "").strip()
                yield SourceDocument(
                    source_type=self.source_type,
                    source_id=str(v["id"]),
                    project_id=owner,
                    title=name,
                    url_path=f"/versions/{v['id']}",
                    updated_on=updated,
                    sections=[Section(f"{name}\n\n{description}" if description else name)],
                )
