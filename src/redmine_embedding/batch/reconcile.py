"""Reflect what incremental sync cannot see: archived projects and deleted records.

``updated_on`` only moves when a record is edited, so a deleted issue, a deleted
wiki page or a whole archived project never shows up as "changed". This pass
compares what is indexed with what Redmine lists today and removes the rest.

Records own children (the attachments and time entries of an issue, the
attachments of a wiki page or news item): when a parent goes, its children go
with it (``delete_children_of``).

Deletion is the dangerous direction, so every check fails closed:
  * a failed or empty project listing deletes nothing;
  * a listing that failed half way deletes nothing for that record type;
  * more than ``max_delete_ratio`` of the index going at once is refused;
  * nothing is deleted unless ``apply=True`` (default is a dry run).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..common.db import ChunkRepository
from ..common.redmine.client import RedmineClient
from ..common.redmine.errors import RedmineError, RedmineForbiddenError, RedmineNotFoundError

logger = logging.getLogger(__name__)

ACTIVE, CLOSED = 1, 5

#: Record types compared with Redmine. ``attachment`` is not listed: its ids are
#: not cheap to list, so it is cleaned through its parents (and ChildrenReset).
PARENT_TYPES = frozenset({"issue", "wiki", "news", "project"})
ALL_SOURCE_TYPES = ("issue", "wiki", "news", "project", "version", "file", "time_entry")


@dataclass(slots=True)
class ReconcileResult:
    applied: bool
    valid_project_ids: set[int] = field(default_factory=set)
    #: project_id -> chunk count that is (or would be) deleted
    stale_projects: dict[int, int] = field(default_factory=dict)
    #: source_type -> source ids that are (or would be) deleted
    stale_records: dict[str, set[str]] = field(default_factory=dict)
    #: reasons a check was skipped (nothing deleted for it)
    skipped: list[str] = field(default_factory=list)
    deleted_chunks: int = 0
    #: child chunks (attachments / time entries) that go with the stale records
    stale_child_chunks: int = 0

    @property
    def aborted(self) -> bool:
        return not self.valid_project_ids


def fetch_valid_projects(client: RedmineClient) -> list[dict[str, Any]]:
    """Active + closed projects. Raises RedmineError on failure; empty is an error too."""
    projects = client.list_projects(statuses=(ACTIVE, CLOSED))
    if not projects:
        raise RedmineError("project listing is empty; refusing to treat every project as archived")
    return projects


def reconcile(
    client: RedmineClient,
    repo: ChunkRepository,
    *,
    apply: bool = False,
    source_types: tuple[str, ...] = ALL_SOURCE_TYPES,
    max_delete_ratio: float = 0.5,
) -> ReconcileResult:
    result = ReconcileResult(applied=apply)
    try:
        projects = fetch_valid_projects(client)
    except RedmineError as e:
        result.skipped.append(f"project listing failed ({e}); nothing deleted")
        logger.error("reconcile: %s", result.skipped[-1])
        return result
    result.valid_project_ids = {p["id"] for p in projects}

    indexed = repo.list_project_ids()
    result.stale_projects = {pid: n for pid, n in indexed.items() if pid not in result.valid_project_ids}
    total = sum(indexed.values())
    stale_total = sum(result.stale_projects.values())
    if total and stale_total / total > max_delete_ratio:
        result.skipped.append(
            f"{stale_total}/{total} chunks belong to unlisted projects (> {max_delete_ratio:.0%}); "
            "looks like a bad listing, nothing deleted"
        )
        logger.error("reconcile: %s", result.skipped[-1])
        result.stale_projects = {}
        return result

    if "issue" in source_types:
        _stale_issues(client, repo, result, max_delete_ratio)
    if "wiki" in source_types:
        _stale_wiki(client, repo, projects, result, max_delete_ratio)
    if "news" in source_types:
        _stale_news(client, repo, result, max_delete_ratio)
    if "project" in source_types:
        _stale_projects(repo, projects, result, max_delete_ratio)
    if "version" in source_types:
        _stale_per_project(client, repo, projects, result, max_delete_ratio, "version", "versions")
    if "file" in source_types:
        _stale_per_project(client, repo, projects, result, max_delete_ratio, "file", "files")
    if "time_entry" in source_types:
        _stale_time_entries(client, repo, result, max_delete_ratio)

    for stype, ids in result.stale_records.items():
        if stype in PARENT_TYPES:
            result.stale_child_chunks += sum(repo.count_children_of(stype, sid) for sid in ids)
    if result.stale_child_chunks:
        logger.info(
            "reconcile: %d child chunk(s) of stale records %s",
            result.stale_child_chunks, "deleted" if apply else "would be deleted",
        )

    for pid, n in result.stale_projects.items():
        logger.info("reconcile: project %s not listed: %d chunk(s) %s", pid, n, "deleted" if apply else "would be deleted")
    for stype, ids in result.stale_records.items():
        logger.info("reconcile: %s: %d record(s) %s", stype, len(ids), "deleted" if apply else "would be deleted")

    if apply:
        with repo.transaction():
            for pid in result.stale_projects:
                result.deleted_chunks += repo.delete_project(pid)
            for stype, ids in result.stale_records.items():
                for sid in ids:
                    result.deleted_chunks += repo.delete_source(stype, sid)
                    if stype in PARENT_TYPES:  # attachments / time entries owned by it
                        result.deleted_chunks += repo.delete_children_of(stype, sid)
    return result


def _guard(result: ReconcileResult, stype: str, indexed: set[str], stale: set[str], ratio: float) -> None:
    if indexed and len(stale) / len(indexed) > ratio:
        result.skipped.append(
            f"{stype}: {len(stale)}/{len(indexed)} records look stale (> {ratio:.0%}); nothing deleted"
        )
        logger.error("reconcile: %s", result.skipped[-1])
    elif stale:
        result.stale_records[stype] = stale


def _stale_issues(client: RedmineClient, repo: ChunkRepository, result: ReconcileResult, ratio: float) -> None:
    indexed = repo.list_source_ids("issue")
    if not indexed:
        return
    present: set[str] = set()
    private: set[str] = set()
    try:
        for item in client.paginate("/issues.json", "issues", {"status_id": "*", "sort": "id:asc"}):
            present.add(str(item["id"]))
            if item.get("is_private"):
                private.add(str(item["id"]))
    except RedmineError as e:
        result.skipped.append(f"issue listing failed ({e}); no issue deleted")
        logger.error("reconcile: %s", result.skipped[-1])
        return
    # Issues of dropped projects are already handled by project deletion.
    _guard(result, "issue", indexed, indexed - (present - private), ratio)


def _stale_wiki(
    client: RedmineClient, repo: ChunkRepository, projects: list[dict[str, Any]], result: ReconcileResult,
    ratio: float,
) -> None:
    indexed = repo.list_source_ids("wiki")
    if not indexed:
        return
    present: set[str] = set()
    unsure: set[str] = set()  # projects whose wiki index could not be read: keep their pages
    for p in projects:
        try:
            pages = client.get(f"/projects/{p['identifier']}/wiki/index.json")["wiki_pages"]
        except RedmineNotFoundError:
            continue  # wiki module disabled: no pages
        except (RedmineForbiddenError, RedmineError) as e:
            unsure.add(str(p["id"]))
            logger.warning("reconcile: wiki index of project %s unreadable (%s); keeping its pages", p["id"], e)
            continue
        present.update(f"{p['id']}:{page['title']}" for page in pages)
    candidates = {sid for sid in indexed - present if sid.split(":", 1)[0] not in unsure}
    if unsure:
        result.skipped.append(f"wiki: {len(unsure)} project(s) unreadable; their pages were kept")
    _guard(result, "wiki", indexed, candidates, ratio)


def _stale_news(client: RedmineClient, repo: ChunkRepository, result: ReconcileResult, ratio: float) -> None:
    indexed = repo.list_source_ids("news")
    if not indexed:
        return
    try:
        present = {str(n["id"]) for n in client.paginate("/news.json", "news", {"sort": "id:asc"})}
    except RedmineError as e:
        result.skipped.append(f"news listing failed ({e}); no news deleted")
        logger.error("reconcile: %s", result.skipped[-1])
        return
    _guard(result, "news", indexed, indexed - present, ratio)


def _stale_projects(
    repo: ChunkRepository, projects: list[dict[str, Any]], result: ReconcileResult, ratio: float
) -> None:
    indexed = repo.list_source_ids("project")
    if indexed:
        _guard(result, "project", indexed, indexed - {str(p["id"]) for p in projects}, ratio)


def _stale_per_project(
    client: RedmineClient, repo: ChunkRepository, projects: list[dict[str, Any]], result: ReconcileResult,
    ratio: float, stype: str, key: str,
) -> None:
    """Versions / files: listed per project; judged per project, as wiki is.

    A project whose list cannot be read (403 = module disabled or no permission,
    404, 5xx, network) is left alone: its indexed records are kept. Only the
    records of projects that were read can become delete candidates.
    Whether to delete the index of a module-disabled project is left to a human
    (deletion is not implemented), so for now it is never deleted.
    """
    indexed = repo.list_source_projects(stype)  # source_id -> project_id
    if not indexed:
        return
    present: set[str] = set()
    readable: set[int] = set()
    unreadable: list[int] = []
    for p in projects:
        try:
            items = client.get(f"/projects/{p['identifier']}/{key}.json")[key]
        except RedmineError as e:  # 403 / 404 / 5xx / transport
            unreadable.append(p["id"])
            logger.warning("reconcile: %s of project %s unreadable (%s); keeping them", key, p["id"], type(e).__name__)
            continue
        readable.add(p["id"])
        present.update(str(i["id"]) for i in items)
    if unreadable:
        result.skipped.append(f"{stype}: {len(unreadable)} project(s) unreadable; their records were kept")
    # Records of projects outside ``projects`` (archived) go with the project deletion.
    candidates = {sid for sid, pid in indexed.items() if pid in readable and sid not in present}
    _guard(result, stype, set(indexed), candidates, ratio)


def _stale_time_entries(client: RedmineClient, repo: ChunkRepository, result: ReconcileResult, ratio: float) -> None:
    # Entries of private issues are never indexed, and go with their issue when it turns private.
    indexed = repo.list_source_ids("time_entry")
    if not indexed:
        return
    try:
        present = {str(e["id"]) for e in client.paginate("/time_entries.json", "time_entries", {})}
    except RedmineError as e:
        result.skipped.append(f"time entry listing failed ({e}); no time entry deleted")
        logger.error("reconcile: %s", result.skipped[-1])
        return
    _guard(result, "time_entry", indexed, indexed - present, ratio)
