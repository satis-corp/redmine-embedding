"""Required settings per command and the startup check.

Each command needs only its own items. ``missing_items`` returns one message per
problem (which item, where to put it); it never contains a value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from .config import Settings

_ENV_HINT = "環境変数（export か .env）で指定してください"
_DB_URL = re.compile(r"^postgres(ql)?://\S+$")
# libpq keyword/value DSN (e.g. "host=db dbname=x user=u"): accepted as psycopg does.
_DB_DSN = re.compile(r"^\s*\w+\s*=\s*\S")
_HTTP_URL = re.compile(r"^https?://[^\s@/]+(/\S*)?$")

OAUTH_ENV = ("REDMINE_OAUTH_CLIENT_ID", "REDMINE_OAUTH_CLIENT_SECRET", "MCP_OAUTH_ENCRYPTION_KEY")

#: command -> env-only items it requires
COMMAND_ITEMS: dict[str, tuple[str, ...]] = {
    "init-db": ("DATABASE_URL",),
    "reconcile": ("DATABASE_URL", "REDMINE_URL", "REDMINE_API_KEY"),
    "sync": ("DATABASE_URL", "REDMINE_URL", "REDMINE_API_KEY"),
    "mcp": ("DATABASE_URL", "REDMINE_URL"),
}


def _value(settings: Settings, item: str) -> str:
    if item == "REDMINE_API_KEY":
        return settings.redmine_api_key.get_secret_value() if settings.redmine_api_key else ""
    return getattr(settings, item.lower()).strip()


def format_problems(settings: Settings) -> dict[str, str]:
    """Format errors of the items that are set (item -> message)."""
    out: dict[str, str] = {}
    db = _value(settings, "DATABASE_URL")
    if db and not (_DB_URL.match(db) or _DB_DSN.match(db)):
        out["DATABASE_URL"] = "DATABASE_URL の形式が不正です: postgresql://user:password@host:5432/dbname（または host=... dbname=... のキーワード形式）にしてください"
    url = _value(settings, "REDMINE_URL")
    if url and not _HTTP_URL.match(url):
        out["REDMINE_URL"] = "REDMINE_URL の形式が不正です: http(s)://host[:port] の形式にしてください"
    return out


def missing_items(
    settings: Settings,
    command: str,
    *,
    oauth: bool = False,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Problems that stop ``command`` from running (empty = ready). ``oauth``: MCP server in OAuth2 mode."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    bad = format_problems(settings)
    for item in COMMAND_ITEMS[command]:
        if not _value(settings, item):
            problems.append(f"{item} が未設定です。{_ENV_HINT}")
        elif item in bad:
            problems.append(bad[item])
    if oauth:
        if not settings.mcp_public_base_url:
            problems.append("mcp_public_base_url が未設定です。設定ファイル（config/settings.<環境>.json）に書いてください")
        names = [n for n in OAUTH_ENV if not env.get(n, "").strip()]
        if names:
            problems.append(f"{', '.join(names)} が未設定です。{_ENV_HINT}")
    return problems


def report_and_exit_code(problems: list[str], command: str) -> int:
    """Print problems to stderr; return the exit code (0 = none)."""
    import sys

    if not problems:
        return 0
    print(f"{command}: 設定が不足・不正です。", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print("  事前確認: redmine-embedding-batch check（--env <環境名> を付ける）", file=sys.stderr)
    return 2
