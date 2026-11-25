"""Issue connector: subject + description, and one section per public comment."""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator
from datetime import datetime, timezone
from typing import Any

from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineNotFoundError
from .base import Deletion, Section, SourceDocument, parse_redmine_datetime

logger = logging.getLogger(__name__)


class PrivacyUnknownError(RuntimeError):
    """The API response does not say whether a record is private."""


def _fmt_since(since: datetime) -> str:
    return since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IssueConnector:
    source_type = "issue"

    def __init__(self, client: RedmineClient, project_ids: Collection[int] | None = None) -> None:
        self._client = client
        # When given, issues of any other project (e.g. archived ones that leak
        # into the cross-project listing) are skipped.
        self._project_ids = None if project_ids is None else frozenset(project_ids)

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument | Deletion]:
        params: dict[str, Any] = {
            "status_id": "*",  # closed issues too
            # Sorted by id, not updated_on: an issue edited while we page would
            # jump to the end of an updated_on ordering and make the next page
            # skip a neighbour. With id order the worst case is a duplicate.
            "sort": "id:asc",
        }
        if since is not None:
            params["updated_on"] = ">=" + _fmt_since(since)
        for item in self._client.paginate("/issues.json", "issues", params):
            project_id = (item.get("project") or {}).get("id")
            if self._project_ids is not None and project_id not in self._project_ids:
                continue
            doc = self._load(item["id"])
            if doc is not None:
                yield doc

    def _load(self, issue_id: int) -> SourceDocument | Deletion | None:
        # Journals are only returned by the single-issue endpoint.
        try:
            issue = self._client.get(f"/issues/{issue_id}.json", {"include": "journals"})["issue"]
        except RedmineNotFoundError:
            return None  # deleted or moved out of reach since the listing
        doc = build_issue_document(issue)
        if doc is None:
            # Made private since it was indexed: remove what we have, including
            # the chunks of its attachments / time entries (children).
            return Deletion(self.source_type, str(issue_id), delete_children=True)
        return doc


def build_issue_document(issue: dict[str, Any]) -> SourceDocument | None:
    """Convert an issue payload; None if the issue is private (never indexed)."""
    if "is_private" not in issue:
        # Fail closed: without this flag a private issue could be indexed.
        raise PrivacyUnknownError(
            f"issue #{issue.get('id')}: the Redmine API response has no 'is_private' field; "
            "refusing to index (check the Redmine version / API)"
        )
    if issue["is_private"]:
        return None

    subject = (issue.get("subject") or "").strip()
    description = (issue.get("description") or "").strip()
    head = f"{subject}\n\n{description}" if description else subject
    sections = [Section(head)]

    # "#note-N" counts every journal, notes or not. With an admin key private
    # notes are counted too, so N can differ from what a normal user sees.
    for index, journal in enumerate(issue.get("journals") or [], start=1):
        if journal.get("private_notes"):
            continue
        notes = (journal.get("notes") or "").strip()
        if notes:
            sections.append(Section(notes, url_fragment=f"#note-{index}"))

    return SourceDocument(
        source_type="issue",
        source_id=str(issue["id"]),
        project_id=issue["project"]["id"],
        title=subject,
        url_path=f"/issues/{issue['id']}",
        updated_on=parse_redmine_datetime(issue["updated_on"]),
        sections=sections,
        tracker_id=(issue.get("tracker") or {}).get("id"),
        author_id=(issue.get("author") or {}).get("id"),
        assigned_to_id=(issue.get("assigned_to") or {}).get("id"),
    )
