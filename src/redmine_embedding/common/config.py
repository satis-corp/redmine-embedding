"""Settings shared by the batch and the MCP server.

Values come from a JSON settings file (see ``settings_file``) except the items
kept out of git (REDMINE_URL, REDMINE_API_KEY, DATABASE_URL), which come from
environment variables (export > ``.env``).
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .settings_file import ENV_ONLY_KEYS, Selection, check_redmine_url, load_settings_file, settings_keys

logger = logging.getLogger(__name__)

_selection = Selection()


def configure(config: str | None = None, env: str | None = None) -> None:
    """Choose the settings file (from --config / --env) and drop the cached Settings."""
    global _selection
    _selection = Selection(config, env)
    get_settings.cache_clear()


def get_selection() -> Selection:
    """The settings file selection chosen by configure() (--config / --env)."""
    return _selection


def add_config_arguments(parser) -> None:
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--config", metavar="PATH", help="settings JSON to use (default: config/active, then config/settings.json)")
    g.add_argument("--env", metavar="NAME", help="shorthand for --config config/settings.NAME.json")


def configure_from_args(args) -> None:
    configure(args.config, args.env)


class _SettingsFileSource(PydanticBaseSettingsSource):
    """Every item except the env-only ones comes from the selected JSON file."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = load_settings_file(_selection)

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict:
        return dict(self._data)


