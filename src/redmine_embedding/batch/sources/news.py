"""News connector: title / summary / description, plus one section per comment.

News has no ``updated_on`` and comments carry no date, so every run reads every
news item and yields it all (upserts are idempotent; ``since`` is ignored).
``/news.json`` spans all projects; ``project_ids`` filters afterwards.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime
from typing import Any

from ...common.redmine.client import RedmineClient
from ...common.redmine.errors import RedmineNotFoundError
from .base import Section, SourceDocument, parse_redmine_datetime


class NewsConnector:
    source_type = "news"

    def __init__(self, client: RedmineClient, project_ids: Collection[int] | None = None) -> None:
        self._client = client
        self._project_ids = None if project_ids is None else frozenset(project_ids)

    def iter_changed(self, since: datetime | None) -> Iterator[SourceDocument]:
        for item in self._client.paginate("/news.json", "news", {"sort": "id:asc"}):
            if not _in_scope(item, self._project_ids):
                continue
            try:
                news = self._client.get(f"/news/{item['id']}.json", {"include": "comments"})["news"]
            except RedmineNotFoundError:
                continue  # deleted since the listing
            yield build_news_document(news)


def _in_scope(item: dict[str, Any], project_ids: frozenset[int] | None) -> bool:
    return project_ids is None or (item.get("project") or {}).get("id") in project_ids


def build_news_document(news: dict[str, Any]) -> SourceDocument:
    parts = [(news.get("title") or "").strip(), (news.get("summary") or "").strip(),
             (news.get("description") or "").strip()]
    sections = [Section("\n\n".join(p for p in parts if p))]
    for comment in news.get("comments") or []:
        text = (comment.get("content") or "").strip()
        if text:
            sections.append(Section(text, url_fragment=f"#comment-{comment['id']}"))
    return SourceDocument(
        source_type="news",
        source_id=str(news["id"]),
        project_id=news["project"]["id"],
        title=(news.get("title") or "").strip(),
        url_path=f"/news/{news['id']}",
        updated_on=parse_redmine_datetime(news["created_on"]),
        sections=sections,
        author_id=(news.get("author") or {}).get("id"),
    )
