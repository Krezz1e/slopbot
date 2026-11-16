from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord

from slopbot.models import DeliveryMethod, Subscription, Transition
from slopbot.notifications import (
    VoiceAlertService,
    VoiceOccupancyTracker,
    format_member_list,
    format_telegram_alert,
)


def fake_member(
    *,
    user_id: int = 999,
    bot: bool = False,
    name: str = "Member",
    activities: tuple[Any, ...] = (),
) -> Any:
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        display_name=name,
        activities=activities,
    )


def fake_channel(channel_id: int, *, member_count: int, guild_id: int = 10) -> Any:
    guild = SimpleNamespace(id=guild_id, name="Weekend Gamers", me=fake_member(user_id=1, bot=True))
    return SimpleNamespace(
        id=channel_id,
        name=f"Voice {channel_id}",
        mention=f"<#{channel_id}>",
        guild=guild,
        members=[fake_member() for _ in range(member_count)],
    )


def grant_access(channel: Any, member: Any) -> None:
    channel.guild.fetch_member = AsyncMock(return_value=member)
    channel.permissions_for = lambda _member: SimpleNamespace(view_channel=True)


def fake_store(
    subscription: Subscription, *, delivery: DeliveryMethod = DeliveryMethod.DISCORD
) -> Any:
    return SimpleNamespace(
        list_for_channel=AsyncMock(return_value=[subscription]),
        get_delivery_method=AsyncMock(return_value=delivery),
        get_telegram_chat_id=AsyncMock(return_value=1234),
    )


async def test_occupancy_tracker_uses_ordered_event_counts() -> None:
    tracker = VoiceOccupancyTracker()
    channel = fake_channel(1, member_count=3)

    joined = await tracker.apply(
        member=fake_member(),
        before=SimpleNamespace(channel=None),
        after=SimpleNamespace(channel=channel),
    )
    left = await tracker.apply(
        member=fake_member(),
        before=SimpleNamespace(channel=channel),
        after=SimpleNamespace(channel=None),
    )

    assert [(item.previous_count, item.current_count) for item in joined] == [(2, 3)]
    assert [(item.previous_count, item.current_count) for item in left] == [(3, 2)]


async def test_occupancy_tracker_handles_moves_and_ignores_bots() -> None:
    tracker = VoiceOccupancyTracker()
    old_channel = fake_channel(1, member_count=3)
    new_channel = fake_channel(2, member_count=1)
    guild = SimpleNamespace(voice_channels=[old_channel, new_channel], stage_channels=[])
    await tracker.rebuild([guild])

    transitions = await tracker.apply(
        member=fake_member(),
        before=SimpleNamespace(channel=old_channel),
        after=SimpleNamespace(channel=new_channel),
    )
    bot_transitions = await tracker.apply(
        member=fake_member(bot=True),
        before=SimpleNamespace(channel=old_channel),
        after=SimpleNamespace(channel=new_channel),
    )

    assert [(item.channel.id, item.previous_count, item.current_count) for item in transitions] == [
        (1, 3, 2),
        (2, 1, 2),
    ]
    assert bot_transitions == []


async def test_alert_service_sends_only_when_threshold_is_crossed() -> None:
    subscription = Subscription(
        id=1,
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )
    store = fake_store(subscription)
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _user_id: user)
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=3)
    grant_access(channel, user)

    await service.handle_channel_transition(
        channel=channel,
        previous_count=2,
        current_count=3,
    )
    await service.handle_channel_transition(
        channel=channel,
        previous_count=3,
        current_count=4,
    )

    user.send.assert_awaited_once()
    embed = user.send.await_args.kwargs["embed"]
    assert embed.title == "Voice 30 is active"
    assert "3 people" in embed.description
    assert embed.fields[0].name == "Who's in the channel"
    assert "Member" in embed.fields[0].value


