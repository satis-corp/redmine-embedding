"""Thin synchronous Redmine REST API client.

Authentication is either an API key (sync batch) or an OAuth2 access token
(MCP server, passed through from the caller and never stored here).
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import httpx

from .errors import (
    AttachmentTooLargeError,
    ForeignHostError,
    RedmineAuthError,
    RedmineError,
    RedmineForbiddenError,
    RedmineNotFoundError,
    RedmineRedirectError,
)

PAGE_SIZE = 100  # Redmine's maximum
_RETRY_STATUS = {429, 502, 503, 504}


class RedmineClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        request_interval: float = 0.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if api_key and access_token:
            raise ValueError("pass either api_key or access_token, not both")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-Redmine-API-Key"] = api_key
        elif access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )
        self._interval = request_interval
        self._max_retries = max_retries
        self._last_request = 0.0

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RedmineClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path`` (e.g. ``/issues/1.json``) and return the decoded JSON."""
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise RedmineError(f"request failed: {e}") from e
                time.sleep(2**attempt)
                continue
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else 2**attempt)
                continue
            return self._decode(resp)
        raise AssertionError("unreachable")

    def paginate(
        self,
        path: str,
        key: str,
        params: Mapping[str, Any] | None = None,
        *,
        page_size: int = PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a paged collection (``key`` e.g. ``"issues"``)."""
        offset = 0
        while True:
            page = self.get(path, {**(params or {}), "limit": page_size, "offset": offset})
            items = page.get(key, [])
            yield from items
            offset += len(items)
            total = page.get("total_count")
            if not items or (total is not None and offset >= total):
                return

    def download(self, content_url: str, *, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        """Download an attachment (``content_url`` of the attachment JSON).

        The API key is only ever sent to the configured Redmine host: a
        ``content_url`` on another host raises ``ForeignHostError`` before any
        request. Files over ``max_bytes`` raise ``AttachmentTooLargeError``,
        decided from Content-Length when present, otherwise while streaming.
        """
        target, base = urlsplit(content_url), urlsplit(self.base_url)
        if target.netloc and (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise ForeignHostError(f"refusing to send credentials to {target.netloc or content_url!r}")
        try:
            with self._http.stream("GET", content_url) as resp:
                if resp.status_code >= 400:
                    self._decode(resp)
                declared = resp.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise AttachmentTooLargeError(f"{declared} bytes > limit {max_bytes}")
                buf = bytearray()
                for part in resp.iter_bytes():
                    buf += part
                    if len(buf) > max_bytes:
                        raise AttachmentTooLargeError(f"over the {max_bytes} byte limit")
                return bytes(buf)
        except httpx.TransportError as e:
            raise RedmineError(f"download failed: {e}") from e

    # -- helpers shared by batch and MCP ----------------------------------
    def list_projects(self, *, statuses: Sequence[int] | None = None) -> list[dict[str, Any]]:
        """Projects visible to the caller.

        Redmine returns active projects only by default. ``statuses`` takes
        Redmine project status codes (1 active, 5 closed, 9 archived) and is
        sent as a ProjectQuery filter (Redmine 5.1+). Whether archived projects
        are returned at all still has to be verified on the target instance.
        """
        params: dict[str, Any] = {}
        if statuses:
            params.update(
                {"set_filter": "1", "f[]": "status", "op[status]": "=", "v[status][]": [str(s) for s in statuses]}
            )
        return list(self.paginate("/projects.json", "projects", params))

    def visible_project_ids(self, *, statuses: Sequence[int] | None = (1, 5)) -> list[int]:
        """IDs of the projects the caller can see (prefilter for vector search)."""
        return [p["id"] for p in self.list_projects(statuses=statuses)]

    # -- internals ---------------------------------------------------------
    def _throttle(self) -> None:
        if self._interval > 0:
            wait = self._interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        code = resp.status_code
        if code == 401:
            raise RedmineAuthError("unauthorized: token invalid or expired", status_code=code)
        if code == 403:
            raise RedmineForbiddenError("forbidden", status_code=code)
        if code == 404:
            raise RedmineNotFoundError("not found (or not visible)", status_code=code)
        if 300 <= code < 400:  # not followed; never parse the redirect body as JSON
            raise RedmineRedirectError(f"Redmine redirected (HTTP {code})", status_code=code)
        if code >= 400:
            raise RedmineError(f"Redmine returned HTTP {code}", status_code=code)
        if not resp.content:
            return {}
        return resp.json()