class _SecretsOnly(PydanticBaseSettingsSource):
    """Wrap the env / .env source and keep only the env-only keys (others are ignored)."""

    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource, label: str) -> None:
        super().__init__(settings_cls)
        self._inner = inner
        self._label = label

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return None, field_name, False

    def __call__(self) -> dict:
        found = self._inner()
        ignored = sorted(k.upper() for k in found if k in settings_keys())
        if ignored:
            logger.warning(
                "%s に設定ファイルへ移した項目があります（%s）。無視されます。設定ファイルで指定してください",
                self._label,
                ", ".join(ignored),
            )
        if isinstance(found.get("redmine_url"), str):
            check_redmine_url(found["redmine_url"], self._label)
        return {k: v for k, v in found.items() if k in ENV_ONLY_KEYS}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        # Settings-file items: settings file > code defaults (environment variables are
        # NOT read). Env-only items (REDMINE_URL, REDMINE_API_KEY, DATABASE_URL): env > .env.
        return (
            init_settings,
            _SecretsOnly(settings_cls, env_settings, "環境変数"),
            _SecretsOnly(settings_cls, dotenv_settings, ".env"),
            _SettingsFileSource(settings_cls),
        )

    # --- Redmine -----------------------------------------------------------
    # From REDMINE_URL (env / .env), not the settings file: kept out of git.
    redmine_url: str = ""
    # Only the sync batch uses this (dedicated admin account). The MCP server
    # never stores a key: it passes the caller's OAuth2 token through.
    redmine_api_key: SecretStr | None = None
    redmine_request_interval: float = 0.0  # seconds to sleep between requests
    redmine_timeout: float = 30.0
    redmine_max_retries: int = 3

    # --- PostgreSQL + pgvector --------------------------------------------
    database_url: str = ""

    # --- Embedding ---------------------------------------------------------
    # Ruri v3 size (30m / 70m / 130m / 310m) is still to be decided.
    embedding_model: str = "cl-nagoya/ruri-v3-130m"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16

    # --- Sync batch --------------------------------------------------------
    # Chunk size and sync interval are still undecided; these are
    # starting values to be tuned during evaluation.
    chunk_max_chars: int = 1000
    # Re-read this far before the last sync so updates that landed while the
    # previous run was working are not missed. Upserts are idempotent.
    sync_overlap_minutes: int = 10
    # Deleting chunks of archived projects / deleted records stays off until a
    # person has read a dry-run report; then set this to true.
    reconcile_apply_deletions: bool = False
    # 暫定: attachment text ingestion limits. Set in the
    # settings file (attachment_max_bytes / attachment_text_max_chars). Files over the byte limit
    # are not downloaded (only their file name / description is indexed).
    attachment_max_bytes: int = 2 * 1024 * 1024
    attachment_text_max_chars: int = 200_000
    # Embed this many chunks at a time (bigger = faster on CPU, more memory).
    embed_flush_size: int = 64

    # --- MCP search (prefilter) -------------------------------------------
    # 暫定: candidates fetched from pgvector before the
    # postfilter drops invisible hits. Set in the settings file (search_top_k etc.).
    search_top_k: int = 50
    # How long a user's visible-project list is reused (seconds), and how many
    # users are kept (oldest dropped first).
    visible_projects_cache_ttl: float = 60.0
    visible_projects_cache_max_entries: int = 1000

    # --- MCP search (postfilter) -----------------------------------
    # 暫定: when fewer than `limit` hits survive the
    # visibility check, search again with top_k doubled (50 -> 100 -> 200),
    # at most `search_max_rounds` rounds in total.
    search_max_rounds: int = 3
    # Redmine API calls the visibility check may make per search; hits left
    # unchecked past this budget are dropped (fail closed).
    search_check_max_requests: int = 100
    # Threads used for the visibility checks.
    search_check_workers: int = 8
    # Default number of hits returned to the caller.
    search_default_limit: int = 10
    # 暫定: seconds a settled visibility verdict is reused
    # across searches (0 = no cache, the default), and how many verdicts are
    # kept (oldest dropped first). Larger TTL = fewer Redmine calls but later
    # reflection of permission changes.
    search_check_cache_ttl: float = 0.0
    search_check_cache_max_entries: int = 10000

    # --- MCP server -------------------------------------------------
    # 暫定: all keys below. Set in the settings file.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    # One search answers within this many seconds (deadline; no new Redmine
    # request starts after it, leftovers are reported as unverified).
    search_timeout_seconds: float = 20.0
    search_max_limit: int = 50
    search_excerpt_chars: int = 300
    # Hide credential-looking values (PW: xxx, tokens, ...) in the excerpt only.
    search_excerpt_mask_credentials: bool = True
    mcp_db_pool_max: int = 4
    # Queries embedded at the same time (CPU bound).
    mcp_embed_concurrency: int = 2
    # Redmine calls made for the caller's token (kept shorter than the batch's).
    mcp_redmine_timeout: float = 10.0
    mcp_redmine_max_retries: int = 1

    # --- MCP OAuth2 -------------------------------------------------
    # 暫定: all keys below except the introspect cache TTL
    # (decided: 30 s). Set in the settings file. Empty ``mcp_public_base_url`` =
    # OAuth2 off (the Authorization header token is passed through, development).
    # The upstream OAuth app's client id / secret and the encryption key are NOT
    # settings: they come from environment variables (see mcp_server/auth/config.py).
    mcp_public_base_url: str = ""
    mcp_public_path: str = "/mcp-search"
    mcp_oauth_scopes: list[str] = ["view_issues", "view_wiki_pages", "view_news", "view_time_entries", "view_files", "view_project"]
    mcp_oauth_introspect_cache_ttl: float = 30.0
    mcp_oauth_access_token_ttl: int = 3600
    mcp_oauth_refresh_token_ttl: int = 30 * 24 * 3600
    mcp_oauth_client_retention_days: int = 90
    # Deleting expired rows stays off (dry-run: counts are logged) until a person has read the counts.
    mcp_oauth_cleanup_apply: bool = False
    mcp_oauth_cleanup_interval: int = 3600
    # Caps against unauthenticated row growth (/register, /authorize), see mcp_server/auth/http.py.
    mcp_oauth_max_clients: int = 1000
    mcp_oauth_max_pending_requests: int = 1000
    mcp_oauth_max_clients_per_ip: int = 50
    mcp_oauth_max_pending_per_ip: int = 20
    # Decided: 30 s. A used-up refresh token shown again within this time keeps the grant.
    mcp_oauth_refresh_grace_seconds: int = 30
    mcp_oauth_rate_limit_per_minute: int = 20

    @model_validator(mode="after")
    def _check_search_limits(self) -> Settings:
        if self.search_default_limit > self.search_max_limit:
            raise ValueError("search_default_limit は search_max_limit 以下にしてください")
        return self

    @model_validator(mode="after")
    def _check_public_url(self) -> Settings:
        base = self.mcp_public_base_url
        if base and not re.fullmatch(r"https?://[^\s/@?#]+/?", base):
            raise ValueError("mcp_public_base_url は http(s)://host[:port] の形式（パス・認証情報なし）にしてください")
        path = self.mcp_public_path
        if not re.fullmatch(r"/[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*", path):
            raise ValueError("mcp_public_path は /xxx の形式（末尾に / を付けない）にしてください")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
