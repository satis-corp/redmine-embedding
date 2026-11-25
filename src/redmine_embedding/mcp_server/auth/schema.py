"""Tables of the OAuth2 authorization server, same style as ``common/db/schema.py``.

Nothing here holds a usable secret in clear text:
* ``*_hash``: SHA-256 of a random token / code / state that *we* issued;
* ``*_enc``: Fernet-encrypted (upstream Redmine tokens, the PKCE verifier, DCR client records).
Expired rows are ignored by every query; ``OAuthStore.cleanup`` removes them (dry-run by default).
"""

from __future__ import annotations

import psycopg

T_CLIENTS = "mcp_oauth_clients"
T_REQUESTS = "mcp_oauth_auth_requests"
T_CODES = "mcp_oauth_codes"
T_TOKENS = "mcp_oauth_tokens"
T_USED = "mcp_oauth_used_refresh"

DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {T_CLIENTS} (
        client_id    TEXT        PRIMARY KEY,             -- issued by us (DCR)
        info_enc     TEXT        NOT NULL,                -- encrypted OAuthClientInformationFull (JSON; holds the client secret)
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- authorize / token exchange; retention is measured from here
        source       TEXT                                 -- client IP that registered it (per-IP cap)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {T_REQUESTS} (             -- authorization in progress: consent page, then Redmine
        request_hash           TEXT        PRIMARY KEY,   -- sha256 of the request id in the consent URL
        state_hash             TEXT        UNIQUE,        -- sha256 of the state sent to Redmine; NULL until the user approved
        binder_hash            TEXT,                      -- sha256 of the cookie value that binds the browser (consent page -> callback)
        csrf_hash              TEXT,                      -- sha256 of the CSRF token of the consent form
        client_id              TEXT        NOT NULL,
        redirect_uri           TEXT        NOT NULL,      -- the client's
        redirect_uri_explicit  BOOLEAN     NOT NULL,
        code_challenge         TEXT        NOT NULL,      -- the client's PKCE challenge
        client_state           TEXT,                      -- the client's state, echoed back
        scopes                 TEXT        NOT NULL,      -- space separated
        resource               TEXT,
        upstream_verifier_enc  TEXT,                      -- our PKCE verifier towards Redmine (set on approval)
        expires_at             TIMESTAMPTZ NOT NULL,
        source                 TEXT                       -- client IP that started it (per-IP cap)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {T_CODES} (                -- our authorization codes (single use)
        code_hash              TEXT        PRIMARY KEY,
        client_id              TEXT        NOT NULL,
        redirect_uri           TEXT        NOT NULL,
        redirect_uri_explicit  BOOLEAN     NOT NULL,
        code_challenge         TEXT        NOT NULL,
        scopes                 TEXT        NOT NULL,
        resource               TEXT,
        upstream_access_enc    TEXT        NOT NULL,
        upstream_refresh_enc   TEXT,
        upstream_expires_at    TIMESTAMPTZ,
        expires_at             TIMESTAMPTZ NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {T_TOKENS} (               -- our token pair <-> the user's upstream tokens
        id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        access_hash            TEXT        NOT NULL UNIQUE,
        refresh_hash           TEXT        NOT NULL UNIQUE,
        client_id              TEXT        NOT NULL,
        scopes                 TEXT        NOT NULL,
        resource               TEXT,
        upstream_access_enc    TEXT        NOT NULL,
        upstream_refresh_enc   TEXT,
        upstream_expires_at    TIMESTAMPTZ,
        access_expires_at      TIMESTAMPTZ NOT NULL,
        refresh_expires_at     TIMESTAMPTZ NOT NULL,      -- the row (and the upstream tokens in it) lives until here
        created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {T_USED} (                 -- every refresh token a grant has used up (reuse detection); hashes only
        refresh_hash TEXT        PRIMARY KEY,
        token_id     BIGINT      NOT NULL,
        expires_at   TIMESTAMPTZ NOT NULL,                -- the grant's refresh expiry at rotation time
        used_at      TIMESTAMPTZ NOT NULL                 -- when it was used up (grace period for retries)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS {T_REQUESTS}_expires_idx ON {T_REQUESTS} (expires_at)",
    f"CREATE INDEX IF NOT EXISTS {T_CODES}_expires_idx ON {T_CODES} (expires_at)",
    f"CREATE INDEX IF NOT EXISTS {T_TOKENS}_refresh_expires_idx ON {T_TOKENS} (refresh_expires_at)",
    f"CREATE INDEX IF NOT EXISTS {T_USED}_token_idx ON {T_USED} (token_id)",
    f"CREATE INDEX IF NOT EXISTS {T_USED}_expires_idx ON {T_USED} (expires_at)",
    f"CREATE INDEX IF NOT EXISTS {T_TOKENS}_client_idx ON {T_TOKENS} (client_id)",
]


def init_oauth_schema(conn: psycopg.Connection) -> None:
    """Create the tables if missing (idempotent)."""
    with conn.transaction():
        for stmt in DDL:
            conn.execute(stmt)
