"""Search exceptions -> messages a model can act on.

Everything raised inside the tool becomes a tool error (``isError``), never a
JSON-RPC protocol error. Details (stack traces, tokens) go to the server log only.
"""

from __future__ import annotations

import logging
import traceback

import httpx
import psycopg
from mcp.server.mcpserver.exceptions import ToolError

from ..common.redmine.errors import RedmineAuthError, RedmineError, RedmineForbiddenError

logger = logging.getLogger(__name__)

MSG_AUTH_MISSING = "認証情報がありません。Redmine にログインし直してから再試行してください"
MSG_AUTH_EXPIRED = "認証が切れています。再ログインしてください"
MSG_FORBIDDEN = "Redmine が閲覧を拒否しました。権限を確認してください"
MSG_REDMINE_DOWN = "Redmine に接続できません。時間をおいて再試行してください"
MSG_DB_DOWN = "検索データベースに接続できません。時間をおいて再試行してください"
MSG_INTERNAL = "検索処理に失敗しました。時間をおいて再試行してください"


def msg_all_unverified(n: int) -> str:
    return f"権限の確認ができず結果を返せませんでした（{n} 件）。0 件ではありません。再試行してください"


MSG_TIMEOUT = "検索が時間内に終わりませんでした。条件を絞るか、時間をおいて再試行してください"


def to_tool_error(exc: Exception) -> ToolError:
    """Map an exception from the search to a ``ToolError`` with a safe message."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, RedmineAuthError):
        return ToolError(MSG_AUTH_EXPIRED)
    if isinstance(exc, RedmineForbiddenError) or (isinstance(exc, RedmineError) and exc.status_code == 403):
        return ToolError(MSG_FORBIDDEN)
    if isinstance(exc, (RedmineError, httpx.HTTPError)):
        logger.warning("redmine call failed: %s", type(exc).__name__)
        return ToolError(MSG_REDMINE_DOWN)
    if isinstance(exc, psycopg.OperationalError):  # includes psycopg_pool.PoolTimeout
        logger.error("database unavailable: %s", type(exc).__name__)
        return ToolError(MSG_DB_DOWN)
    # Type and frames only: the message may contain request details (e.g. a token).
    logger.error("search failed unexpectedly: %s\n%s", type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)))
    return ToolError(MSG_INTERNAL)
