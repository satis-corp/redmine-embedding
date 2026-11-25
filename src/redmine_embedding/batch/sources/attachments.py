"""Attachment connector: file name / description, plus the text of text files.

Attachments have no ``updated_on`` and no API of their own, so they are read
through their parent: issues (re-read when the issue changed), wiki pages and
news (both re-read every run; PROVISIONAL). Per parent we also yield a
``ChildrenReset`` so attachments removed from the parent leave the index.

Private issues: nothing of their attachments is yielded and their old
attachment chunks are reset. A listing without ``is_private`` fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ...common.config import Settings
from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineAuthError, RedmineError, RedmineForbiddenError, RedmineNotFoundError
from ..extractors import document_extractor
from .base import ChildrenReset, Deletion, Section, SourceDocument, parse_redmine_datetime
from .issues import PrivacyUnknownError, _fmt_since
from .textfile import contains_private_key, decode_text, is_excluded_name, is_text_candidate

logger = logging.getLogger(__name__)

#: bytes -> text (None = cannot extract). Replace to support PDF / Office later.
TextExtractor = Callable[[bytes], "str | None"]

_DEFAULT_MAX_BYTES: int = Settings.model_fields["attachment_max_bytes"].default
_DEFAULT_MAX_CHARS: int = Settings.model_fields["attachment_text_max_chars"].default
_SKIPPED_MIME_PREFIXES = ("image/", "video/", "audio/")


class AttachmentConnector:
    source_type = "attachment"

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

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument | ChildrenReset | Deletion]:
        yield from self._issue_attachments(since)
        yield from self._wiki_attachments()
        yield from self._news_attachments()

    # -- issues --------------------------------------------------------------
    def _issue_attachments(self, since: datetime | None) -> Iterator[SourceDocument | ChildrenReset]:
        params: dict[str, Any] = {"status_id": "*", "sort": "id:asc", "include": "attachments"}
        if since is not None:
            params["updated_on"] = ">=" + _fmt_since(since)
        for item in self._client.paginate("/issues.json", "issues", params):
            project_id = (item.get("project") or {}).get("id")
            if self._project_ids is not None and project_id not in self._project_ids:
                continue
            if "is_private" not in item:
                raise PrivacyUnknownError(
                    f"issue #{item.get('id')}: the issue listing has no 'is_private' field; "
                    "refusing to index its attachments"
                )
            issue_id = str(item["id"])
            if item["is_private"]:
                yield ChildrenReset(self.source_type, "issue", issue_id)  # nothing of it may stay
                continue
            if "attachments" not in item:
                # The server ignored include=attachments. Absent is not "none": do not reset.
                logger.warning("issue #%s: listing has no 'attachments' key; left as is", issue_id)
                continue
            attachments = item["attachments"] or []
            hidden = self._attachments_of_private_notes(item["id"]) if attachments else set()
            if hidden is None:
                # Private (or vanished) in the detail although the listing said public: drop what we hold.
                yield ChildrenReset(self.source_type, "issue", issue_id)
                continue
            docs = [
                self._build(a, project_id, "issue", issue_id, item.get("updated_on"))
                for a in attachments
                if str(a["id"]) not in hidden
            ]
            yield from docs
            yield ChildrenReset(self.source_type, "issue", issue_id, frozenset(d.source_id for d in docs))

    def _attachments_of_private_notes(self, issue_id: int) -> set[str] | None:
        """Ids of attachments added by private notes (PROVISIONAL: journal detail format unverified)."""
        try:
            issue = self._client.get(f"/issues/{issue_id}.json", {"include": "journals"})["issue"]
        except RedmineNotFoundError:
            return None
        if issue.get("is_private", True):  # fail closed: missing flag counts as private
            return None
        hidden: set[str] = set()
        for journal in issue.get("journals") or []:
            if journal.get("private_notes"):
                hidden.update(
                    str(d.get("name")) for d in journal.get("details") or [] if d.get("property") == "attachment"
                )
        return hidden

    # -- wiki ----------------------------------------------------------------
    def _wiki_attachments(self) -> Iterator[SourceDocument | ChildrenReset]:
        # PROVISIONAL: every page is read every run, because it is not known
        # whether adding a file moves the page's updated_on.
        for project in self._client.list_projects(statuses=self._statuses):
            if self._project_ids is not None and project["id"] not in self._project_ids:
                continue
            ident = project["identifier"]
            try:
                pages = self._client.get(f"/projects/{ident}/wiki/index.json")["wiki_pages"]
            except (RedmineNotFoundError, RedmineForbiddenError):
                continue
            for entry in pages:
                title = entry["title"]
                try:
                    page = self._client.get(
                        f"/projects/{ident}/wiki/{quote(title, safe='')}.json", {"include": "attachments"}
                    )["wiki_page"]
                except RedmineNotFoundError:
                    continue
                parent_id = f"{project['id']}:{title}"  # same as the wiki source_id
                if "attachments" not in page:
                    logger.warning("wiki %s: no 'attachments' key; left as is", parent_id)
                    continue
                docs = [
                    self._build(a, project["id"], "wiki", parent_id, page.get("updated_on"))
                    for a in page["attachments"] or []
                ]
                yield from docs
                yield ChildrenReset(self.source_type, "wiki", parent_id, frozenset(d.source_id for d in docs))

    # -- news ----------------------------------------------------------------
    def _news_attachments(self) -> Iterator[SourceDocument | ChildrenReset]:
        # News has no updated_on: every item is re-read every run (as the news connector does).
        for item in self._client.paginate("/news.json", "news", {"sort": "id:asc"}):
            project_id = (item.get("project") or {}).get("id")
            if self._project_ids is not None and project_id not in self._project_ids:
                continue
            try:
                news = self._client.get(f"/news/{item['id']}.json", {"include": "attachments"})["news"]
            except RedmineNotFoundError:
                continue
            news_id = str(item["id"])
            if "attachments" not in news:
                logger.warning("news %s: no 'attachments' key; left as is", news_id)
                continue
            docs = [
                self._build(a, project_id, "news", news_id, news.get("created_on"))
                for a in news["attachments"] or []
            ]
            yield from docs
            yield ChildrenReset(self.source_type, "news", news_id, frozenset(d.source_id for d in docs))

    # -- one attachment -------------------------------------------------------
    def _build(
        self, att: dict[str, Any], project_id: int, parent_type: str, parent_id: str, parent_updated: str | None
    ) -> SourceDocument:
        filename = att.get("filename") or ""
        head = f"{filename}\n{att['description'].strip()}" if (att.get("description") or "").strip() else filename
        sections = [Section(head)]
        body = self._read_text(att)
        if body:
            sections.append(Section(body))
        created = att.get("created_on") or parent_updated
        return SourceDocument(
            source_type=self.source_type,
            source_id=str(att["id"]),
            project_id=project_id,
            title=filename,
            url_path=f"/attachments/{att['id']}",
            updated_on=parse_redmine_datetime(created) if created else datetime(1970, 1, 1, tzinfo=timezone.utc),
            sections=sections,
            author_id=(att.get("author") or {}).get("id"),
            parent_type=parent_type,
            parent_id=parent_id,
        )

    def _read_text(self, att: dict[str, Any]) -> str | None:
        return read_attachment_text(self._client, att, self._max_bytes, self._max_chars, self._extract)


def read_attachment_text(
    client: RedmineClient, att: dict[str, Any], max_bytes: int, max_chars: int, extract: TextExtractor
) -> str | None:
    """Text of a small text / PDF / xlsx file, or None (metadata only). Never logs URLs or keys.

    Shared by attachments and the project files tab. PDF and xlsx are chosen by
    extension only and go through ``batch.extractors`` (never decoded as text);
    ``extract`` is the text-file decoder.
    """
    filename, ctype = att.get("filename") or "", att.get("content_type")
    doc_extract = document_extractor(filename)
    if (ctype or "").lower().startswith(_SKIPPED_MIME_PREFIXES) or not (doc_extract or is_text_candidate(filename, ctype)):
        return None
    if is_excluded_name(filename):
        return None
    size = att.get("filesize")
    if isinstance(size, int) and size > max_bytes:
        return None  # decided before downloading
    if not att.get("content_url"):
        return None
    try:
        data = client.download(att["content_url"], max_bytes=max_bytes)
    except RedmineAuthError:
        raise  # the key is wrong: every download would fail, so fail the run
    except RedmineError as e:
        # Too large, 404, 403, 5xx, timeout, foreign host: this one attachment becomes metadata
        # only; the run goes on (a stuck file must not stop the whole source). The type name
        # is logged, never the URL or the key.
        logger.warning("attachment %s: body skipped (%s)", att.get("id"), type(e).__name__)
        return None
    text = doc_extract(data, max_chars) if doc_extract else extract(data)
    if doc_extract and not text:
        logger.warning("attachment %s: document body empty or skipped", att.get("id"))  # id only
    if text and contains_private_key(text):
        logger.warning("attachment %s: body skipped (private key header)", att.get("id"))
        return None
    return text[:max_chars].strip() if text else None
