import json
import logging
from pathlib import Path

import pytest

from redmine_embedding.common import config, settings_file
from redmine_embedding.common.config import Settings, configure
from redmine_embedding.common.settings_file import SettingsFileError, settings_keys

REPO = Path(__file__).resolve().parents[2]
JSON_KEYS = {
    "embedding_model", "embedding_device", "redmine_timeout", "redmine_max_retries",
    "reconcile_apply_deletions", "chunk_max_chars", "sync_overlap_minutes", "embed_flush_size",
    "embedding_batch_size", "attachment_max_bytes", "attachment_text_max_chars",
    "redmine_request_interval", "search_top_k", "visible_projects_cache_ttl",
    "visible_projects_cache_max_entries", "search_max_rounds", "search_check_max_requests",
    "search_check_workers", "search_default_limit", "search_check_cache_ttl",
    "search_check_cache_max_entries", "mcp_host", "mcp_port", "search_timeout_seconds",
    "search_max_limit", "search_excerpt_chars", "search_excerpt_mask_credentials", "mcp_db_pool_max", "mcp_embed_concurrency",
    "mcp_redmine_timeout", "mcp_redmine_max_retries",
    "mcp_public_base_url", "mcp_public_path", "mcp_oauth_scopes", "mcp_oauth_introspect_cache_ttl",
    "mcp_oauth_access_token_ttl", "mcp_oauth_refresh_token_ttl", "mcp_oauth_client_retention_days",
    "mcp_oauth_cleanup_apply", "mcp_oauth_cleanup_interval", "mcp_oauth_max_clients",
    "mcp_oauth_max_pending_requests", "mcp_oauth_rate_limit_per_minute", "mcp_oauth_max_clients_per_ip",
    "mcp_oauth_max_pending_per_ip", "mcp_oauth_refresh_grace_seconds",
}


