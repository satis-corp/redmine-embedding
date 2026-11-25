"""Persistence of the OAuth2 server state.

``OAuthStore`` is synchronous (the provider runs it in a worker thread).
``PgOAuthStore`` is the real one (PostgreSQL); ``MemoryOAuthStore`` has the same
behaviour for tests / development. Values arrive already encrypted or hashed:
the store never sees a plaintext secret.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

import psycopg

from .schema import T_CLIENTS, T_CODES, T_REQUESTS, T_TOKENS, T_USED


@dataclass(frozen=True)
class AuthRequest:
    request_hash: str
    client_id: str
    redirect_uri: str
    redirect_uri_explicit: bool
    code_challenge: str
    client_state: str | None
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: float
    state_hash: str | None = None  # set when the user approved (the state sent to Redmine)
    binder_hash: str | None = None  # hash of the cookie that binds the browser
    csrf_hash: str | None = None
    upstream_verifier_enc: str | None = None
    source: str | None = None  # client IP


@dataclass(frozen=True)
class CodeRecord:
    code_hash: str
    client_id: str
    redirect_uri: str
    redirect_uri_explicit: bool
    code_challenge: str
    scopes: tuple[str, ...]
    resource: str | None
    upstream_access_enc: str
    upstream_refresh_enc: str | None
    upstream_expires_at: float | None
    expires_at: float


@dataclass(frozen=True)
class TokenRecord:
    access_hash: str
    refresh_hash: str
    client_id: str
    scopes: tuple[str, ...]
    resource: str | None
    upstream_access_enc: str
    upstream_refresh_enc: str | None
    upstream_expires_at: float | None
    access_expires_at: float
    refresh_expires_at: float
    id: int | None = None


class OAuthStore(Protocol):
    def save_client(self, client_id: str, info_enc: str, now: float, source: str | None = None) -> None: ...
    def get_client(self, client_id: str) -> str | None: ...
    def touch_client(self, client_id: str, now: float) -> None: ...
    def count_unused_clients(self, now: float, cutoff: float, source: str | None = None) -> int: ...
    def save_auth_request(self, rec: AuthRequest) -> None: ...
    def get_auth_request(self, request_hash: str, now: float) -> AuthRequest | None: ...
    def bind_auth_request(self, request_hash: str, binder_hash: str, csrf_hash: str, now: float) -> bool: ...
    def approve_auth_request(self, request_hash: str, csrf_hash: str, binder_hash: str, state_hash: str,
                             verifier_enc: str, now: float) -> AuthRequest | None: ...
    def delete_auth_request(self, request_hash: str) -> None: ...
    def pop_auth_request(self, state_hash: str, now: float) -> AuthRequest | None: ...
    def count_pending_requests(self, now: float, source: str | None = None) -> int: ...
    def save_code(self, rec: CodeRecord) -> None: ...
    def get_code(self, code_hash: str, now: float) -> CodeRecord | None: ...
    def pop_code(self, code_hash: str, now: float) -> CodeRecord | None: ...
    def create_token(self, rec: TokenRecord) -> TokenRecord: ...
    def get_token_by_access(self, access_hash: str, now: float) -> TokenRecord | None: ...
    def get_token_by_refresh(self, refresh_hash: str, now: float) -> TokenRecord | None: ...
    def get_token_by_used_refresh(self, refresh_hash: str, now: float) -> tuple[TokenRecord, float] | None: ...
    """(grant, time the hash was used up)"""
    def get_token(self, token_id: int, now: float) -> TokenRecord | None: ...
    def update_upstream(self, token_id: int, access_enc: str, refresh_enc: str | None, expires_at: float | None) -> None: ...
    def rotate_token(self, token_id: int, old_refresh_hash: str, new: TokenRecord, now: float) -> bool: ...
    def delete_token(self, token_id: int) -> None: ...
    def cleanup(self, now: float, client_cutoff: float, apply: bool) -> dict[str, int]: ...


def _ts(v: float | None) -> datetime | None:
    return None if v is None else datetime.fromtimestamp(v, tz=timezone.utc)


def _epoch(v: datetime | None) -> float | None:
    return None if v is None else v.timestamp()


def _scopes(s: str) -> tuple[str, ...]:
    return tuple(s.split())


class PgOAuthStore:
    """``connection`` returns a context manager yielding a connection that commits on exit
    (``pool.connection`` of psycopg_pool does)."""

    def __init__(self, connection: Callable[[], AbstractContextManager[psycopg.Connection]]) -> None:
        self._connection = connection

    def save_client(self, client_id, info_enc, now, source=None):
        with self._connection() as c:
            c.execute(
                f"INSERT INTO {T_CLIENTS} (client_id, info_enc, created_at, last_used_at, source) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (client_id) DO UPDATE SET info_enc = EXCLUDED.info_enc",
                (client_id, info_enc, _ts(now), _ts(now), source),
            )

    def get_client(self, client_id):
        with self._connection() as c:
            row = c.execute(f"SELECT info_enc FROM {T_CLIENTS} WHERE client_id = %s", (client_id,)).fetchone()
        return row[0] if row else None

    def touch_client(self, client_id, now):
        with self._connection() as c:
            c.execute(f"UPDATE {T_CLIENTS} SET last_used_at = %s WHERE client_id = %s", (_ts(now), client_id))

    _REQ_COLS = ("request_hash, client_id, redirect_uri, redirect_uri_explicit, code_challenge, client_state, scopes, resource, "
                 "expires_at, state_hash, binder_hash, csrf_hash, upstream_verifier_enc, source")

    @staticmethod
    def _req(row) -> AuthRequest:
        return AuthRequest(row[0], row[1], row[2], row[3], row[4], row[5], _scopes(row[6]), row[7], row[8].timestamp(),
                           row[9], row[10], row[11], row[12], row[13])

    def count_unused_clients(self, now, cutoff, source=None):
        """Clients without a live token that were used / registered after ``cutoff`` (older ones do not count)."""
        with self._connection() as c:
            return c.execute(
                f"SELECT count(*) FROM {T_CLIENTS} WHERE last_used_at > %s AND (%s::text IS NULL OR source = %s) AND NOT EXISTS "
                f"(SELECT 1 FROM {T_TOKENS} t WHERE t.client_id = {T_CLIENTS}.client_id AND t.refresh_expires_at > %s)",
                (_ts(cutoff), source, source, _ts(now)),
            ).fetchone()[0]

    def save_auth_request(self, r):
        with self._connection() as c:
            c.execute(
                f"INSERT INTO {T_REQUESTS} ({self._REQ_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (r.request_hash, r.client_id, r.redirect_uri, r.redirect_uri_explicit, r.code_challenge, r.client_state,
                 " ".join(r.scopes), r.resource, _ts(r.expires_at), r.state_hash, r.binder_hash, r.csrf_hash, r.upstream_verifier_enc, r.source),
            )

    def get_auth_request(self, request_hash, now):
        with self._connection() as c:
            row = c.execute(f"SELECT {self._REQ_COLS} FROM {T_REQUESTS} WHERE request_hash = %s AND expires_at > %s",
                            (request_hash, _ts(now))).fetchone()
        return self._req(row) if row else None

    def bind_auth_request(self, request_hash, binder_hash, csrf_hash, now):
        with self._connection() as c:
            cur = c.execute(
                f"UPDATE {T_REQUESTS} SET binder_hash = %s, csrf_hash = %s "
                "WHERE request_hash = %s AND binder_hash IS NULL AND state_hash IS NULL AND expires_at > %s",
                (binder_hash, csrf_hash, request_hash, _ts(now)),
            )
            return cur.rowcount == 1

    def approve_auth_request(self, request_hash, csrf_hash, binder_hash, state_hash, verifier_enc, now):
        with self._connection() as c:
            row = c.execute(
                f"UPDATE {T_REQUESTS} SET state_hash = %s, upstream_verifier_enc = %s, csrf_hash = NULL "
                "WHERE request_hash = %s AND csrf_hash = %s AND binder_hash = %s AND state_hash IS NULL AND expires_at > %s "
                f"RETURNING {self._REQ_COLS}",
                (state_hash, verifier_enc, request_hash, csrf_hash, binder_hash, _ts(now)),
            ).fetchone()
        return self._req(row) if row else None

    def delete_auth_request(self, request_hash):
        with self._connection() as c:
            c.execute(f"DELETE FROM {T_REQUESTS} WHERE request_hash = %s", (request_hash,))

    def pop_auth_request(self, state_hash, now):
        with self._connection() as c:
            row = c.execute(f"DELETE FROM {T_REQUESTS} WHERE state_hash = %s RETURNING {self._REQ_COLS}", (state_hash,)).fetchone()
        if row is None or row[8].timestamp() <= now:
            return None
        return self._req(row)

    def count_pending_requests(self, now, source=None):
        with self._connection() as c:
            return c.execute(f"SELECT count(*) FROM {T_REQUESTS} WHERE expires_at > %s AND (%s::text IS NULL OR source = %s)",
                             (_ts(now), source, source)).fetchone()[0]

    _CODE_COLS = ("code_hash, client_id, redirect_uri, redirect_uri_explicit, code_challenge, scopes, resource, "
                  "upstream_access_enc, upstream_refresh_enc, upstream_expires_at, expires_at")

    @staticmethod
    def _code(row) -> CodeRecord:
        return CodeRecord(row[0], row[1], row[2], row[3], row[4], _scopes(row[5]), row[6], row[7], row[8],
                          _epoch(row[9]), row[10].timestamp())

    def save_code(self, r):
        with self._connection() as c:
            c.execute(
                f"INSERT INTO {T_CODES} ({self._CODE_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (r.code_hash, r.client_id, r.redirect_uri, r.redirect_uri_explicit, r.code_challenge, " ".join(r.scopes),
                 r.resource, r.upstream_access_enc, r.upstream_refresh_enc, _ts(r.upstream_expires_at), _ts(r.expires_at)),
            )

    def get_code(self, code_hash, now):
        with self._connection() as c:
            row = c.execute(f"SELECT {self._CODE_COLS} FROM {T_CODES} WHERE code_hash = %s AND expires_at > %s",
                            (code_hash, _ts(now))).fetchone()
        return self._code(row) if row else None

    def pop_code(self, code_hash, now):
        with self._connection() as c:
            row = c.execute(f"DELETE FROM {T_CODES} WHERE code_hash = %s RETURNING {self._CODE_COLS}", (code_hash,)).fetchone()
        if row is None or row[10].timestamp() <= now:
            return None
        return self._code(row)

    _TOKEN_COLS = ("id, access_hash, refresh_hash, client_id, scopes, resource, upstream_access_enc, upstream_refresh_enc, "
                   "upstream_expires_at, access_expires_at, refresh_expires_at")

    @staticmethod
    def _token(row) -> TokenRecord:
        return TokenRecord(row[1], row[2], row[3], _scopes(row[4]), row[5], row[6], row[7], _epoch(row[8]),
                           row[9].timestamp(), row[10].timestamp(), id=row[0])

    def create_token(self, r):
        with self._connection() as c:
            row = c.execute(
                f"INSERT INTO {T_TOKENS} (access_hash, refresh_hash, client_id, scopes, resource, upstream_access_enc, "
                "upstream_refresh_enc, upstream_expires_at, access_expires_at, refresh_expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (r.access_hash, r.refresh_hash, r.client_id, " ".join(r.scopes), r.resource, r.upstream_access_enc,
                 r.upstream_refresh_enc, _ts(r.upstream_expires_at), _ts(r.access_expires_at), _ts(r.refresh_expires_at)),
            ).fetchone()
        return replace(r, id=row[0])

    def _get(self, where: str, arg, now: float) -> TokenRecord | None:
        with self._connection() as c:
            row = c.execute(f"SELECT {self._TOKEN_COLS} FROM {T_TOKENS} WHERE {where} AND refresh_expires_at > %s",
                            (arg, _ts(now))).fetchone()
        return self._token(row) if row else None

    def get_token_by_access(self, access_hash, now):
        return self._get("access_hash = %s", access_hash, now)

    def get_token_by_refresh(self, refresh_hash, now):
        return self._get("refresh_hash = %s", refresh_hash, now)

    def get_token_by_used_refresh(self, refresh_hash, now):
        with self._connection() as c:
            u = c.execute(f"SELECT token_id, used_at FROM {T_USED} WHERE refresh_hash = %s", (refresh_hash,)).fetchone()
        if u is None:
            return None
        tok = self.get_token(u[0], now)
        return (tok, u[1].timestamp()) if tok else None

    def get_token(self, token_id, now):
        return self._get("id = %s", token_id, now)

    def update_upstream(self, token_id, access_enc, refresh_enc, expires_at):
        with self._connection() as c:
            c.execute(
                f"UPDATE {T_TOKENS} SET upstream_access_enc = %s, upstream_refresh_enc = %s, upstream_expires_at = %s WHERE id = %s",
                (access_enc, refresh_enc, _ts(expires_at), token_id),
            )

    def rotate_token(self, token_id, old_refresh_hash, new, now):
        with self._connection() as c:  # one transaction: swap the hashes and remember the used-up one
            cur = c.execute(
                f"UPDATE {T_TOKENS} SET access_hash = %s, refresh_hash = %s, access_expires_at = %s, refresh_expires_at = %s "
                "WHERE id = %s AND refresh_hash = %s",
                (new.access_hash, new.refresh_hash, _ts(new.access_expires_at), _ts(new.refresh_expires_at), token_id, old_refresh_hash),
            )
            if cur.rowcount != 1:
                return False
            c.execute(f"INSERT INTO {T_USED} (refresh_hash, token_id, expires_at, used_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                      (old_refresh_hash, token_id, _ts(new.refresh_expires_at), _ts(now)))
            return True

    def delete_token(self, token_id):
        with self._connection() as c:
            c.execute(f"DELETE FROM {T_TOKENS} WHERE id = %s", (token_id,))
            c.execute(f"DELETE FROM {T_USED} WHERE token_id = %s", (token_id,))

    def cleanup(self, now, client_cutoff, apply):
        now_ts, cut = _ts(now), _ts(client_cutoff)
        conds = {
            "auth_requests": (T_REQUESTS, "expires_at <= %s", (now_ts,)),
            "codes": (T_CODES, "expires_at <= %s", (now_ts,)),
            "tokens": (T_TOKENS, "refresh_expires_at <= %s", (now_ts,)),
            # used-up refresh hashes: past expiry, or their grant is gone (tokens are handled before this one)
            "used_refresh": (T_USED, f"expires_at <= %s OR token_id NOT IN (SELECT id FROM {T_TOKENS})", (now_ts,)),
        }
        counts: dict[str, int] = {}
        with self._connection() as c:
            for name, (table, cond, args) in conds.items():
                if apply:
                    counts[name] = c.execute(f"DELETE FROM {table} WHERE {cond}", args).rowcount
                else:
                    counts[name] = c.execute(f"SELECT count(*) FROM {table} WHERE {cond}", args).fetchone()[0]
            # a client with a live token or an in-flight authorization is never a candidate
            where = (f"last_used_at <= %s AND NOT EXISTS (SELECT 1 FROM {T_TOKENS} t WHERE t.client_id = {T_CLIENTS}.client_id "
                     f"AND t.refresh_expires_at > %s) AND NOT EXISTS (SELECT 1 FROM {T_REQUESTS} r "
                     f"WHERE r.client_id = {T_CLIENTS}.client_id AND r.expires_at > %s)")
            if apply:
                counts["clients"] = c.execute(f"DELETE FROM {T_CLIENTS} WHERE {where}", (cut, now_ts, now_ts)).rowcount
            else:
                counts["clients"] = c.execute(f"SELECT count(*) FROM {T_CLIENTS} WHERE {where}", (cut, now_ts, now_ts)).fetchone()[0]
        return counts


class MemoryOAuthStore:
    """Same behaviour as ``PgOAuthStore`` (tests / development; lost at restart)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.clients: dict[str, list] = {}  # client_id -> [info_enc, last_used_at, source]
        self.used: dict[str, tuple[int, float, float]] = {}  # used-up refresh hash -> (token_id, expires_at, used_at)
        self.requests: dict[str, AuthRequest] = {}
        self.codes: dict[str, CodeRecord] = {}
        self.tokens: dict[int, TokenRecord] = {}
        self._next_id = 1

    def save_client(self, client_id, info_enc, now, source=None):
        with self._lock:
            old = self.clients.get(client_id)
            self.clients[client_id] = [info_enc, old[1] if old else now, old[2] if old else source]

    def get_client(self, client_id):
        with self._lock:
            c = self.clients.get(client_id)
            return c[0] if c else None

    def touch_client(self, client_id, now):
        with self._lock:
            if client_id in self.clients:
                self.clients[client_id][1] = now

    def count_unused_clients(self, now, cutoff, source=None):
        with self._lock:
            busy = {t.client_id for t in self.tokens.values() if t.refresh_expires_at > now}
            return sum(1 for k, v in self.clients.items() if v[1] > cutoff and k not in busy and (source is None or v[2] == source))

    def save_auth_request(self, rec):
        with self._lock:
            self.requests[rec.request_hash] = rec

    def get_auth_request(self, request_hash, now):
        with self._lock:
            rec = self.requests.get(request_hash)
        return rec if rec and rec.expires_at > now else None

    def bind_auth_request(self, request_hash, binder_hash, csrf_hash, now):
        with self._lock:
            rec = self.requests.get(request_hash)
            if not rec or rec.binder_hash is not None or rec.state_hash is not None or rec.expires_at <= now:
                return False
            self.requests[request_hash] = replace(rec, binder_hash=binder_hash, csrf_hash=csrf_hash)
            return True

    def approve_auth_request(self, request_hash, csrf_hash, binder_hash, state_hash, verifier_enc, now):
        with self._lock:
            rec = self.requests.get(request_hash)
            if (not rec or rec.state_hash is not None or rec.expires_at <= now or rec.csrf_hash != csrf_hash
                    or rec.binder_hash != binder_hash):
                return None
            rec = replace(rec, state_hash=state_hash, upstream_verifier_enc=verifier_enc, csrf_hash=None)
            self.requests[request_hash] = rec
            return rec

    def delete_auth_request(self, request_hash):
        with self._lock:
            self.requests.pop(request_hash, None)

    def pop_auth_request(self, state_hash, now):
        with self._lock:
            key = next((k for k, v in self.requests.items() if v.state_hash == state_hash), None)
            rec = self.requests.pop(key, None) if key else None
        return rec if rec and rec.expires_at > now else None

    def count_pending_requests(self, now, source=None):
        with self._lock:
            return sum(1 for v in self.requests.values() if v.expires_at > now and (source is None or v.source == source))

    def save_code(self, rec):
        with self._lock:
            self.codes[rec.code_hash] = rec

    def get_code(self, code_hash, now):
        with self._lock:
            rec = self.codes.get(code_hash)
        return rec if rec and rec.expires_at > now else None

    def pop_code(self, code_hash, now):
        with self._lock:
            rec = self.codes.pop(code_hash, None)
        return rec if rec and rec.expires_at > now else None

    def create_token(self, rec):
        with self._lock:
            rec = replace(rec, id=self._next_id)
            self._next_id += 1
            self.tokens[rec.id] = rec
        return rec

    def _find(self, pred, now):
        with self._lock:
            for t in self.tokens.values():
                if pred(t) and t.refresh_expires_at > now:
                    return t
        return None

    def get_token_by_access(self, access_hash, now):
        return self._find(lambda t: t.access_hash == access_hash, now)

    def get_token_by_refresh(self, refresh_hash, now):
        return self._find(lambda t: t.refresh_hash == refresh_hash, now)

    def get_token_by_used_refresh(self, refresh_hash, now):
        with self._lock:
            used = self.used.get(refresh_hash)
        if used is None:
            return None
        tok = self._find(lambda t: t.id == used[0], now)
        return (tok, used[2]) if tok else None

    def get_token(self, token_id, now):
        return self._find(lambda t: t.id == token_id, now)

    def update_upstream(self, token_id, access_enc, refresh_enc, expires_at):
        with self._lock:
            t = self.tokens.get(token_id)
            if t:
                self.tokens[token_id] = replace(t, upstream_access_enc=access_enc, upstream_refresh_enc=refresh_enc, upstream_expires_at=expires_at)

    def rotate_token(self, token_id, old_refresh_hash, new, now):
        with self._lock:
            t = self.tokens.get(token_id)
            if not t or t.refresh_hash != old_refresh_hash:
                return False
            self.used[old_refresh_hash] = (token_id, new.refresh_expires_at, now)
            self.tokens[token_id] = replace(t, access_hash=new.access_hash, refresh_hash=new.refresh_hash,
                                            access_expires_at=new.access_expires_at, refresh_expires_at=new.refresh_expires_at)
            return True

    def delete_token(self, token_id):
        with self._lock:
            self.tokens.pop(token_id, None)
            self.used = {h: v for h, v in self.used.items() if v[0] != token_id}

    def cleanup(self, now, client_cutoff, apply):
        with self._lock:
            dead = {
                "auth_requests": [k for k, v in self.requests.items() if v.expires_at <= now],
                "codes": [k for k, v in self.codes.items() if v.expires_at <= now],
                "tokens": [k for k, v in self.tokens.items() if v.refresh_expires_at <= now],
            }
            dead["used_refresh"] = [h for h, (tid, exp, _used) in self.used.items() if exp <= now or tid in dead["tokens"] or tid not in self.tokens]
            busy = {t.client_id for t in self.tokens.values() if t.refresh_expires_at > now} | {
                r.client_id for r in self.requests.values() if r.expires_at > now
            }
            dead_clients = [k for k, v in self.clients.items() if v[1] <= client_cutoff and k not in busy]
            if apply:
                for k in dead["auth_requests"]:
                    del self.requests[k]
                for k in dead["codes"]:
                    del self.codes[k]
                for k in dead["tokens"]:
                    del self.tokens[k]
                for k in dead["used_refresh"]:
                    del self.used[k]
                for k in dead_clients:
                    del self.clients[k]
            counts = {k: len(v) for k, v in dead.items()}
            counts["clients"] = len(dead_clients)
            return counts
