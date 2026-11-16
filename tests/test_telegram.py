from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import pytest

from slopbot.telegram import TelegramError, TelegramService


async def test_link_url_stores_only_hashed_single_use_code() -> None:
    store = SimpleNamespace(save_telegram_link_code=AsyncMock())
    service = TelegramService(token="token", store=cast(Any, store))
    service.username = "SlopbotAlertsBot"

    url = await service.create_link_url(discord_user_id=42)

    assert url.startswith("https://t.me/SlopbotAlertsBot?start=")
    raw_code = url.rsplit("=", 1)[1]
    saved = store.save_telegram_link_code.await_args.kwargs
    assert saved["discord_user_id"] == 42
    assert saved["code_hash"] != raw_code
    assert len(saved["code_hash"]) == 64


async def test_private_start_message_links_chat_and_confirms() -> None:
    store = SimpleNamespace(consume_telegram_link_code=AsyncMock(return_value=42))
    service = TelegramService(token="token", store=cast(Any, store))
    service.send_message = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await service._handle_update(
        {
            "update_id": 1,
            "message": {
                "text": "/start private-code",
                "chat": {"id": 900, "type": "private"},
            },
        }
    )

    store.consume_telegram_link_code.assert_awaited_once()
    service.send_message.assert_awaited_once_with(
        chat_id=900,
        text="Telegram is linked to your Discord notification preferences.",
    )


async def test_group_start_message_is_ignored() -> None:
    store = SimpleNamespace(consume_telegram_link_code=AsyncMock())
    service = TelegramService(token="token", store=cast(Any, store))
    service.send_message = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await service._handle_update(
        {
            "update_id": 1,
            "message": {
                "text": "/start private-code",
                "chat": {"id": -900, "type": "group"},
            },
        }
    )

    store.consume_telegram_link_code.assert_not_awaited()
    service.send_message.assert_not_awaited()


async def test_invalid_telegram_response_is_normalized() -> None:
    class FakeResponse:
        status = 502

        async def __aenter__(self) -> FakeResponse:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def json(self, *, content_type: Any = None) -> Any:
            raise ValueError("not json")

    class FakeSession:
        def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    service = TelegramService(token="token", store=cast(Any, SimpleNamespace()))
    service._session = cast(Any, FakeSession())

    with pytest.raises(TelegramError, match="invalid response"):
        await service._call("getMe")


async def test_telegram_transport_errors_do_not_expose_token_url() -> None:
    class FakeSession:
        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            raise aiohttp.InvalidURL("https://api.telegram.org/botSUPERSECRET/getMe")

    service = TelegramService(token="SUPERSECRET", store=cast(Any, SimpleNamespace()))
    service._session = cast(Any, FakeSession())

    with pytest.raises(TelegramError, match="Telegram getMe request failed") as captured:
        await service._call("getMe")

    assert "SUPERSECRET" not in str(captured.value)
