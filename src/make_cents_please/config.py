"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CACHE_TTL_SECONDS = 300


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


class Secret:
    """Holds a secret value without ever exposing it through str/repr."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


@dataclass(frozen=True)
class Config:
    token: Secret | None
    default_plan_id: str
    cache_ttl_seconds: int
    log_level: str
    allow_writes: bool = False
    charityrecord_dir: Path | None = None
    # False when YNAB_TOOLS=off: the ynab_* tools are hidden (e.g. when YNAB's own MCP
    # server is used instead). The token is then optional and only used by the
    # Charity Record tools that read YNAB (find_ynab_donations, reconcile).
    ynab_tools: bool = True


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a Config from ``env`` (defaults to os.environ after loading .env)."""
    if env is None:
        load_dotenv()
        env = dict(os.environ)

    ynab_tools = _parse_bool(env.get("YNAB_TOOLS"), "YNAB_TOOLS", default=True)
    charityrecord_dir = _parse_dir(env.get("CHARITYRECORD_DIR"), "CHARITYRECORD_DIR")
    if not ynab_tools and charityrecord_dir is None:
        raise ConfigError(
            "YNAB_TOOLS is off and CHARITYRECORD_DIR is not set, so there are no tools to "
            "serve. Turn YNAB_TOOLS back on or set CHARITYRECORD_DIR."
        )

    token = (env.get("YNAB_ACCESS_TOKEN") or "").strip()
    if not token and ynab_tools:
        raise ConfigError(
            "YNAB_ACCESS_TOKEN is not set. Create a personal access token at "
            "https://app.ynab.com/settings/developer and put it in your .env file "
            "or the Claude Desktop config 'env' block."
        )

    ttl_raw = (env.get("YNAB_CACHE_TTL_SECONDS") or "").strip()
    try:
        ttl = int(ttl_raw) if ttl_raw else DEFAULT_CACHE_TTL_SECONDS
    except ValueError as exc:
        raise ConfigError("YNAB_CACHE_TTL_SECONDS must be a whole number of seconds.") from exc
    if ttl < 0:
        raise ConfigError("YNAB_CACHE_TTL_SECONDS must not be negative.")

    return Config(
        token=Secret(token) if token else None,
        default_plan_id=(env.get("YNAB_DEFAULT_PLAN_ID") or "").strip() or "last-used",
        cache_ttl_seconds=ttl,
        log_level=(env.get("LOG_LEVEL") or "WARNING").strip().upper(),
        allow_writes=_parse_bool(env.get("YNAB_ALLOW_WRITES"), "YNAB_ALLOW_WRITES"),
        charityrecord_dir=charityrecord_dir,
        ynab_tools=ynab_tools,
    )


def _parse_dir(raw: str | None, name: str) -> Path | None:
    value = (raw or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ConfigError(f"{name} must be an existing folder (got {value!r}).")
    return path.resolve()


def _parse_bool(raw: str | None, name: str, default: bool = False) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be on or off / true or false (got {raw!r}).")
