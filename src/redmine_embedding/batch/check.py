"""``redmine-embedding-batch check``: pre-flight check before a deployment.

Read-only: one SELECT-only DB session and one Redmine GET. No login to the
OAuth app is attempted (only the format of the settings is checked). Secrets are
never printed (only masked / presence).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..common import config
from ..common.config import Settings
from ..common.db import connect
from ..common.db.repository import ChunkRepository
from ..common.redmine.client import RedmineClient
from ..common.redmine.errors import RedmineAuthError, RedmineError, RedmineForbiddenError
from ..common.requirements import OAUTH_ENV, format_problems
from ..common.settings_file import resolve_path

OK, NG, SKIP, WARN = "OK", "NG", "SKIP", "WARN"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _mask(value: str, keep: int = 0) -> str:
    return "(未設定)" if not value else ("*" * 8 + (value[-keep:] if keep else ""))


def _scrub(text: str, settings: Settings) -> str:
    """One line, with the secret values removed."""
    secrets = [settings.database_url, settings.redmine_api_key.get_secret_value() if settings.redmine_api_key else ""]
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return " ".join(text.split())[:200]


def check_env(settings: Settings) -> list[Result]:
    out = []
    bad = format_problems(settings)
    key = settings.redmine_api_key.get_secret_value() if settings.redmine_api_key else ""
    for item, value, shown in (
        ("DATABASE_URL", settings.database_url, _mask(settings.database_url)),
        ("REDMINE_URL", settings.redmine_url, settings.redmine_url),
        ("REDMINE_API_KEY", key, _mask(key)),
    ):
        if not value.strip():
            out.append(Result(f"環境変数 {item}", NG, "未設定", "環境変数（export か .env）で指定する（.env.example 参照）"))
        elif item in bad:
            out.append(Result(f"環境変数 {item}", NG, "形式が不正", bad[item]))
        else:
            out.append(Result(f"環境変数 {item}", OK, shown))
    return out


def check_oauth(settings: Settings, environ, *, require: bool = False) -> list[Result]:
    base = settings.mcp_public_base_url
    if not base:
        detail = "無効（mcp_public_base_url 未設定）。外部の MCP クライアントから使う本番では必須"
        hint = "設定ファイル（config/settings.<環境>.json）に mcp_public_base_url を書く"
        if require:
            return [Result("MCP OAuth2 設定", NG, detail, hint)]
        return [Result("MCP OAuth2 設定", WARN, detail, hint)]
    host = urlsplit(base).netloc
    out = [Result("MCP mcp_public_base_url", OK, f"{host}{settings.mcp_public_path}")]
    missing = [n for n in OAUTH_ENV if not environ.get(n, "").strip()]
    if missing:
        out.append(Result("MCP OAuth 環境変数", NG, "未設定: " + ", ".join(missing), "環境変数（export か EnvironmentFile）で指定する"))
    else:
        out.append(Result("MCP OAuth 環境変数", OK, "3 項目とも設定済み（値は表示しない）"))
        try:
            from cryptography.fernet import Fernet

            for k in environ["MCP_OAUTH_ENCRYPTION_KEY"].split(","):
                Fernet(k.strip().encode())
        except Exception:
            out[-1] = Result("MCP OAuth 環境変数", NG, "MCP_OAUTH_ENCRYPTION_KEY が Fernet 鍵の形式ではない",
                             'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" で作る')
    if not base.startswith("https://") and urlsplit(base).hostname not in ("localhost", "127.0.0.1"):
        out.append(Result("MCP 公開 URL", NG, "https ではありません", "本番は https の公開 URL にする（Redmine の OAuth リダイレクト URI と一致させる）"))
    return out


def check_db(settings: Settings, connect_fn: Callable = connect) -> list[Result]:
    name = "DB 接続"
    try:
        conn = connect_fn(settings.database_url, autocommit=True)
    except Exception as e:
        return [
            Result(name, NG, f"{type(e).__name__}: {_scrub(str(e), settings)}", "DATABASE_URL のホスト・ポート・認証・DB 名と、DB の起動を確認する"),
            Result("pgvector 拡張", SKIP, "DB 接続が NG のため未確認"),
            Result("スキーマ初期化", SKIP, "DB 接続が NG のため未確認"),
        ]
    out = [Result(name, OK)]
    try:
        version = ChunkRepository(conn).pgvector_version()
        if version is None:
            out.append(Result("pgvector 拡張", NG, "拡張 vector が未作成", "init-db を実行（権限不足なら DB 管理者が CREATE EXTENSION vector）。pgvector 入りの PostgreSQL か確認"))
        else:
            out.append(Result("pgvector 拡張", OK, f"version {version}"))
        row = conn.execute("SELECT to_regclass('redmine_chunks'), to_regclass('index_meta')").fetchone()
        if not row[0] or not row[1]:
            out.append(Result("スキーマ初期化", NG, "テーブルがありません", "redmine-embedding-batch init-db を実行する"))
        else:
            m = conn.execute("SELECT value FROM index_meta WHERE key = 'embedding_model'").fetchone()
            out.append(Result("スキーマ初期化", OK, f"embedding_model={m[0]}" if m else "初期化済み（モデル未記録）"))
    except Exception as e:
        out.append(Result("スキーマ確認", NG, f"{type(e).__name__}: {_scrub(str(e), settings)}", "DB の権限・状態を確認する"))
    finally:
        conn.close()
    return out


def check_redmine(settings: Settings, client_factory: Callable = RedmineClient) -> list[Result]:
    name = "Redmine API"
    key = settings.redmine_api_key.get_secret_value() if settings.redmine_api_key else ""
    if not settings.redmine_url or not key:
        return [Result(name, SKIP, "REDMINE_URL / REDMINE_API_KEY 未設定のため未確認")]
    try:
        with client_factory(settings.redmine_url, api_key=key, timeout=settings.redmine_timeout, max_retries=0) as c:
            user = c.get("/users/current.json").get("user", {})
    except RedmineAuthError:
        return [Result(name, NG, "401 認証エラー", "REDMINE_API_KEY が無効。Redmine の「個人設定」で API キーを確認し、REST API の有効化も確認する")]
    except RedmineForbiddenError:
        return [Result(name, NG, "403 権限エラー", "API キーのユーザー権限（管理者か）を確認する")]
    except RedmineError as e:
        return [Result(name, NG, _scrub(str(e), settings), "REDMINE_URL（https/ホスト/ポート）とネットワーク到達性を確認する")]
    except Exception as e:
        return [Result(name, NG, f"{type(e).__name__}: {_scrub(str(e), settings)}", "REDMINE_URL とネットワーク到達性を確認する")]
    admin = user.get("admin")
    extra = "" if admin is None else ("、管理者" if admin else "、管理者ではない（非公開データの取りこぼしに注意）")
    return [Result(name, OK, f"到達・API キー有効（user id={user.get('id', '?')}{extra}）")]


def run_check(settings: Settings, *, environ=None, require_oauth: bool = False, connect_fn: Callable = connect, client_factory: Callable = RedmineClient, out=None) -> int:
    import os

    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    path, _, how = resolve_path(config.get_selection())
    results = [Result("設定ファイル", OK, f"{path}（{how}）" + ("" if path.is_file() else " ※存在せず既定値で動作"))]
    results += check_env(settings)
    results += check_db(settings, connect_fn) if settings.database_url else [Result("DB 接続", SKIP, "DATABASE_URL 未設定のため未確認")]
    results += check_redmine(settings, client_factory)
    results += check_oauth(settings, environ, require=require_oauth)
    for r in results:
        print(f"[{r.status:4}] {r.name}" + (f": {r.detail}" if r.detail else ""), file=out)
        if r.status in (NG, WARN) and r.hint:
            print(f"         対処: {r.hint}", file=out)
    ng = sum(r.status == NG for r in results)
    print(f"\n{'NG が ' + str(ng) + ' 件あります' if ng else 'すべて OK'}", file=out)
    return 1 if ng else 0
