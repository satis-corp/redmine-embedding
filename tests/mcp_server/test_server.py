import logging

import httpx
import psycopg
import pytest

pytest.importorskip("mcp")

from mcp.client import Client  # noqa: E402

from redmine_embedding.common.config import Settings  # noqa: E402
from redmine_embedding.common.db.models import SearchHit  # noqa: E402
from redmine_embedding.common.redmine.errors import RedmineAuthError, RedmineError, RedmineForbiddenError  # noqa: E402
from redmine_embedding.mcp_server.auth.token import bearer_header_token, make_token_source  # noqa: E402
from redmine_embedding.mcp_server.runtime import AppRuntime  # noqa: E402
from redmine_embedding.mcp_server.search.postfilter import FilterResult  # noqa: E402
from redmine_embedding.mcp_server.server import create_server  # noqa: E402

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


SECRET = "super-secret-token-123"


def hit(i=1, content="a  b\n c" , **kw):
    d = dict(source_type="issue", source_id=str(i), chunk_no=0, project_id=5, title=f"t{i}",
             content=content, url_path=f"/issues/{i}", score=0.9)
    d.update(kw)
    return SearchHit(**d)


class FakeSearcher:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result or FilterResult(), exc, []

    def search(self, query, **kw):
        self.calls.append((query, kw))
        if self.exc:
            raise self.exc
        return self.result


def settings(**kw):
    return Settings(_env_file=None, redmine_url="https://r.example", **kw)


async def call(searcher, args, token=SECRET, **skw):
    st = settings(**skw)
    srv = create_server(
        st,
        token_source=lambda ctx: token,
        runtime_factory=lambda s, src: AppRuntime(s, searcher, src),
    )
    async with Client(srv) as c:
        return await c.call_tool("search_redmine", args)


def text(r):
    return "".join(getattr(b, "text", "") for b in r.content)


async def test_schema_and_structured_output():
    st = settings()
    srv = create_server(st, runtime_factory=lambda s, src: AppRuntime(s, FakeSearcher(), src))
    async with Client(srv) as c:
        tools = (await c.list_tools()).tools
        assert [t.name for t in tools] == ["search_redmine"]
        t = tools[0]
        props = t.input_schema["properties"]
        assert t.input_schema["required"] == ["query"]
        assert set(props) == {"query", "types", "projects", "limit"}
        assert props["limit"]["anyOf"][0]["maximum"] == 50
        assert "Redmine MCP" in t.description
        assert t.output_schema is not None


async def test_success_result_shape_and_args_passed():
    fs = FakeSearcher(FilterResult(kept=[hit(1, "x" * 1000)]))
    r = await call(fs, {"query": "q", "types": ["issue"], "projects": [5], "limit": 3})
    assert not r.is_error
    sc = r.structured_content
    assert sc["count"] == 1 and sc["incomplete"] is False and sc["notice"] is None
    item = sc["results"][0]
    assert item["url"] == "https://r.example/issues/1" and item["id"] == "1"
    assert len(item["excerpt"]) == 300 and item["excerpt"].endswith("…")
    _, kw = fs.calls[0]
    assert kw["token"] == SECRET and kw["limit"] == 3
    assert kw["project_ids"] == [5] and kw["source_types"] == ["issue"]
    assert kw["deadline"] is not None


async def test_default_limit_and_excerpt_whitespace():
    fs = FakeSearcher(FilterResult(kept=[hit(1)]))
    r = await call(fs, {"query": "q"})
    assert fs.calls[0][1]["limit"] == 10
    assert r.structured_content["results"][0]["excerpt"] == "a b c"


async def test_zero_hits_is_success():
    r = await call(FakeSearcher(), {"query": "q"})
    assert not r.is_error and r.structured_content["results"] == []


