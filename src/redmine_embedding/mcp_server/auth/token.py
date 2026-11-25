"""Where the caller's Redmine token comes from.

Tools only call a ``TokenSource``; with OAuth2 the source is
``access_token_context`` instead of ``bearer_header_token``, the tools are
unchanged. The token is passed to Redmine as is. It is never stored, logged or put into an error message.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

#: A source returns the token, or None when the caller sent none.
TokenSource = Callable[[Any], "str | None"]

DEV_TOKEN_ENV = "REDMINE_MCP_DEV_TOKEN"


def bearer_header_token(ctx: Any) -> str | None:
    """``Authorization: Bearer <token>`` of the HTTP request (None over stdio / without it)."""
    request = getattr(ctx.request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def access_token_context(ctx: Any) -> str | None:
    """OAuth2 mode: the user's Redmine token, mapped from the verified bearer token by the provider.

    The Authorization header holds *our* token in this mode, so it must never be passed to Redmine as is.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    access = get_access_token()
    return access.token if access is not None else None


def make_token_source(*, allow_dev_token: bool = False, environ: dict[str, str] | None = None, oauth: bool = False) -> TokenSource:
    """Header first (``oauth``: the verified OAuth2 context instead). The env token (development only)
    is used only when ``allow_dev_token`` is set explicitly."""
    env = os.environ if environ is None else environ
    from_request = access_token_context if oauth else bearer_header_token

    def source(ctx: Any) -> str | None:
        token = from_request(ctx)
        if token is None and allow_dev_token:
            token = env.get(DEV_TOKEN_ENV, "").strip() or None
        return token

    return source
