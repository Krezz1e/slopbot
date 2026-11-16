from __future__ import annotations

import logging
from pathlib import Path

from slopbot.bot import create_bot
from slopbot.config import Settings


def test_bot_requests_keyword_message_intents(tmp_path: Path) -> None:
    bot = create_bot(
        Settings(
            token="secret",
            telegram_token=None,
            database_path=tmp_path / "slopbot.db",
            guild_id=None,
            cleanup_guild_id=None,
            log_level=logging.INFO,
        )
    )

    assert bot.intents.guild_messages is True
    assert bot.intents.members is True
    assert bot.intents.message_content is True
    assert bot.intents.presences is True
