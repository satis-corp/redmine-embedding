"""Time entry connector: the comment of each time entry, filed under its issue.

PROVISIONAL: only entries with a
non-empty comment are indexed; the rest are yielded as ``Deletion`` so a comment
that was emptied later leaves the index.

``/time_entries.json`` ignores every ``updated_on`` / ``created_on`` filter and
``updated_on`` sorting (measured: always all entries), so the full list is read
every run and old entries are dropped on the client. Never send such a filter
and rely on it: the listing is only correct because we read all of it.

Entries of private issues are never indexed. The private issue ids come from
``is_private=1``; if that listing fails or looks unfiltered we raise
(``RedmineError``, counted as a failure by ``sync_source``) instead of guessing.
Entries of private issues are yielded as ``Deletion`` on every run, regardless
of ``since``, because making an issue private does not touch the entry's
``updated_on``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime

from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineError
from .base import Deletion, Section, SourceDocument, parse_redmine_datetime


class TimeEntryConnector:
    source_type = "time_entry"

    def __init__(self, client: RedmineClient, project_ids: Collection[int] | None = None) -> None:
        self._client = client
        self._project_ids = None if project_ids is None else frozenset(project_ids)

    def private_issue_ids(self) -> set[int]:
        """Ids of private issues. Raises RedmineError when they cannot be trusted (fail closed)."""
        ids: set[int] = set()
        params = {"status_id": "*", "is_private": "1", "sort": "id:asc"}
        for item in self._client.paginate("/issues.json", "issues", params):
            if item.get("is_private") is not True:
                # The filter was ignored (or the flag is missing): the set would be wrong.
                raise RedmineError(
                    "the private-issue listing is not filtered by is_private; "
                    "refusing to index time entries"
                )
            ids.add(item["id"])
        return ids

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument | Deletion]:
        private = self.private_issue_ids()
        # No updated_on parameter on purpose (see module docstring).
        for entry in self._client.paginate("/time_entries.json", "time_entries", {}):
            issue_id = (entry.get("issue") or {}).get("id")
            project_id = (entry.get("project") or {}).get("id")
            if issue_id is None or project_id is None:
                continue  # not tied to an issue: nothing to file it under
            if self._project_ids is not None and project_id not in self._project_ids:
                continue
            sid = str(entry["id"])
            if issue_id in private:
                yield Deletion(self.source_type, sid)
                continue
            updated = parse_redmine_datetime(entry["updated_on"])
            if since is not None and updated < since:
                continue
            comments = (entry.get("comments") or "").strip()
            if not comments:
                yield Deletion(self.source_type, sid)
                continue
            activity = ((entry.get("activity") or {}).get("name") or "").strip()
            yield SourceDocument(
                source_type=self.source_type,
                source_id=sid,
                project_id=project_id,
                title=f"#{issue_id} 作業時間",
                url_path=f"/issues/{issue_id}",
                updated_on=updated,
                sections=[Section(f"{activity}\n{comments}" if activity else comments)],
                author_id=(entry.get("user") or {}).get("id"),
                parent_type="issue",
                parent_id=str(issue_id),
            )
