import io
import json

import httpx
import pytest

from redmine_embedding.batch import check as chk
from redmine_embedding.batch.__main__ import main
from redmine_embedding.common import config
from redmine_embedding.common.config import Settings
from redmine_embedding.common.redmine.client import RedmineClient
from redmine_embedding.common.requirements import missing_items

KEY = "sekret-api-key-123"


def _settings(**kw):
    base = dict(database_url="postgresql://u:pw@h/db", redmine_url="https://r.example.com", redmine_api_key=KEY)
    base.update(kw)
    return Settings(**base)


# ---- startup check ----
def test_init_db_needs_only_database_url():
    s = _settings(redmine_url="", redmine_api_key=None)
    assert missing_items(s, "init-db") == []
    assert any("DATABASE_URL" in m for m in missing_items(_settings(database_url=""), "init-db"))


def test_sync_lists_every_missing_item_with_where():
    msgs = missing_items(_settings(database_url="", redmine_url="", redmine_api_key=None), "sync")
    text = "\n".join(msgs)
    for item in ("DATABASE_URL", "REDMINE_URL", "REDMINE_API_KEY"):
        assert item in text
    assert "環境変数" in text


def test_bad_format():
    assert any("形式" in m for m in missing_items(_settings(database_url="mysql://x"), "init-db"))
    assert any("形式" in m for m in missing_items(_settings(redmine_url="ftp://x"), "sync"))


def test_mcp_oauth_items():
    s = _settings(mcp_public_base_url="https://m.example.com")
    msgs = "\n".join(missing_items(s, "mcp", oauth=True, environ={}))
    assert "REDMINE_OAUTH_CLIENT_ID" in msgs and "MCP_OAUTH_ENCRYPTION_KEY" in msgs
    assert missing_items(s, "mcp", oauth=False) == []
    assert "mcp_public_base_url" in "\n".join(missing_items(_settings(), "mcp", oauth=True, environ={}))


def test_cli_stops_without_traceback(capsys, monkeypatch):
    monkeypatch.delenv("REDMINE_URL", raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    config.configure()
    assert main(["init-db"]) == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err and "Traceback" not in err


# ---- check command ----
def _client_factory(handler):
    def make(url, **kw):
        return RedmineClient(url, transport=httpx.MockTransport(handler), **kw)
    return make


def _ok_handler(req):
    assert req.method == "GET"
    return httpx.Response(200, json={"user": {"id": 1, "admin": True}})


class _Conn:
    def __init__(self, version="0.8.0", tables=True):
        self.version, self.tables = version, tables

    def execute(self, sql, *a):
        self.sql = sql
        rows = {
            "pg_extension": [self.version] if self.version else None,
            "to_regclass": ("redmine_chunks", "index_meta") if self.tables else (None, None),
            "index_meta": ["m"],
        }
        for k, v in rows.items():
            if k in sql:
                return type("C", (), {"fetchone": lambda self_, v=v: v})()

    def close(self):
        pass


def _run(settings, conn=None, handler=_ok_handler, environ=None, **kw):
    buf = io.StringIO()

    def connect_fn(url, **kw):
        if isinstance(conn, Exception):
            raise conn
        return conn

    code = chk.run_check(settings, environ=environ or {}, connect_fn=connect_fn, client_factory=_client_factory(handler), out=buf, **kw)
    return code, buf.getvalue()


def test_all_ok_and_secrets_masked():
    code, out = _run(_settings(), _Conn())
    assert code == 0 and "すべて OK" in out
    assert KEY not in out and "pw" not in out


def test_db_down():
    code, out = _run(_settings(), RuntimeError("connection refused " + "postgresql://u:pw@h/db"))
    assert code == 1 and "[NG  ] DB 接続" in out and "pw@" not in out and "SKIP" in out


def test_no_pgvector_and_no_schema():
    code, out = _run(_settings(), _Conn(version=None, tables=False))
    assert code == 1 and "[NG  ] pgvector" in out and "[NG  ] スキーマ初期化" in out and "init-db" in out


def test_api_key_invalid():
    code, out = _run(_settings(), _Conn(), handler=lambda r: httpx.Response(401))
    assert code == 1 and "[NG  ] Redmine API" in out and "401" in out and KEY not in out


def test_missing_env_is_ng():
    code, out = _run(_settings(redmine_url="", redmine_api_key=None), _Conn())
    assert code == 1 and "[NG  ] 環境変数 REDMINE_URL" in out and "[NG  ] 環境変数 REDMINE_API_KEY" in out


def test_oauth_checks():
    s = _settings(mcp_public_base_url="https://m.example.com")
    code, out = _run(s, _Conn(), environ={})
    assert code == 1 and "[NG  ] MCP OAuth 環境変数" in out
    from cryptography.fernet import Fernet

    env = {"REDMINE_OAUTH_CLIENT_ID": "a", "REDMINE_OAUTH_CLIENT_SECRET": "oauth-secret-xyz", "MCP_OAUTH_ENCRYPTION_KEY": Fernet.generate_key().decode()}
    code, out = _run(s, _Conn(), environ=env)
    assert code == 0 and env["REDMINE_OAUTH_CLIENT_SECRET"] not in out
    env["MCP_OAUTH_ENCRYPTION_KEY"] = "bad"
    assert _run(s, _Conn(), environ=env)[0] == 1


def test_oauth_unset_is_warn_unless_required():
    code, out = _run(_settings(), _Conn())
    assert code == 0 and "[WARN] MCP OAuth2 設定" in out
    code, out = _run(_settings(), _Conn(), require_oauth=True)
    assert code == 1 and "[NG  ] MCP OAuth2 設定" in out


def test_database_url_keyword_dsn_accepted():
    assert missing_items(_settings(database_url="host=db dbname=x user=u password=p"), "init-db") == []
    assert any("形式" in m for m in missing_items(_settings(database_url="just text"), "init-db"))


def test_cli_check_runs_and_returns_exit_code(capsys, monkeypatch):
    # main(["check"]) の分岐: 必須設定が無ければ NG で終了コード 1（traceback なし）
    monkeypatch.delenv("REDMINE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    config.configure()
    assert main(["check"]) == 1
    cap = capsys.readouterr()
    assert "NG" in cap.out and "Traceback" not in cap.err


def test_cli_check_passes_require_oauth(monkeypatch):
    seen = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@h/db")
    monkeypatch.setenv("REDMINE_URL", "https://r.example.com")
    monkeypatch.setenv("REDMINE_API_KEY", KEY)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    config.configure()
    monkeypatch.setattr("redmine_embedding.batch.check.run_check", lambda s, **kw: seen.update(kw) or 0)
    assert main(["check", "--require-oauth"]) == 0
    assert seen == {"require_oauth": True}


def test_get_selection_reflects_configure():
    config.configure(env="prod")
    assert config.get_selection().env == "prod"
    config.configure()
