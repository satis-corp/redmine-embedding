import httpx
import pytest

from redmine_embedding.common.redmine.client import RedmineClient
from redmine_embedding.common.redmine.errors import (
    RedmineAuthError,
    RedmineForbiddenError,
    RedmineNotFoundError,
)


def make(handler, **kw):
    return RedmineClient("https://redmine.example.com/", transport=httpx.MockTransport(handler), **kw)


def test_api_key_header():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={})

    make(handler, api_key="k").get("/x.json")
    assert seen["x-redmine-api-key"] == "k"
    assert "authorization" not in seen


def test_bearer_token_header():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={})

    make(handler, access_token="t").get("/x.json")
    assert seen["authorization"] == "Bearer t"


def test_key_and_token_are_exclusive():
    with pytest.raises(ValueError):
        RedmineClient("https://r", api_key="a", access_token="b")


def test_paginate_walks_all_pages():
    data = [{"id": i} for i in range(250)]

    def handler(req):
        off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
        return httpx.Response(200, json={"issues": data[off : off + lim], "total_count": 250})

    assert [i["id"] for i in make(handler).paginate("/issues.json", "issues")] == list(range(250))


def test_paginate_stops_on_empty_page_without_total():
    pages = {0: [{"id": 1}], 1: []}

    def handler(req):
        return httpx.Response(200, json={"issues": pages[int(req.url.params["offset"])]})

    assert [i["id"] for i in make(handler).paginate("/issues.json", "issues")] == [1]


@pytest.mark.parametrize(
    "status, exc",
    [(401, RedmineAuthError), (403, RedmineForbiddenError), (404, RedmineNotFoundError)],
)
def test_error_mapping(status, exc):
    with pytest.raises(exc):
        make(lambda req: httpx.Response(status)).get("/x.json")


def test_retry_on_503_then_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert make(handler).get("/x.json") == {"ok": True}
    assert len(calls) == 3


def test_visible_project_ids_sends_status_filter():
    seen = {}

    def handler(req):
        seen.update(req.url.params.multi_items())
        return httpx.Response(200, json={"projects": [{"id": 1}, {"id": 2}], "total_count": 2})

    assert make(handler).visible_project_ids(statuses=(1, 5)) == [1, 2]
    assert seen["set_filter"] == "1" and seen["f[]"] == "status"


def test_download_never_sends_the_api_key_to_another_host():
    import pytest

    from redmine_embedding.common.redmine.errors import ForeignHostError

    calls = []
    c = RedmineClient("https://r.example.com", api_key="k",
                      transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=b"x")))
    with pytest.raises(ForeignHostError):
        c.download("https://evil.example.net/attachments/download/1/a.pdf")
    assert calls == []
    assert c.download("https://r.example.com/attachments/download/1/a.pdf") == b"x"


def test_download_skips_oversized_files_by_header_and_while_streaming():
    import pytest

    from redmine_embedding.common.redmine.errors import AttachmentTooLargeError

    big = 14_500_000
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("declared.pdf"):
            return httpx.Response(200, headers={"Content-Length": str(big)}, content=b"x")
        return httpx.Response(200, content=b"y" * 2048)  # no usable header

    c = RedmineClient("https://r.example.com", transport=httpx.MockTransport(handler))
    with pytest.raises(AttachmentTooLargeError):
        c.download("/attachments/download/1/declared.pdf", max_bytes=10 * 1024 * 1024)
    with pytest.raises(AttachmentTooLargeError):
        c.download("/attachments/download/2/chunked.pdf", max_bytes=1024)
    assert len(c.download("/attachments/download/2/chunked.pdf", max_bytes=4096)) == 2048