async def test_departure_alert_uses_subscriber_threshold() -> None:
    subscription = Subscription(
        id=1,
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=4,
        notify_on_reach=False,
        notify_on_drop=True,
    )
    store = fake_store(subscription)
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _user_id: user)
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=3)
    grant_access(channel, user)

    await service.handle_channel_transition(
        channel=channel,
        previous_count=4,
        current_count=3,
    )

    embed = user.send.await_args.kwargs["embed"]
    assert embed.title == "Voice 30 quieted down"
    assert "4-person" in embed.description


async def test_no_alert_while_subscriber_is_in_channel() -> None:
    subscription = Subscription(
        id=1,
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=True,
    )
    store = fake_store(subscription)
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _user_id: user)
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=0)
    channel.members = [fake_member(user_id=20), fake_member(user_id=21), fake_member(user_id=22)]
    grant_access(channel, user)

    await service.handle_channel_transition(
        channel=channel,
        previous_count=2,
        current_count=3,
        actor_user_id=22,
        actor_joined=True,
    )
    await service.handle_channel_transition(
        channel=channel,
        previous_count=3,
        current_count=2,
        actor_user_id=22,
        actor_joined=False,
    )

    user.send.assert_not_awaited()


async def test_subscriber_joining_does_not_trigger_their_own_alert() -> None:
    subscription = Subscription(
        id=1,
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )
    store = fake_store(subscription)
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_user=lambda _user_id: user)
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=0)
    channel.members = [
        fake_member(user_id=20),
        fake_member(user_id=21),
        fake_member(user_id=22),
    ]
    grant_access(channel, user)

    await service.handle_channel_transition(
        channel=channel,
        previous_count=2,
        current_count=3,
        actor_user_id=20,
        actor_joined=True,
    )

    user.send.assert_not_awaited()


async def test_alert_is_removed_when_subscriber_cannot_view_channel() -> None:
    subscription = Subscription(
        id=1,
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )
    store = SimpleNamespace(
        list_for_channel=AsyncMock(return_value=[subscription]),
        delete_for_user=AsyncMock(return_value=True),
    )
    member = SimpleNamespace(id=20, send=AsyncMock())
    bot = SimpleNamespace()
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=3)
    channel.guild.fetch_member = AsyncMock(return_value=member)
    channel.permissions_for = lambda candidate: SimpleNamespace(
        view_channel=candidate is channel.guild.me
    )

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_not_awaited()
    store.delete_for_user.assert_awaited_once_with(
        subscription_id=1,
        guild_id=10,
        user_id=20,
    )


async def test_alert_is_skipped_when_bot_cannot_view_channel() -> None:
    subscription = Subscription(1, 10, 20, 30, 3, True, False)
    store = fake_store(subscription)
    member = SimpleNamespace(id=20, send=AsyncMock())
    service = VoiceAlertService(cast(Any, SimpleNamespace()), cast(Any, store))
    channel = fake_channel(30, member_count=3)
    channel.guild.fetch_member = AsyncMock(return_value=member)
    channel.permissions_for = lambda candidate: SimpleNamespace(
        view_channel=candidate is not channel.guild.me
    )

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_not_awaited()
    channel.guild.fetch_member.assert_not_awaited()


def test_member_list_uses_display_names_and_excludes_bots() -> None:
    channel = fake_channel(30, member_count=0)
    channel.members = [
        fake_member(user_id=20, name="Krezzle"),
        fake_member(user_id=21, name="Alice_*"),
        fake_member(user_id=22, name="Helper", bot=True),
    ]

    result = format_member_list(channel)

    assert result == "• Krezzle\n• Alice\\_\\*"
    assert "Helper" not in result


def test_member_list_includes_playing_game_when_requested() -> None:
    channel = fake_channel(30, member_count=0)
    channel.members = [
        fake_member(
            name="Krezzle",
            activities=(SimpleNamespace(type=discord.ActivityType.playing, name="Helldivers 2"),),
        ),
        fake_member(name="Alice"),
    ]

    reached_result = format_member_list(channel, include_games=True)
    departure_result = format_member_list(channel)

    assert reached_result == "• Krezzle — Playing Helldivers 2\n• Alice"
    assert departure_result == "• Krezzle\n• Alice"


