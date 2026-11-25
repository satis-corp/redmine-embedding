"""Wiki connector: latest version of each page, one section per heading."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineForbiddenError, RedmineNotFoundError
from .base import Section, SourceDocument, parse_redmine_datetime

# Redmine renders textile (h1. ) and markdown (# ) depending on the instance setting.
_HEADING = re.compile(r"^(?:h[1-6]\.\s|#{1,6}\s)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def split_wiki_sections(text: str) -> list[Section]:
    """Split on headings, ignoring '#' lines inside code fences / <pre> blocks."""
    sections: list[list[str]] = [[]]
    in_fence = False
    in_pre = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        if "<pre" in line:
            in_pre = True
        if not in_fence and not in_pre and _HEADING.match(line) and any(l.strip() for l in sections[-1]):
            sections.append([])
        sections[-1].append(line)
        if "</pre>" in line:
            in_pre = False
    return [Section(body) for s in sections if (body := "\n".join(s).strip())]


class WikiConnector:
    source_type = "wiki"

    def __init__(
        self,
        client: RedmineClient,
        project_ids: Collection[int] | None = None,
        *,
        project_statuses: tuple[int, ...] = (1, 5),
        indexed_ids: Callable[[], Collection[str]] | None = None,
    ) -> None:
        self._client = client
        self._project_ids = None if project_ids is None else frozenset(project_ids)
        self._statuses = project_statuses  # active + closed
        # Returns indexed wiki source_ids. A rename does not change updated_on, so titles
        # missing from the index are loaded even when older than ``since`` (None = off).
        self._indexed_ids = indexed_ids

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument]:
        indexed: Collection[str] | None = None
        if since is not None and self._indexed_ids is not None:
            indexed = self._indexed_ids()  # one query per run
        for project in self._client.list_projects(statuses=self._statuses):
            if self._project_ids is not None and project["id"] not in self._project_ids:
                continue
            ident = project["identifier"]
            try:
                pages = self._client.get(f"/projects/{ident}/wiki/index.json")["wiki_pages"]
            except (RedmineNotFoundError, RedmineForbiddenError):
                continue  # wiki module disabled for this project
            for page in pages:
                if (
                    since is not None
                    and parse_redmine_datetime(page["updated_on"]) < since
                    and (indexed is None or f"{project['id']}:{page['title']}" in indexed)
                ):
                    continue
                doc = self._load(project, page["title"])
                if doc is not None:
                    yield doc

    def _load(self, project: dict[str, Any], title: str) -> SourceDocument | None:
        ident = project["identifier"]
        quoted = quote(title, safe="")
        try:
            page = self._client.get(f"/projects/{ident}/wiki/{quoted}.json")["wiki_page"]
        except RedmineNotFoundError:
            return None  # deleted or renamed since the index was read
        return SourceDocument(
            source_type="wiki",
            source_id=f"{project['id']}:{title}",
            project_id=project["id"],
            title=title.replace("_", " "),
            url_path=f"/projects/{ident}/wiki/{quoted}",
            updated_on=parse_redmine_datetime(page["updated_on"]),
            sections=split_wiki_sections(page.get("text") or ""),
            author_id=(page.get("author") or {}).get("id"),
        )
