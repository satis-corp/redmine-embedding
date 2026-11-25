"""Settings kept in a JSON file and validated with JSON Schema.

Settings live in the file, except the items kept out of git (``REDMINE_URL``,
``REDMINE_API_KEY``, ``DATABASE_URL``), which are read from the environment
(export > ``.env``) only.

Which file is read (first match wins):
  1. ``--config <path>`` / ``--env <name>`` (``config/settings.<name>.json``)
  2. ``./config/active`` (one line: a path or an environment name)
  3. ``./config/settings.json`` (may be absent: code defaults are used)
1 and 2 must exist when given; only 3 may be missing.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("config")
DEFAULT_PATH = CONFIG_DIR / "settings.json"
ACTIVE_PATH = CONFIG_DIR / "active"
# Not stored in git: only from environment variables (export > .env).
ENV_ONLY_KEYS = ("redmine_url", "redmine_api_key", "database_url")
_URL_OK = re.compile(r"^(https?://[^\s@/]+(/\S*)?)?$")
_ENV_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class SettingsFileError(ValueError):
    """Invalid settings: bad / missing settings file, or a bad env-only value (REDMINE_URL)."""


@dataclass(frozen=True)
class Selection:
    """What the command line asked for (both None = not given)."""

    config: str | None = None
    env: str | None = None


def load_schema() -> dict[str, Any]:
    text = resources.files("redmine_embedding.common").joinpath("settings.schema.json").read_text("utf-8")
    return json.loads(text)


def settings_keys() -> set[str]:
    return set(load_schema()["properties"]) - {"$schema"}


def env_path(name: str) -> Path:
    if not _ENV_NAME.match(name):
        raise SettingsFileError(f"環境名が不正です: {name!r}（英数字・_ . - のみ）")
    return CONFIG_DIR / f"settings.{name}.json"


def _read_active() -> str | None:
    if not ACTIVE_PATH.is_file():
        return None
    for line in ACTIVE_PATH.read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def resolve_path(sel: Selection | None = None) -> tuple[Path, bool, str]:
    """Return (path, explicit, how). explicit means it must exist."""
    sel = sel or Selection()
    if sel.config and sel.env:
        raise SettingsFileError("--config と --env は同時に指定できません")
    if sel.config:
        return Path(sel.config), True, "--config"
    if sel.env:
        return env_path(sel.env), True, "--env"
    active = _read_active()
    if active:
        if "/" in active or active.endswith(".json"):
            # a relative path is relative to the directory of the active file
            return ACTIVE_PATH.parent / active, True, f"{ACTIVE_PATH}"
        return env_path(active), True, f"{ACTIVE_PATH}"
    return DEFAULT_PATH, False, "default"


def _describe(e) -> str:
    """Kind of violation only; never echo the input value (it may hold a credential)."""
    v = e.validator
    if v == "additionalProperties":
        known = set(e.schema.get("properties", {}))
        names = ", ".join(sorted(k for k in e.instance if k not in known))
        return f"未知のキーです（{names}）。typo か、対象外の項目です"
    if v == "type":
        return f"型が不正です（期待: {e.validator_value}）"
    if v == "minimum":
        return f"下限 {e.validator_value} 未満です"
    if v == "maximum":
        return f"上限 {e.validator_value} を超えています"
    if v == "pattern":
        return "形式が不正です（embedding_device は cpu / cuda / cuda:N / mps）"
    if v == "minLength":
        return "空にできません"
    return f"制約 {v} に違反しています"


def check_redmine_url(url: str, source: str) -> None:
    """Quick check of REDMINE_URL (from env / .env). Never echoes the value."""
    if not _URL_OK.match(url):
        raise SettingsFileError(
            f"REDMINE_URL の形式が不正です ({source}): http(s):// で始め、user:pass@ などの認証情報を含めないでください"
            "（API キーは REDMINE_API_KEY で指定します）"
        )


def validate(data: Any, source: str = "<settings>") -> None:
    if isinstance(data, dict):
        secrets = [k for k in ENV_ONLY_KEYS if k in data]
        if secrets:
            raise SettingsFileError(
                f"設定ファイルに書けないキーがあります ({source}): {', '.join('$.' + k for k in secrets)}\n"
                "  git に載せない項目（REDMINE_URL / REDMINE_API_KEY / DATABASE_URL）は環境変数（export か .env）で指定してください。"
            )
    errors = sorted(
        Draft202012Validator(load_schema()).iter_errors(data),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if not errors:
        return
    lines = []
    for e in errors:
        loc = "$" + "".join(f".{p}" for p in e.absolute_path)
        lines.append(f"  {loc}: {_describe(e)}")
    raise SettingsFileError(f"設定ファイルが不正です ({source}):\n" + "\n".join(lines))


def load_settings_file(sel: Selection | None = None) -> dict[str, Any]:
    """Load and validate the selected file; {} when only the default file is absent."""
    path, explicit, how = resolve_path(sel)
    if not path.is_file():
        if explicit:
            raise SettingsFileError(f"設定ファイルがありません ({how} で指定): {path}")
        logger.warning(
            "設定ファイルが見つかりません。コード内の既定値で動きます（探したパス: %s）。"
            "--config / --env / config/active で指定できます",
            path.resolve(),
        )
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise SettingsFileError(
            f"設定ファイルが JSON として不正です ({path}): 行 {e.lineno} 列 {e.colno}: {e.msg}"
        ) from e
    except OSError as e:
        raise SettingsFileError(f"設定ファイルを読めません ({path}): {e.strerror}") from e
    if isinstance(data, dict):
        bad = [k for k, v in data.items() if isinstance(v, float) and not math.isfinite(v)]
        if bad:
            raise SettingsFileError(
                f"設定ファイルが不正です ({path}):\n"
                + "\n".join(f"  $.{k}: NaN / Infinity は使えません（有限の数値のみ）" for k in bad)
            )
    validate(data, str(path))
    data.pop("$schema", None)
    logger.info("settings file: %s (%s)", path, how)
    return data
