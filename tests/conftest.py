import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path_factory):
    """Keep tests independent of a local config/ directory, .env and secrets in the environment."""
    from redmine_embedding.common import config

    for k in ("REDMINE_API_KEY", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    config.configure()
    yield
    config.configure()
