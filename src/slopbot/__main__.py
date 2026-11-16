from __future__ import annotations

import logging
import sys

from slopbot.bot import create_bot
from slopbot.config import ConfigurationError, Settings


def configure_logging(level: int) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    configure_logging(settings.log_level)
    bot = create_bot(settings)
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
