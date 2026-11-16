from __future__ import annotations

import logging

import pytest

from slopbot.config import ConfigurationError, Settings


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("DATABASE_PATH", "data/test.db")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("DISCORD_CLEANUP_GUILD_ID", "456")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_TOKEN", "s" * 32)

    settings = Settings.from_environment()

    assert settings.token == "secret"
    assert settings.telegram_token == "telegram-secret"
    assert settings.database_path.as_posix() == "data/test.db"
    assert settings.guild_id == 123
    assert settings.cleanup_guild_id == 456
    assert settings.log_level == logging.WARNING
    assert settings.rocket_league_url == "http://127.0.0.1:8765"
    assert settings.rocket_league_token == "s" * 32


def test_missing_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN"):
        Settings.from_environment()


def test_telegram_token_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    assert Settings.from_environment().telegram_token is None


@pytest.mark.parametrize("guild_id", ["hello", "0", "-1"])
def test_invalid_guild_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    guild_id: str,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", guild_id)
    with pytest.raises(ConfigurationError, match="DISCORD_GUILD_ID"):
        Settings.from_environment()


@pytest.mark.parametrize("guild_id", ["hello", "0", "-1"])
def test_invalid_cleanup_guild_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    guild_id: str,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("DISCORD_CLEANUP_GUILD_ID", guild_id)
    with pytest.raises(ConfigurationError, match="DISCORD_CLEANUP_GUILD_ID"):
        Settings.from_environment()


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
        Settings.from_environment()


def test_rocket_league_service_settings_must_be_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("ROCKET_LEAGUE_SERVICE_TOKEN", raising=False)

    with pytest.raises(ConfigurationError, match="must be set together"):
        Settings.from_environment()


def test_plain_http_rocket_league_service_must_be_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_URL", "http://example.com:8765")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_TOKEN", "s" * 32)

    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_environment()


def test_rocket_league_service_token_must_be_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ROCKET_LEAGUE_SERVICE_TOKEN", "too-short")

    with pytest.raises(ConfigurationError, match="at least 32"):
        Settings.from_environment()
