from datetime import datetime, timezone

import httpx

from redmine_embedding.batch.sources import CONNECTORS, build_connectors
from redmine_embedding.batch.sources.news import NewsConnector
from redmine_embedding.common.redmine.client import RedmineClient

COMMENTS = {5: [{"id": 11, "author": {"id": 2}, "content": "いいですね"}, {"id": 12, "content": "  "}], 6: []}


def make(project_ids=None, missing=()):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/news.json":
            items = [{"id": 5, "project": {"id": 3}}, {"id": 6, "project": {"id": 4}}, {"id": 8, "project": {"id": 3}}]
            return httpx.Response(200, json={"news": items, "total_count": 3})
        nid = int(req.url.path.split("/")[2].split(".")[0])
        assert req.url.params["include"] == "comments"
        if nid in missing or nid == 8:
            return httpx.Response(404)
        return httpx.Response(200, json={"news": {
            "id": nid, "project": {"id": 3 if nid == 5 else 4}, "title": f"お知らせ{nid}", "summary": "概要",
            "description": "本文", "author": {"id": 1}, "created_on": "2026-10-01T00:00:00Z",
            "comments": COMMENTS[nid]}})

    c = RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler), max_retries=0)
    return NewsConnector(c, project_ids), seen


def test_news_document_has_one_section_per_comment_with_anchor():
    conn, _ = make()
    d = next(iter(conn.iter_changed(None)))
    assert (d.source_type, d.source_id, d.title, d.url_path, d.project_id, d.author_id) == (
        "news", "5", "お知らせ5", "/news/5", 3, 1)
    assert [s.text for s in d.sections] == ["お知らせ5\n\n概要\n\n本文", "いいですね"]  # blank comment dropped
    assert d.sections[1].url_fragment == "#comment-11"


def test_since_is_ignored_so_everything_is_read_each_time():
    conn, _ = make()
    docs = list(conn.iter_changed(datetime(2030, 1, 1, tzinfo=timezone.utc)))
    assert [d.source_id for d in docs] == ["5", "6"]  # 8 vanished (404) and is skipped


def test_projects_outside_the_allowed_list_are_not_fetched():
    conn, seen = make(project_ids={3})
    assert [d.source_id for d in conn.iter_changed(None)] == ["5"]
    assert all("/news/6" not in r.url.path for r in seen)


def test_news_is_registered_and_buildable():
    assert "news" in CONNECTORS
    assert isinstance(build_connectors(None, ["news"], project_ids={1})[0], NewsConnector)