def test_member_list_is_truncated_to_embed_field_limit() -> None:
    channel = fake_channel(30, member_count=0)
    channel.members = [
        fake_member(user_id=user_id, name=f"Member {user_id}") for user_id in range(100)
    ]

    result = format_member_list(channel, limit=100)

    assert len(result) <= 100
    assert "more" in result


async def test_both_delivery_sends_discord_and_telegram() -> None:
    subscription = Subscription(1, 10, 20, 30, 3, True, False)
    store = fake_store(subscription, delivery=DeliveryMethod.BOTH)
    member = SimpleNamespace(send=AsyncMock())
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = VoiceAlertService(
        cast(Any, SimpleNamespace()), cast(Any, store), telegram=cast(Any, telegram)
    )
    channel = fake_channel(30, member_count=3)
    grant_access(channel, member)

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_awaited_once()
    telegram.send_message.assert_awaited_once()
    assert telegram.send_message.await_args.kwargs["chat_id"] == 1234
    assert "Voice 30 is active" in telegram.send_message.await_args.kwargs["text"]


async def test_voice_alert_sends_pwa_push() -> None:
    subscription = Subscription(1, 10, 20, 30, 3, True, False)
    store = fake_store(subscription, delivery=DeliveryMethod.PWA)
    member = SimpleNamespace(send=AsyncMock())
    push = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(web=SimpleNamespace(push=push))
    service = VoiceAlertService(cast(Any, bot), cast(Any, store))
    channel = fake_channel(30, member_count=3)
    grant_access(channel, member)

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_not_awaited()
    push.send.assert_awaited_once()
    assert push.send.await_args.kwargs["discord_user_id"] == 20
    assert push.send.await_args.kwargs["title"] == "Voice 30 is active"


async def test_telegram_only_delivery_does_not_send_discord_dm() -> None:
    subscription = Subscription(1, 10, 20, 30, 3, True, False)
    store = fake_store(subscription, delivery=DeliveryMethod.TELEGRAM)
    member = SimpleNamespace(send=AsyncMock())
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = VoiceAlertService(
        cast(Any, SimpleNamespace()), cast(Any, store), telegram=cast(Any, telegram)
    )
    channel = fake_channel(30, member_count=3)
    grant_access(channel, member)

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_not_awaited()
    telegram.send_message.assert_awaited_once()


async def test_telegram_only_delivery_falls_back_to_discord() -> None:
    subscription = Subscription(1, 10, 20, 30, 3, True, False)
    store = fake_store(subscription, delivery=DeliveryMethod.TELEGRAM)
    member = SimpleNamespace(send=AsyncMock())
    telegram = SimpleNamespace(send_message=AsyncMock(return_value=False))
    service = VoiceAlertService(
        cast(Any, SimpleNamespace()), cast(Any, store), telegram=cast(Any, telegram)
    )
    channel = fake_channel(30, member_count=3)
    grant_access(channel, member)

    await service.handle_channel_transition(channel=channel, previous_count=2, current_count=3)

    member.send.assert_awaited_once()


def test_telegram_reached_message_includes_playing_game() -> None:
    channel = fake_channel(30, member_count=0)
    channel.members = [
        fake_member(
            name="Big Al",
            activities=(SimpleNamespace(type=discord.ActivityType.playing, name="Fortnite"),),
        )
    ]

    message = format_telegram_alert(
        channel=channel,
        transition=Transition.REACHED,
        threshold=3,
        current_count=3,
    )

    assert "• Big Al — Playing Fortnite" in message


def test_telegram_departure_message_uses_server_nicknames() -> None:
    channel = fake_channel(30, member_count=0)
    channel.members = [fake_member(user_id=21, name="Big Al")]

    message = format_telegram_alert(
        channel=channel,
        transition=Transition.DROPPED,
        threshold=3,
        current_count=1,
    )

    assert "Voice 30 quieted down" in message
    assert "Big Al" in message