@pytest.mark.parametrize(
    "args",
    [{"query": ""}, {"query": "q", "limit": 0}, {"query": "q", "limit": 51}, {"query": "q", "types": ["nope"]}],
)
async def test_invalid_input_is_tool_error_and_search_not_called(args):
    fs = FakeSearcher()
    r = await call(fs, args)
    assert r.is_error
    assert fs.calls == []


async def test_blank_query_rejected():
    fs = FakeSearcher()
    r = await call(fs, {"query": "   "})
    assert r.is_error and "空" in text(r) and fs.calls == []


async def test_missing_token_is_error():
    fs = FakeSearcher()
    r = await call(fs, {"query": "q"}, token=None)
    assert r.is_error and "認証" in text(r) and fs.calls == []


@pytest.mark.parametrize(
    "exc,word",
    [
        (RedmineAuthError("401 " + SECRET, status_code=401), "認証が切れ"),
        (RedmineForbiddenError("403", status_code=403), "拒否"),
        (RedmineError("boom", status_code=502), "Redmine に接続できません"),
        (RedmineError("request failed", status_code=None), "Redmine に接続できません"),
        (httpx.ConnectError("x"), "Redmine に接続できません"),
        (psycopg.OperationalError("db " + SECRET), "データベース"),
        (RuntimeError("secret " + SECRET), "検索処理に失敗"),
    ],
)
async def test_error_conversion_hides_details(exc, word, caplog):
    caplog.set_level(logging.DEBUG)
    r = await call(FakeSearcher(exc=exc), {"query": "q"})
    assert r.is_error and word in text(r)
    assert SECRET not in text(r)


async def test_all_unverified_is_error_not_empty():
    r = await call(FakeSearcher(FilterResult(unverified_count=7)), {"query": "q"})
    assert r.is_error and "7" in text(r) and "0 件ではありません" in text(r)


async def test_timeout_without_hits_is_error():
    r = await call(FakeSearcher(FilterResult(unverified_count=0, timed_out=True)), {"query": "q"})
    assert r.is_error and "時間内" in text(r)


async def test_partial_failure_is_incomplete_success():
    r = await call(FakeSearcher(FilterResult(kept=[hit(1)], unverified_count=2)), {"query": "q"})
    sc = r.structured_content
    assert not r.is_error and sc["incomplete"] is True and sc["unverified_count"] == 2 and sc["notice"]


async def test_timeout_with_hits_is_incomplete():
    r = await call(FakeSearcher(FilterResult(kept=[hit(1)], timed_out=True)), {"query": "q"})
    assert not r.is_error and r.structured_content["incomplete"] is True


async def test_token_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    await call(FakeSearcher(FilterResult(kept=[hit(1)])), {"query": "秘密のクエリ"})
    await call(FakeSearcher(exc=RedmineAuthError("t " + SECRET, status_code=401)), {"query": "q"})
    await call(FakeSearcher(exc=RuntimeError(SECRET)), {"query": "q"})
    assert SECRET not in caplog.text
    assert "秘密のクエリ" not in caplog.text


async def test_lifespan_closes_runtime():
    closed = []
    st = settings()
    srv = create_server(st, runtime_factory=lambda s, src: AppRuntime(s, FakeSearcher(), src, close=lambda: closed.append(1)))
    async with Client(srv) as c:
        await c.list_tools()
    assert closed == [1]


# --- token sources ---------------------------------------------------------
class Req:
    def __init__(self, headers):
        self.headers = headers


class RC:
    def __init__(self, request):
        self.request = request


class Ctx:
    def __init__(self, request):
        self.request_context = RC(request)


def test_bearer_header():
    assert bearer_header_token(Ctx(Req({"authorization": "Bearer abc"}))) == "abc"
    assert bearer_header_token(Ctx(Req({"authorization": "bearer abc"}))) == "abc"
    assert bearer_header_token(Ctx(Req({"authorization": "Basic abc"}))) is None
    assert bearer_header_token(Ctx(Req({"authorization": "Bearer "}))) is None
    assert bearer_header_token(Ctx(Req({}))) is None
    assert bearer_header_token(Ctx(None)) is None


