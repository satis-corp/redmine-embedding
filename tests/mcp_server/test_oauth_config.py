"""OAuth2 configuration: settings, environment secrets, host / path switching."""

import pytest

pytest.importorskip("mcp")

from oauth_fakes import CLIENT_SECRET, ENC_KEY, ENV, settings  # noqa: E402

from redmine_embedding.common.config import Settings  # noqa: E402
from redmine_embedding.mcp_server.auth.config import OAuthConfig, OAuthConfigError  # noqa: E402
from redmine_embedding.mcp_server.auth.crypto import DecryptError, TokenCipher  # noqa: E402


def test_defaults_are_off_and_provisional_values():
    s = Settings(_env_file=None)
    assert s.mcp_public_base_url == "" and s.mcp_public_path == "/mcp-search"
    assert s.mcp_oauth_introspect_cache_ttl == 30.0
    assert s.mcp_oauth_cleanup_apply is False  # deletion is dry-run by default


def test_urls_follow_the_settings():
    cfg = OAuthConfig.from_settings(settings(mcp_public_base_url="https://other.example:8443/", mcp_public_path="/x/y"), ENV)
    assert cfg.issuer_url == cfg.resource_url == "https://other.example:8443/x/y"
    assert cfg.callback_url == "https://other.example:8443/x/y/oauth/callback"
    assert cfg.public_netloc == "other.example:8443"


@pytest.mark.parametrize("base", ["https://h/path", "ftp://h", "https://u:p@h", "h.example", "https://h?x=1"])
def test_bad_public_base_url(base):
    with pytest.raises(ValueError):
        settings(mcp_public_base_url=base)


@pytest.mark.parametrize("path", ["mcp", "/mcp/", "/", "/a b", "/a?x"])
def test_bad_public_path(path):
    with pytest.raises(ValueError):
        settings(mcp_public_path=path)


def test_missing_env_names_the_variables_not_values():
    env = dict(ENV)
    del env["REDMINE_OAUTH_CLIENT_SECRET"]
    with pytest.raises(OAuthConfigError) as e:
        OAuthConfig.from_settings(settings(), env)
    assert "REDMINE_OAUTH_CLIENT_SECRET" in str(e.value) and CLIENT_SECRET not in str(e.value)


def test_requires_public_url_and_redmine_url():
    with pytest.raises(OAuthConfigError):
        OAuthConfig.from_settings(Settings(_env_file=None, redmine_url="https://r"), ENV)
    with pytest.raises(OAuthConfigError):
        OAuthConfig.from_settings(Settings(_env_file=None, mcp_public_base_url="https://h"), ENV)


def test_repr_hides_secrets():
    cfg = OAuthConfig.from_settings(settings(), ENV)
    assert CLIENT_SECRET not in repr(cfg) and ENC_KEY not in repr(cfg)


def test_cipher_roundtrip_rotation_and_errors():
    from cryptography.fernet import Fernet

    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old = TokenCipher(k1).encrypt("secret")
    assert "secret" not in old
    both = TokenCipher(f"{k2},{k1}")  # rotation: new key first, old still decrypts
    assert both.decrypt(old) == "secret"
    with pytest.raises(DecryptError):
        TokenCipher(k2).decrypt(old)
    with pytest.raises(OAuthConfigError) as e:
        TokenCipher("not-a-key-SECRETVALUE")
    assert "SECRETVALUE" not in str(e.value)


def test_main_in_oauth_mode_exits_2_naming_missing_env(monkeypatch, capsys, tmp_path):
    import json

    from redmine_embedding.mcp_server.__main__ import main

    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps({"mcp_public_base_url": "https://mcp.test"}))
    monkeypatch.setenv("REDMINE_URL", "https://redmine.test")
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg)])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "REDMINE_OAUTH_CLIENT_ID" in err and "MCP_OAUTH_ENCRYPTION_KEY" in err


def test_token_source_in_oauth_mode_ignores_the_raw_header():
    from types import SimpleNamespace

    from redmine_embedding.mcp_server.auth.token import make_token_source

    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"authorization": "Bearer OUR-TOKEN"})))
    assert make_token_source()(ctx) == "OUR-TOKEN"  # pass-through mode
    assert make_token_source(oauth=True)(ctx) is None  # our token must never be sent to Redmine


def test_main_in_oauth_mode_rejects_dev_token(monkeypatch, capsys, tmp_path):
    import json

    from redmine_embedding.mcp_server.__main__ import main

    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps({"mcp_public_base_url": "https://mcp.test"}))
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg), "--allow-dev-token"])
    assert e.value.code == 2


def test_main_without_database_url_exits_2_without_traceback(monkeypatch, capsys, tmp_path):
    import json

    from redmine_embedding.common import config
    from redmine_embedding.common.config import Settings
    from redmine_embedding.mcp_server.__main__ import main

    cfg = tmp_path / "s.json"
    cfg.write_text(json.dumps({}))
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    monkeypatch.setenv("REDMINE_URL", "https://redmine.test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg)])
    config.configure()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err and "Traceback" not in err