def put(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(path)


def test_no_file_uses_defaults():
    s = Settings(_env_file=None)
    assert s.chunk_max_chars == 1000 and s.search_top_k == 50


def test_default_file(tmp_path):
    put("config/settings.json", {"search_top_k": 7, "$schema": "x"})
    assert Settings(_env_file=None).search_top_k == 7


def test_priority_config_over_active_over_default():
    put("config/settings.json", {"search_top_k": 1})
    put("config/settings.prod.json", {"search_top_k": 2})
    put("config/settings.dev.json", {"search_top_k": 3})
    p = put("other/x.json", {"search_top_k": 4})
    assert Settings(_env_file=None).search_top_k == 1
    put("config/active", "prod")
    assert Settings(_env_file=None).search_top_k == 2
    configure(env="dev")
    assert Settings(_env_file=None).search_top_k == 3
    configure(config=p)
    assert Settings(_env_file=None).search_top_k == 4  # --config is relative to the cwd


def test_active_file_format():
    put("config/settings.prod.json", {"search_top_k": 2})
    put("config/active", "# comment\n\n   prod  \nignored\n")
    assert Settings(_env_file=None).search_top_k == 2
    put("config/other/y.json", {"search_top_k": 9})
    put("config/active", "# path form (relative to config/)\nother/y.json\n")
    assert Settings(_env_file=None).search_top_k == 9
    put("config/active", "settings.prod.json\n")  # .json suffix = path, not an env name
    assert Settings(_env_file=None).search_top_k == 2
    abs_p = put("elsewhere/a.json", {"search_top_k": 8})
    put("config/active", str(Path(abs_p).resolve()))
    assert Settings(_env_file=None).search_top_k == 8
    put("config/active", "# only comments\n\n")
    assert Settings(_env_file=None).search_top_k == 50  # falls back to the default


def test_explicit_missing_is_error():
    configure(config="nope.json")
    with pytest.raises(SettingsFileError, match="nope.json"):
        Settings(_env_file=None)
    configure(env="ghost")
    with pytest.raises(SettingsFileError, match="settings.ghost.json"):
        Settings(_env_file=None)
    configure()
    put("config/active", "ghost2")
    with pytest.raises(SettingsFileError, match="ghost2"):
        Settings(_env_file=None)


def test_bad_env_name_and_both_flags():
    configure(env="../x")
    with pytest.raises(SettingsFileError, match="環境名"):
        Settings(_env_file=None)
    configure(config="a", env="b")
    with pytest.raises(SettingsFileError, match="同時"):
        Settings(_env_file=None)


def test_env_vars_do_not_override_managed_keys(monkeypatch):
    put("config/settings.json", {"search_top_k": 7})
    monkeypatch.setenv("SEARCH_TOP_K", "99")
    monkeypatch.setenv("CHUNK_MAX_CHARS", "300")
    Path(".env").write_text("SEARCH_TOP_K=20\nEMBEDDING_DEVICE=cuda\n")
    s = Settings(_env_file=".env")
    assert (s.search_top_k, s.chunk_max_chars, s.embedding_device) == (7, 1000, "cpu")


def test_env_only_items_export_beats_dotenv(monkeypatch):
    Path(".env").write_text(
        "REDMINE_URL=https://file.example.com\nDATABASE_URL=postgresql://a/file\nREDMINE_API_KEY=fromfile\n"
    )
    s = Settings(_env_file=".env")  # .env alone
    assert (s.redmine_url, s.database_url) == ("https://file.example.com", "postgresql://a/file")
    monkeypatch.setenv("REDMINE_URL", "https://env.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://a/env")
    monkeypatch.setenv("REDMINE_API_KEY", "fromenv")
    s = Settings(_env_file=".env")  # export wins
    assert (s.redmine_url, s.database_url) == ("https://env.example.com", "postgresql://a/env")
    assert s.redmine_api_key.get_secret_value() == "fromenv"


def test_redmine_url_is_not_a_json_item():
    assert "redmine_url" not in settings_keys()


def test_redmine_url_with_userinfo_in_env_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("REDMINE_URL", "https://zed:hunter2@h.example.com")
    with pytest.raises(SettingsFileError, match="REDMINE_URL") as ei:
        Settings(_env_file=None)
    assert "hunter2" not in str(ei.value)
    from redmine_embedding.batch.__main__ import main as batch_main
    assert batch_main(["init-db"]) == 2
    err = capsys.readouterr().err
    assert "REDMINE_URL" in err and "hunter2" not in err and "Traceback" not in err
    Path(".env").write_text("REDMINE_URL=ftp://x\n")
    monkeypatch.delenv("REDMINE_URL")
    with pytest.raises(SettingsFileError, match="REDMINE_URL"):
        Settings(_env_file=".env")


def test_redmine_url_with_at_in_path_allowed(monkeypatch):
    monkeypatch.setenv("REDMINE_URL", "https://h.example.com/a@b")
    assert Settings(_env_file=None).redmine_url == "https://h.example.com/a@b"


@pytest.mark.parametrize("key", ["redmine_url", "redmine_api_key", "database_url"])
def test_secret_key_in_json_is_rejected(key):
    put("config/settings.json", {key: "s3cr3t-value"})
    with pytest.raises(SettingsFileError, match=rf"(?s){key}.*REDMINE_URL.*環境変数") as ei:
        Settings(_env_file=None)
    assert "s3cr3t-value" not in str(ei.value)


@pytest.mark.parametrize(
    "data,key",
    [
        ({"serch_top_k": 1}, "serch_top_k"),
        ({"search_top_k": "many"}, "search_top_k"),
        ({"search_top_k": 0}, "search_top_k"),
        ({"chunk_max_chars": 10**9}, "chunk_max_chars"),
        ({"embedding_device": "gpu"}, "embedding_device"),
        ({"redmine_max_retries": 99}, "redmine_max_retries"),
        ({"reconcile_apply_deletions": "yes"}, "reconcile_apply_deletions"),
    ],
)
def test_invalid_values_name_the_key(data, key):
    configure(config=put("t.json", data))
    with pytest.raises(SettingsFileError) as ei:
        Settings(_env_file=None)
    assert key in str(ei.value) and "t.json" in str(ei.value)


@pytest.mark.parametrize("dev", ["cpu", "cuda", "cuda:1", "mps"])
def test_valid_devices(dev):
    configure(config=put("t.json", {"embedding_device": dev}))
    assert Settings(_env_file=None).embedding_device == dev


def test_json_syntax_error():
    configure(config=put("t.json", "{bad"))
    with pytest.raises(SettingsFileError, match="JSON として不正.*t.json"):
        Settings(_env_file=None)


@pytest.mark.parametrize("raw", ['{"visible_projects_cache_ttl": NaN}', '{"visible_projects_cache_ttl": Infinity}'])
def test_nan_infinity_rejected(raw):
    configure(config=put("t.json", raw))
    with pytest.raises(SettingsFileError, match="visible_projects_cache_ttl.*NaN"):
        Settings(_env_file=None)


def test_info_log_names_file_only(caplog):
    configure(config=put("t.json", {"embedding_model": "secret-model-name"}))
    with caplog.at_level(logging.INFO, logger="redmine_embedding.common.settings_file"):
        Settings(_env_file=None)
    assert "t.json" in caplog.text and "secret-model" not in caplog.text


def test_examples_match_schema_and_defaults():
    ex = json.loads((REPO / "config" / "settings.example.json").read_text("utf-8"))
    settings_file.validate(ex)
    values = {k: v for k, v in ex.items() if k != "$schema"}
    assert set(values) == settings_keys()
    defaults = Settings(_env_file=None)
    schema = settings_file.load_schema()["properties"]
    for k, v in values.items():
        assert getattr(defaults, k) == v, k
        assert schema[k]["default"] == v, k


def test_schema_keys_are_the_managed_parameters():
    assert settings_keys() == JSON_KEYS
    assert settings_keys() <= set(Settings.model_fields)
    assert set(Settings.model_fields) - settings_keys() == {"redmine_url", "redmine_api_key", "database_url"}
    assert len(JSON_KEYS) == 46


def test_cli_args_reach_settings(monkeypatch):
    from redmine_embedding.batch.__main__ import _parser
    from redmine_embedding.mcp_server.__main__ import main as mcp_main

    put("config/settings.prod.json", {"search_top_k": 2})
    args = _parser().parse_args(["--env", "prod", "init-db"])
    config.configure_from_args(args)
    assert config.get_settings().search_top_k == 2
    args = _parser().parse_args(["--config", put("z.json", {"search_top_k": 5}), "sync"])
    config.configure_from_args(args)
    assert config.get_settings().search_top_k == 5
    with pytest.raises(SystemExit):
        _parser().parse_args(["--env", "a", "--config", "b", "init-db"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("REDMINE_URL", "https://r.example")
    started = []

    class FakeServer:
        def run(self, transport, **kw):
            started.append((transport, kw))

    monkeypatch.setattr("redmine_embedding.mcp_server.server.create_server", lambda settings, **kw: FakeServer())
    mcp_main(["--env", "prod", "--port", "9001"])  # reads the settings file, does not start a real server
    assert started == [("streamable-http", {"host": "127.0.0.1", "port": 9001})]
    assert config.get_settings().search_top_k == 2


def test_error_never_echoes_values(caplog):
    key = "sk-LIVE-abcdef0123456789"
    for bad in ({"redmine_url": key}, {"redmine_api_key": key}, {"embedding_device": key}, {"search_top_k": key}, {"chunk_max_chars": 10**12 + 7}):
        configure(config=put("t.json", bad))
        with caplog.at_level(logging.DEBUG), pytest.raises(SettingsFileError) as ei:
            Settings(_env_file=None)
        assert key not in str(ei.value) and "1000000000007" not in str(ei.value)
        assert key not in caplog.text
        assert next(iter(bad)) in str(ei.value)


def test_message_kinds():
    configure(config=put("t.json", {"search_top_k": 0, "chunk_max_chars": "x", "serch": 1, "embedding_device": "gpu"}))
    with pytest.raises(SettingsFileError) as ei:
        Settings(_env_file=None)
    m = str(ei.value)
    assert "下限 1 未満" in m and "型が不正" in m and "未知のキー" in m and "serch" in m and "形式が不正" in m


def test_warns_about_legacy_env_keys_without_values(monkeypatch, caplog):
    monkeypatch.setenv("embedding_model", "leak-model-env")
    monkeypatch.setenv("SEARCH_TOP_K", "leak-99")
    Path(".env").write_text("CHUNK_MAX_CHARS=leak-1\nREDMINE_URL=https://dotenv.example.com\nDATABASE_URL=postgresql://a/b\n")
    with caplog.at_level(logging.WARNING):
        s = Settings(_env_file=".env")
    t = caplog.text
    assert "EMBEDDING_MODEL" in t and "SEARCH_TOP_K" in t and "CHUNK_MAX_CHARS" in t and "無視されます" in t
    assert "leak" not in t and "DATABASE_URL" not in t and "REDMINE_URL" not in t
    assert s.embedding_model == "cl-nagoya/ruri-v3-130m"


def test_no_warning_when_only_env_only_items_in_env(monkeypatch, caplog):
    monkeypatch.setenv("REDMINE_API_KEY", "k")
    monkeypatch.setenv("REDMINE_URL", "https://h.example.com")
    with caplog.at_level(logging.WARNING, logger="redmine_embedding.common.config"):
        Settings(_env_file=None)
    assert [r for r in caplog.records if r.name == "redmine_embedding.common.config"] == []


def test_warns_when_no_file_found(caplog):
    with caplog.at_level(logging.WARNING, logger="redmine_embedding.common.settings_file"):
        Settings(_env_file=None)
    assert "見つかりません" in caplog.text and "settings.json" in caplog.text
    caplog.clear()
    put("config/settings.json", {})
    with caplog.at_level(logging.WARNING, logger="redmine_embedding.common.settings_file"):
        Settings(_env_file=None)
    assert "見つかりません" not in caplog.text


# 環境別ファイル（settings.<env>.json）は配布物に含まれないことがあるので、存在するものだけ検査する。
PRESENT_ENV_FILES = tuple(
    sorted(p.name for p in (REPO / "config").glob("settings.*.json") if p.name != "settings.example.json" and not p.name.endswith(".local.json"))
)


def test_tracked_config_files_are_valid_and_safe():
    names = ["settings.example.json", *PRESENT_ENV_FILES]
    for name in names:
        p = REPO / "config" / name
        data = json.loads(p.read_text("utf-8"))
        settings_file.validate(data, name)  # schema-valid (also rejects env-only keys)
        for k in ("redmine_url", "redmine_api_key", "database_url"):
            assert k not in data, (name, k)
        # never enable deletions by a tracked file (production safety)
        assert data.get("reconcile_apply_deletions", False) is False, name


@pytest.mark.parametrize("name", PRESENT_ENV_FILES)
def test_env_files_load_via_cli_selection(name):
    env = name.split(".")[1]
    configure(config=str(REPO / "config" / name))
    a = Settings(_env_file=None)
    configure(config=str(REPO / "config" / f"settings.{env}.json"))
    assert Settings(_env_file=None).reconcile_apply_deletions is False
    assert a.reconcile_apply_deletions is False


def test_config_errors_exit_2_without_traceback(capsys):
    from redmine_embedding.batch.__main__ import main as batch_main
    from redmine_embedding.mcp_server.__main__ import main as mcp_main

    assert batch_main(["--config", "nope.json", "init-db"]) == 2
    err = capsys.readouterr().err
    assert "nope.json" in err and "Traceback" not in err
    with pytest.raises(SystemExit) as ei:
        mcp_main(["--config", "nope.json"])
    assert ei.value.code == 2
    assert "nope.json" in capsys.readouterr().err


def test_no_warning_for_unrelated_dotenv_keys(caplog):
    Path(".env").write_text("POSTGRES_PASSWORD=pw\nPOSTGRES_USER=u\nREDMINE_URL=https://h.example.com\n")
    with caplog.at_level(logging.WARNING):
        Settings(_env_file=".env")
    assert [r for r in caplog.records if r.name == "redmine_embedding.common.config"] == []


def test_invalid_dotenv_url_fails_even_if_export_is_valid(monkeypatch):
    Path(".env").write_text("REDMINE_URL=ftp://x\n")
    monkeypatch.setenv("REDMINE_URL", "https://ok.example.com")
    with pytest.raises(SettingsFileError, match="REDMINE_URL"):
        Settings(_env_file=".env")
