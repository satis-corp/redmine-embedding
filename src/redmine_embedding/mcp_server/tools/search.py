"""The ``search_redmine`` tool. Similarity search only; no LLM is called here."""

import logging
import time
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ...common.config import Settings
from ..errors import (
    MSG_AUTH_MISSING,
    MSG_TIMEOUT,
    msg_all_unverified,
    to_tool_error,
)
from ..runtime import AppRuntime, source_types
from ..search.excerpt import make_excerpt
from ..search.visible_projects import token_key
from .schemas import SearchResponse, SearchResult

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Redmine のチケット・wiki・ニュースなどから、意味が近いものをベクトル検索する（キーワード一致ではない）。"
    "呼び出し元ユーザーが閲覧できるものだけを返す。各結果は種別・id・プロジェクト・タイトル・抜粋・類似度・URL で、"
    "本文全体は返さない。本文が必要なときは、結果の type と id を使って Redmine MCP の取得ツール"
    "（チケットなら get_redmine_issue など）を呼ぶこと。"
    "incomplete が true のときは、権限確認の失敗や時間切れで結果が欠けている可能性がある。"
)


def register(server: MCPServer, settings: Settings) -> None:
    max_limit = settings.search_max_limit
    types_allowed = source_types()
    SourceType = Literal[tuple(types_allowed)]  # type: ignore[valid-type]

    QueryT = Annotated[str, Field(min_length=1, max_length=1000, description="検索文（自然文でよい）")]
    TypesT = Annotated[
        list[SourceType] | None, Field(description=f"対象種別。省略時はすべて。{', '.join(types_allowed)}")
    ]
    ProjectsT = Annotated[
        list[int] | None,
        Field(max_length=MAX_PROJECTS, description=f"プロジェクト ID（整数）で絞り込む。見えないプロジェクトは無視される（最大 {MAX_PROJECTS} 件、空は指定なし）")
    ]
    LimitT = Annotated[
        int | None,
        Field(ge=1, le=max_limit, description=f"返す件数（1〜{max_limit}、省略時 {settings.search_default_limit}）"),
    ]

    def search_redmine(
        query: QueryT,
        ctx: Context,
        types: TypesT = None,
        projects: ProjectsT = None,
        limit: LimitT = None,
    ) -> SearchResponse:
        runtime: AppRuntime = ctx.request_context.lifespan_context
        return run_search(runtime, ctx, query, types, projects, limit)

    server.add_tool(search_redmine, name="search_redmine", description=DESCRIPTION)


MAX_PROJECTS = 50  # 暫定: upper bound of the `projects` filter


def run_search(
    runtime: AppRuntime,
    ctx: Context,
    query: str,
    types: list[str] | None,
    projects: list[int] | None,
    limit: int | None,
) -> SearchResponse:
    try:
        return _run_search(runtime, ctx, query, types, projects, limit)
    except Exception as e:  # noqa: BLE001 - ToolError passes through; the rest gets a safe message
        raise to_tool_error(e) from None  # `from None`: keep the token-bearing chain out of the log


def _run_search(
    runtime: AppRuntime,
    ctx: Context,
    query: str,
    types: list[str] | None,
    projects: list[int] | None,
    limit: int | None,
) -> SearchResponse:
    settings = runtime.settings
    if not query.strip():
        raise ToolError("query が空です")
    token = runtime.token_source(ctx)
    if not token:
        raise ToolError(MSG_AUTH_MISSING)
    started = time.monotonic()
    # 暫定: an empty list means "not specified" (same as None), not an input error.
    result = runtime.searcher.search(
        query,
        token=token,
        limit=settings.search_default_limit if limit is None else limit,
        project_ids=projects or None,
        source_types=types or None,
        deadline=started + settings.search_timeout_seconds,
    )
    elapsed = time.monotonic() - started
    # No query text, no token: only a short hash to tell callers apart.
    logger.info(
        "search done user=%s kept=%d unverified=%d exhausted=%s timed_out=%s elapsed=%.2fs",
        token_key(token)[:8], len(result.kept), result.unverified_count, result.exhausted, result.timed_out, elapsed,
    )
    if not result.kept and result.timed_out:
        raise ToolError(MSG_TIMEOUT)
    if not result.kept and result.unverified_count > 0:
        raise ToolError(msg_all_unverified(result.unverified_count))

    base = settings.redmine_url.rstrip("/")
    items = [
        SearchResult(
            type=h.source_type,
            id=h.source_id,
            project_id=h.project_id,
            title=h.title,
            excerpt=make_excerpt(
                h.content, settings.search_excerpt_chars, mask=settings.search_excerpt_mask_credentials
            ),
            score=h.score,
            url=base + (h.url_path or ""),
            chunk_no=h.chunk_no,
        )
        for h in result.kept
    ]
    incomplete = result.unverified_count > 0 or result.timed_out or result.exhausted
    notice = None
    if incomplete:
        notice = "一部の結果は権限を確認できなかった、または時間・件数の上限に達したため、欠けている可能性があります。"
    return SearchResponse(
        results=items,
        count=len(items),
        incomplete=incomplete,
        unverified_count=result.unverified_count,
        notice=notice,
    )
