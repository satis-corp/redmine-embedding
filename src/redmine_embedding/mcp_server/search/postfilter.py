"""Drop search hits the calling user may not see (postfilter).

The vector prefilter only knows project membership. Issue visibility range,
disabled modules, private records and role permissions are decided by Redmine,
so each record behind a hit is fetched with the *caller's* token: 403/404 means
"not visible". This is the last line of defence, so it fails closed: an unknown
source type, a failed request, a redirect or an unparsable answer all drop the
hit. The one exception is 401 (token invalid or expired): it propagates, so an
expired login is never mistaken for "no results".

Per source type the check is:
issue ``/issues/<id>``; wiki ``/projects/<pid>/wiki/<title>``; news, version,
project by their own id; file and attachment ``/attachments/<id>`` (follows the
container's permission); time_entry ``/time_entries/<id>`` AND its parent issue
(Redmine judges time entries independently of the issue); a time entry
without a parent issue is dropped.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

from ...common.config import Settings
from ...common.db.models import SearchHit
from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineAuthError, RedmineError
from .check_cache import CheckCache
from .prefilter import PrefilterSearcher

logger = logging.getLogger(__name__)

# 暫定: see Settings.search_* for the configured values.
DEFAULT_WORKERS = 8
DEFAULT_MAX_REQUESTS = 100


def check_paths(hit: SearchHit) -> list[str] | None:
    """Redmine JSON endpoints that must all succeed for ``hit`` to be visible.

    ``None`` means "cannot be checked" (unknown type, missing ids): drop the hit.
    Chunks of one record (e.g. ``/issues/7#note-2``) map to the same path, so the
    record is checked once.
    """
    st, sid = hit.source_type, hit.source_id
    if not sid or not hit.url_path:
        return None
    if st == "issue":
        return [f"/issues/{sid}.json"]
    if st == "news":
        return [f"/news/{sid}.json"]
    if st == "version":
        return [f"/versions/{sid}.json"]
    if st == "project":
        return [f"/projects/{sid}.json"]
    if st in ("attachment", "file"):
        return [f"/attachments/{sid}.json"]
    if st == "wiki":
        title = sid.split(":", 1)[1] if ":" in sid else ""
        if not title:
            return None
        return [f"/projects/{hit.project_id}/wiki/{quote(title, safe='')}.json"]
    if st == "time_entry":
        if hit.parent_type != "issue" or not hit.parent_id:
            return None  # no parent issue to check: fail closed
        return [f"/time_entries/{sid}.json", f"/issues/{hit.parent_id}.json"]
    return None


@dataclass
class FilterResult:
    """Outcome of a visibility filter.

    ``unverified_count``: hits dropped because the check itself failed (HTTP
    errors other than 403/404, redirects, bad JSON, API budget exceeded), not
    because they were found invisible. ``exhausted``: the API budget ran out.
    An empty ``kept`` with ``unverified_count > 0`` means "could not check", not
    "nothing visible".
    """

    kept: list[SearchHit] = field(default_factory=list)
    unverified_count: int = 0
    exhausted: bool = False
    # The search deadline passed: remaining hits were counted as unverified.
    timed_out: bool = False


class VisibilityChecker:
    """Checks paths with a per-search verdict table and an API call budget.

    The table lives only as long as one search (it lets re-fetch rounds reuse
    answers). An optional ``cache`` (with the caller's ``token``) shares settled
    verdicts across searches; cache hits do not count against the API budget.
    """

    def __init__(
        self,
        client: RedmineClient,
        *,
        workers: int = DEFAULT_WORKERS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        cache: CheckCache | None = None,
        token: str = "",
        deadline: float | None = None,
    ):
        # ``deadline``: absolute ``time.monotonic()`` value; past it no new request starts.
        self._deadline = deadline
        self.deadline_hit = False  # a check was skipped because the deadline passed
        self._cache = cache if cache is not None and cache.enabled and token else None
        self._token = token
        self._client = client
        self._workers = max(1, workers)
        self._budget = max_requests
        # True visible, False invisible (403/404), None unverified (error / over budget)
        self._verdict: dict[str, bool | None] = {}

    @property
    def exhausted(self) -> bool:
        return self._budget <= 0

    @property
    def expired(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _fetch(self, path: str) -> bool | None:
        if self.expired:
            self.deadline_hit = True
            return None  # unverified (not cached)
        try:
            self._client.get(path)
            return True
        except RedmineAuthError:
            raise
        except RedmineError as e:
            if e.status_code in (403, 404):
                return False
            logger.warning("visibility check %s failed (HTTP %s); dropping hit", path, e.status_code)
            return None
        except Exception as e:  # bad JSON, transport oddities: fail closed
            logger.warning("visibility check %s failed (%s); dropping hit", path, type(e).__name__)
            return None

    def prefetch(self, paths: Sequence[str]) -> None:
        """Check every not-yet-known path in parallel, within the remaining budget."""
        todo = []
        for p in dict.fromkeys(p for p in paths if p not in self._verdict):
            cached = self._cache.get(self._token, p) if self._cache is not None else None
            if cached is None:
                todo.append(p)
            else:
                self._verdict[p] = cached
        run, skipped = todo[: max(self._budget, 0)], todo[max(self._budget, 0):]
        self._budget -= len(run)
        for p in skipped:  # over budget: unverified, hence dropped
            self._verdict[p] = None
        if not run:
            return
        with ThreadPoolExecutor(max_workers=min(self._workers, len(run))) as pool:
            for p, ok in zip(run, pool.map(self._fetch, run)):  # re-raises RedmineAuthError
                self._verdict[p] = ok
                if self._cache is not None and ok is not None:  # never cache unverified
                    self._cache.put(self._token, p, ok)

    def judge(self, paths: Sequence[str]) -> bool | None:
        """True visible; False invisible (any path 403/404); None unverified."""
        verdicts = [self._verdict.get(p) for p in paths]
        if False in verdicts:
            return False
        return True if all(v is True for v in verdicts) else None

    def visible(self, paths: Sequence[str]) -> bool:
        return self.judge(paths) is True


def aggregate_sources(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """One hit per source (type, id): the highest-scoring chunk, best score first.

    Ties are broken by (source_type, source_id, chunk_no), so the chosen chunk and the
    order do not depend on the order the DB returned the rows. Visibility is decided per source (``check_paths`` is the
    same for every chunk of a record), so aggregating before the visibility check
    loses nothing and spends no API calls on duplicate chunks.
    """
    best: dict[tuple[str, str], SearchHit] = {}
    for h in sorted(hits, key=lambda h: (-h.score, h.source_type, h.source_id, h.chunk_no)):
        best.setdefault((h.source_type, h.source_id), h)
    return list(best.values())


def _filter(hits: Sequence[SearchHit], checker: VisibilityChecker, limit: int | None) -> FilterResult:
    result = FilterResult()
    if limit is not None and limit <= 0:
        return result
    ordered = aggregate_sources(hits)  # limit counts sources, not chunks
    window = max((limit or len(ordered)) * 2, checker._workers, 1)
    for i in range(0, len(ordered), window):
        if checker.expired:  # out of time: the rest could not be checked
            result.unverified_count += len(ordered) - i
            result.timed_out = True
            break
        chunk = ordered[i : i + window]
        plans = [(h, check_paths(h)) for h in chunk]
        checker.prefetch([p for _, paths in plans if paths for p in paths])
        for h, paths in plans:
            verdict = checker.judge(paths) if paths else False
            if verdict:
                result.kept.append(h)
                if limit is not None and len(result.kept) >= limit:
                    return result  # complete: limit reached, so budget use is not a shortfall (exhausted stays False)
            elif verdict is None:
                result.unverified_count += 1
    result.exhausted = checker.exhausted
    if checker.deadline_hit:  # deadline passed in the middle of a window
        result.timed_out = True
    return result


def filter_visible(
    hits: Sequence[SearchHit],
    client: RedmineClient,
    *,
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> FilterResult:
    """Keep sources visible to ``client``'s user (best chunk each), best score first, at most ``limit``."""
    return _filter(hits, VisibilityChecker(client, workers=workers, max_requests=max_requests), limit)


class VisibleSearcher:
    """Prefilter search + postfilter, re-fetching with a larger top_k when short."""

    def __init__(
        self,
        searcher: PrefilterSearcher,
        client_factory: Callable[[str], RedmineClient],
        *,
        limit: int = 10,
        max_rounds: int = 3,
        workers: int = DEFAULT_WORKERS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        check_cache: CheckCache | None = None,
    ) -> None:
        self._check_cache = check_cache
        self._searcher = searcher
        self._client_factory = client_factory
        self._limit = limit
        self._max_rounds = max_rounds
        self._workers = workers
        self._max_requests = max_requests

    @classmethod
    def from_settings(
        cls, settings: Settings, searcher: PrefilterSearcher, client_factory: Callable[[str], RedmineClient]
    ) -> VisibleSearcher:
        return cls(
            searcher,
            client_factory,
            limit=settings.search_default_limit,
            max_rounds=settings.search_max_rounds,
            workers=settings.search_check_workers,
            max_requests=settings.search_check_max_requests,
            check_cache=CheckCache(
                ttl=settings.search_check_cache_ttl, max_entries=settings.search_check_cache_max_entries
            ),
        )

    def search(
        self,
        query: str,
        *,
        token: str,
        limit: int | None = None,
        project_ids: Sequence[int] | None = None,
        source_types: Sequence[str] | None = None,
        deadline: float | None = None,
    ) -> FilterResult:
        """Visible hits (<= limit) plus unverified count. Raises ``RedmineAuthError`` when the token is rejected.

        ``deadline`` (absolute ``time.monotonic()``): no new round or check starts after it;
        what is left is reported as unverified with ``timed_out`` set.
        """
        limit = self._limit if limit is None else limit
        top_k = self._searcher.top_k
        with self._client_factory(token) as client:
            checker = VisibilityChecker(
                client, workers=self._workers, max_requests=self._max_requests, cache=self._check_cache, token=token,
                deadline=deadline,
            )
            result = FilterResult()
            for n in range(max(self._max_rounds, 1)):
                if n and checker.expired:
                    result.timed_out = True
                    break
                hits = self._searcher.search_candidates(
                    query, token=token, project_ids=project_ids, source_types=source_types, top_k=top_k
                )
                new = _filter(hits, checker, limit)
                if n and new.timed_out and len(new.kept) < len(result.kept):
                    # Deadline hit during this round: keep what an earlier round already verified.
                    result.timed_out = True
                    break
                result = new
                # done: enough hits, candidates ran out, or no API budget left
                if len(result.kept) >= limit or len(hits) < top_k or checker.exhausted or result.timed_out:
                    break
                top_k *= 2
            return result
