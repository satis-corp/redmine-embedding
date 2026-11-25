"""File connector: the "Files" tab of each project (``/projects/<id>/files.json``).

Files have no ``updated_on`` (description edits cannot be tracked), so every run
reads every file. Text files get their body indexed with the same limits,
exclusions and extractor as attachments (PROVISIONAL values); other
files are indexed by file name and description only.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime, timezone

from ...common.config import Settings
from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineForbiddenError, RedmineNotFoundError
from .attachments import TextExtractor, read_attachment_text
from .base import Section, SourceDocument, parse_redmine_datetime
from .textfile import decode_text

_DEFAULT_MAX_BYTES: int = Settings.model_fields["attachment_max_bytes"].default
_DEFAULT_MAX_CHARS: int = Settings.model_fields["attachment_text_max_chars"].default


class FileConnector:
    source_type = "file"

    def __init__(
        self,
        client: RedmineClient,
        project_ids: Collection[int] | None = None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_chars: int = _DEFAULT_MAX_CHARS,
        extract: TextExtractor = decode_text,
        project_statuses: tuple[int, ...] = (1, 5),
    ) -> None:
        self._client = client
        self._project_ids = None if project_ids is None else frozenset(project_ids)
        self._max_bytes, self._max_chars, self._extract = max_bytes, max_chars, extract
        self._statuses = project_statuses

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument]:
        seen: set[int] = set()
        for project in self._client.list_projects(statuses=self._statuses):
            if self._project_ids is not None and project["id"] not in self._project_ids:
                continue
            try:
                files = self._client.get(f"/projects/{project['identifier']}/files.json")["files"]
            except (RedmineNotFoundError, RedmineForbiddenError):
                continue  # module disabled for this project
            for f in files:
                if f["id"] in seen:
                    continue
                seen.add(f["id"])
                yield self._build(f, project["id"])

    def _build(self, f: dict, project_id: int) -> SourceDocument:
        filename = f.get("filename") or ""
        description = (f.get("description") or "").strip()
        sections = [Section(f"{filename}\n{description}" if description else filename)]
        body = read_attachment_text(self._client, f, self._max_bytes, self._max_chars, self._extract)
        if body:
            sections.append(Section(body))
        created = f.get("created_on")
        return SourceDocument(
            source_type=self.source_type,
            source_id=str(f["id"]),
            project_id=project_id,
            title=filename,
            url_path=f"/attachments/{f['id']}",
            updated_on=parse_redmine_datetime(created) if created else datetime(1970, 1, 1, tzinfo=timezone.utc),
            sections=sections,
            author_id=(f.get("author") or {}).get("id"),
            parent_type="project",
            parent_id=str(project_id),
        )
