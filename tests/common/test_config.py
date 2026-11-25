from redmine_embedding.common.config import Settings


def test_env_only_items_come_from_env_and_never_show_in_repr(monkeypatch):
    monkeypatch.setenv("REDMINE_API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    s = Settings(_env_file=None)
    assert s.redmine_api_key.get_secret_value() == "secret"
    assert s.database_url == "postgresql://u:p@h/db"
    assert "secret" not in repr(s)
    assert s.embedding_device == "cpu"


def test_attachment_limits_have_provisional_defaults_and_ignore_env(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.attachment_max_bytes, s.attachment_text_max_chars) == (2 * 1024 * 1024, 200_000)
    monkeypatch.setenv("ATTACHMENT_MAX_BYTES", "1000")
    assert Settings(_env_file=None).attachment_max_bytes == 2 * 1024 * 1024
