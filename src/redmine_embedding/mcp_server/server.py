"""MCP server: lifespan + tool registration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from ..common.config import Settings
from .auth.setup import OAuthComponents
from .auth.token import TokenSource, make_token_source
from .runtime import AppRuntime, build_runtime
from .tools.search import register

logger = logging.getLogger(__name__)

INSTRUCTIONS = "Redmine の類似検索サーバー。search_redmine で関連する記録を探し、本文は Redmine MCP で取得する。"


def create_server(
    settings: Settings,
    *,
    token_source: TokenSource | None = None,
    runtime_factory: Callable[[Settings, TokenSource], AppRuntime] | None = None,
    oauth: OAuthComponents | None = None,
) -> MCPServer:
    """``runtime_factory`` is replaced in tests (no DB / model).

    With ``oauth`` this server is its own OAuth2 authorization server and
    resource server; HTTP requests need a valid token and the tools get the user's
    Redmine token from the verified context. Without it, the header token is passed through.
    """
    source = token_source or make_token_source(oauth=oauth is not None)
    factory = runtime_factory or build_runtime

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppRuntime]:
        runtime = factory(settings, source)  # model load: blocks startup once, by design
        try:
            yield runtime
        finally:
            if callable(runtime.close):
                runtime.close()

    kwargs = {}
    if oauth is not None:
        kwargs = {"auth_server_provider": oauth.provider, "auth": oauth.auth_settings()}
    server = MCPServer("redmine-embedding", instructions=INSTRUCTIONS, lifespan=lifespan, **kwargs)
    register(server, settings)
    return server
