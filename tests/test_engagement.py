from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from slopbot.engagement import (
    EngagementCog,
    format_keyword_matches,
    keyword_notification_allowed,
    send_geton_notification,
    send_to_destinations,
)
from slopbot.models import DeliveryMethod, GetOnDelivery, KeywordNotificationMode


@pytest.mark.parametrize(
    "mode,status",
    [
        (KeywordNotificationMode.ALWAYS, discord.Status.online),
        (KeywordNotificationMode.ALWAYS, discord.Status.offline),
        (KeywordNotificationMode.OFFLINE_ONLY, discord.Status.offline),
    ],
)
def test_keyword_alert_never_notifies_message_author(
    mode: KeywordNotificationMode,
    status: discord.Status,
) -> None:
    assert not keyword_notification_allowed(
        author_id=10,
        recipient_id=10,
        notification_mode=mode,
        recipient_status=status,
    )


def test_always_mode_notifies_another_online_user() -> None:
    assert keyword_notification_allowed(
        author_id=10,
        recipient_id=20,
        notification_mode=KeywordNotificationMode.ALWAYS,
        recipient_status=discord.Status.online,
    )


@pytest.mark.parametrize(
    "status",
    [discord.Status.online, discord.Status.idle, discord.Status.dnd],
)
def test_offline_only_mode_skips_visible_users(status: discord.Status) -> None:
    assert not keyword_notification_allowed(
        author_id=10,
        recipient_id=20,
        notification_mode=KeywordNotificationMode.OFFLINE_ONLY,
        recipient_status=status,
    )


def test_offline_only_mode_notifies_offline_user() -> None:
    assert keyword_notification_allowed(
        author_id=10,
        recipient_id=20,
        notification_mode=KeywordNotificationMode.OFFLINE_ONLY,
        recipient_status=discord.Status.offline,
    )


def test_offline_only_mode_treats_unknown_cache_status_as_offline() -> None:
    assert keyword_notification_allowed(
        author_id=10,
        recipient_id=20,
        notification_mode=KeywordNotificationMode.OFFLINE_ONLY,
        recipient_status=None,
    )


def test_keyword_matches_fit_discord_embed_field_limit() -> None:
    formatted = format_keyword_matches(tuple("x" * 50 + str(index) for index in range(25)))

    assert len(formatted) <= 1024
    assert "more" in formatted


async def test_unexpected_geton_failure_is_deferred_and_counted(monkeypatch: Any) -> None:
    delivery = GetOnDelivery(1, 10, 20, "Krezzle", "Get on.", attempt_count=0)
    store = SimpleNamespace(
        list_due_geton_deliveries=AsyncMock(return_value=[delivery]),
        get_geton_enabled=AsyncMock(return_value=True),
        retry_geton_delivery=AsyncMock(),
        mark_geton_delivered=AsyncMock(),
    )
    monkeypatch.setattr(
        "slopbot.engagement.send_geton_notification",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    )
    cog = EngagementCog(cast(Any, SimpleNamespace()), cast(Any, store), None)

    await cog.deliver_scheduled_getons.coro(cog)

    store.retry_geton_delivery.assert_awaited_once()
    assert store.retry_geton_delivery.await_args.kwargs["next_attempt_at"] > 0
    assert store.retry_geton_delivery.await_args.kwargs["error"] == "unexpected delivery failure"
    store.mark_geton_delivered.assert_not_awaited()


async def test_telegram_only_notification_falls_back_to_discord() -> None:
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _user_id: user)
    store = SimpleNamespace(
        get_delivery_method=AsyncMock(return_value=DeliveryMethod.TELEGRAM),
        get_telegram_chat_id=AsyncMock(return_value=123),
    )
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=False))

    outcome = await send_to_destinations(
        bot=cast(Any, bot),
        store=cast(Any, store),
        telegram=cast(Any, telegram),
        guild_id=1,
        recipient_id=2,
        discord_embed=discord.Embed(title="Test"),
        telegram_text="Test",
    )

    assert outcome.delivered is True
    user.send.assert_awaited_once()


async def test_pwa_notification_uses_linked_web_push() -> None:
    push = SimpleNamespace(
        send=AsyncMock(
            return_value=SimpleNamespace(delivered=True, retryable=False, detail="Delivered")
        )
    )
    bot = SimpleNamespace(web=SimpleNamespace(push=push), get_user=lambda _: None)
    store = SimpleNamespace(get_delivery_method=AsyncMock(return_value=DeliveryMethod.PWA))

    outcome = await send_to_destinations(
        bot=cast(Any, bot),
        store=cast(Any, store),
        telegram=None,
        guild_id=1,
        recipient_id=2,
        discord_embed=discord.Embed(title="Test", description="It works"),
        telegram_text="Test",
    )

    assert outcome.delivered is True
    push.send.assert_awaited_once_with(discord_user_id=2, title="Test", body="It works")
    assert ("pwa", "delivered", "Delivered") in outcome.receipts


async def test_summary_retries_only_failed_destinations() -> None:
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _: user)
    store = SimpleNamespace(
        get_delivery_method=AsyncMock(return_value=DeliveryMethod.BOTH),
        get_telegram_chat_id=AsyncMock(return_value=123),
    )
    telegram = SimpleNamespace(send_message=AsyncMock(side_effect=[False, True]))
    args = dict(
        bot=cast(Any, bot),
        store=cast(Any, store),
        telegram=cast(Any, telegram),
        guild_id=1,
        recipient_id=2,
        discord_embed=discord.Embed(title="Test"),
        telegram_text="Test",
    )
    first = await send_to_destinations(**args)
    assert first.delivered and first.retryable
    assert ("discord", "delivered", "Delivered") in first.receipts
    second = await send_to_destinations(**args, completed=frozenset({"discord"}))
    assert second.delivered and not second.retryable
    user.send.assert_awaited_once()
    assert telegram.send_message.await_count == 2


async def test_geton_does_not_deliver_after_recipient_leaves(monkeypatch: Any) -> None:
    class FakeNotFound(Exception):
        pass

    monkeypatch.setattr("slopbot.engagement.discord.NotFound", FakeNotFound)
    guild = SimpleNamespace(
        name="Weekend Gamers",
        fetch_member=AsyncMock(side_effect=FakeNotFound()),
    )
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
    store = SimpleNamespace()

    outcome = await send_geton_notification(
        bot=cast(Any, bot),
        store=cast(Any, store),
        telegram=None,
        delivery=GetOnDelivery(1, 10, 20, "Krezzle", "Get on."),
    )

    assert outcome.delivered is False
    assert outcome.retryable is False
    assert "no longer" in outcome.detail
