from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    telegram_token: str | None
    database_path: Path
    guild_id: int | None
    cleanup_guild_id: int | None
    log_level: int
    rocket_league_url: str | None = None
    rocket_league_token: str | None = None

    @classmethod
    def from_environment(cls) -> Settings:
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("DISCORD_TOKEN is required")

        database_path = Path(os.environ.get("DATABASE_PATH", "data/slopbot.db")).expanduser()
        if not database_path.name:
            raise ConfigurationError("DATABASE_PATH must point to a file")

        guild_id = _optional_positive_int("DISCORD_GUILD_ID")
        cleanup_guild_id = _optional_positive_int("DISCORD_CLEANUP_GUILD_ID")

        log_level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        log_level = logging.getLevelNamesMapping().get(log_level_name)
        if not isinstance(log_level, int):
            raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        rocket_league_url = os.environ.get("ROCKET_LEAGUE_SERVICE_URL", "").strip() or None
        rocket_league_token = os.environ.get("ROCKET_LEAGUE_SERVICE_TOKEN", "").strip() or None
        if (rocket_league_url is None) != (rocket_league_token is None):
            raise ConfigurationError(
                "ROCKET_LEAGUE_SERVICE_URL and ROCKET_LEAGUE_SERVICE_TOKEN must be set together"
            )
        if rocket_league_url is not None:
            _validate_service_url(rocket_league_url)
            if rocket_league_token is None or len(rocket_league_token) < 32:
                raise ConfigurationError(
                    "ROCKET_LEAGUE_SERVICE_TOKEN must contain at least 32 characters"
                )

        return cls(
            token=token,
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None,
            database_path=database_path,
            guild_id=guild_id,
            cleanup_guild_id=cleanup_guild_id,
            log_level=log_level,
            rocket_league_url=rocket_league_url,
            rocket_league_token=rocket_league_token,
        )


def _optional_positive_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def _validate_service_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("ROCKET_LEAGUE_SERVICE_URL must be an HTTP(S) URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "ROCKET_LEAGUE_SERVICE_URL must not contain credentials or a query"
        )
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError(
            "ROCKET_LEAGUE_SERVICE_URL must use HTTPS unless it points to localhost"
        )