def test_dev_token_only_when_allowed():
    env = {"REDMINE_MCP_DEV_TOKEN": "dev"}
    assert make_token_source(environ=env)(Ctx(None)) is None
    assert make_token_source(allow_dev_token=True, environ=env)(Ctx(None)) == "dev"
    # a header wins over the dev token
    assert make_token_source(allow_dev_token=True, environ=env)(Ctx(Req({"authorization": "Bearer h"}))) == "h"


async def test_pool_timeout_is_db_error():
    from psycopg_pool import PoolTimeout

    r = await call(FakeSearcher(exc=PoolTimeout("pool " + SECRET)), {"query": "q"})
    assert r.is_error and "データベース" in text(r) and SECRET not in text(r)


async def test_formatting_error_goes_through_tool_error():
    bad = FilterResult(kept=[hit(1, score="x")])  # SearchResult validation fails while formatting
    r = await call(FakeSearcher(result=bad), {"query": "q"})
    assert r.is_error and "検索処理に失敗" in text(r)


async def test_empty_lists_mean_unspecified():
    fs = FakeSearcher()
    r = await call(fs, {"query": "q", "projects": [], "types": []})
    assert not r.is_error
    assert fs.calls[0][1]["project_ids"] is None and fs.calls[0][1]["source_types"] is None


async def test_too_many_projects_rejected():
    fs = FakeSearcher()
    r = await call(fs, {"query": "q", "projects": list(range(1, 52))})
    assert r.is_error and fs.calls == []


def test_default_limit_over_max_rejected():
    with pytest.raises(Exception):
        settings(search_default_limit=60, search_max_limit=50)


@pytest.mark.parametrize("host,ok", [("0.0.0.0", False), ("192.168.1.5", False), ("127.0.0.1", True), ("localhost", True), ("::1", True)])
def test_dev_token_requires_loopback(monkeypatch, host, ok):
    from redmine_embedding.mcp_server import __main__ as m
    from redmine_embedding.common import config

    started = []
    monkeypatch.setattr(config, "configure_from_args", lambda a: None)
    monkeypatch.setattr(m, "configure_from_args", lambda a: None)
    monkeypatch.setattr(m, "get_settings", lambda: settings(database_url="postgresql://u:p@h/d"))
    import redmine_embedding.mcp_server.server as srvmod

    class S:
        def run(self, *a, **kw):
            started.append(kw)

    monkeypatch.setattr(srvmod, "create_server", lambda *a, **kw: S())
    if ok:
        m.main(["--allow-dev-token", "--host", host])
        assert started
    else:
        with pytest.raises(SystemExit) as e:
            m.main(["--allow-dev-token", "--host", host])
        assert e.value.code == 2 and not started


def test_non_loopback_without_dev_token_is_allowed(monkeypatch):
    from redmine_embedding.mcp_server import __main__ as m
    import redmine_embedding.mcp_server.server as srvmod

    started = []

    class S:
        def run(self, *a, **kw):
            started.append(kw)

    monkeypatch.setattr(m, "configure_from_args", lambda a: None)
    monkeypatch.setattr(m, "get_settings", lambda: settings(database_url="postgresql://u:p@h/d"))
    monkeypatch.setattr(srvmod, "create_server", lambda *a, **kw: S())
    m.main(["--host", "0.0.0.0"])
    assert started


async def test_excerpt_mask_setting():
    h = hit(1, content="ID: a PW: Zq9xYw3p")
    r = await call(FakeSearcher(FilterResult(kept=[h])), {"query": "q"})
    assert r.structured_content["results"][0]["excerpt"] == "ID: a PW: ****"
    r = await call(FakeSearcher(FilterResult(kept=[h])), {"query": "q"}, search_excerpt_mask_credentials=False)
    assert r.structured_content["results"][0]["excerpt"] == "ID: a PW: Zq9xYw3p"
