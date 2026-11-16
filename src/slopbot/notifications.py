from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import discord

from slopbot.database import SubscriptionStore
from slopbot.models import (
    DeliveryMethod,
    Subscription,
    Transition,
    delivery_channels,
    detect_transition,
)

LOGGER = logging.getLogger(__name__)


class TelegramSender(Protocol):
    async def send_message(self, *, chat_id: int, text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ChannelTransition:
    channel: discord.VoiceChannel | discord.StageChannel
    previous_count: int
    current_count: int
    actor_user_id: int
    actor_joined: bool


class VoiceOccupancyTracker:
    """Tracks counts from ordered voice events instead of relying on mutable channel caches."""

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def rebuild(self, guilds: Sequence[discord.Guild]) -> None:
        counts: dict[int, int] = {}
        for guild in guilds:
            for channel in guild.voice_channels:
                counts[channel.id] = human_member_count(channel)
            for stage_channel in guild.stage_channels:
                counts[stage_channel.id] = human_member_count(stage_channel)
        async with self._lock:
            self._counts = counts

    async def apply(
        self,
        *,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> list[ChannelTransition]:
        if member.bot or before.channel == after.channel:
            return []

        transitions: list[ChannelTransition] = []
        async with self._lock:
            if before.channel is not None:
                previous_count = self._counts.get(
                    before.channel.id,
                    human_member_count(before.channel) + 1,
                )
                current_count = max(0, previous_count - 1)
                self._counts[before.channel.id] = current_count
                transitions.append(
                    ChannelTransition(
                        before.channel, previous_count, current_count, member.id, False
                    )
                )

            if after.channel is not None:
                previous_count = self._counts.get(
                    after.channel.id,
                    max(0, human_member_count(after.channel) - 1),
                )
                current_count = previous_count + 1
                self._counts[after.channel.id] = current_count
                transitions.append(
                    ChannelTransition(after.channel, previous_count, current_count, member.id, True)
                )
        return transitions

    async def forget(self, channel_id: int) -> None:
        async with self._lock:
            self._counts.pop(channel_id, None)


class VoiceAlertService:
    def __init__(
        self,
        bot: discord.Client,
        store: SubscriptionStore,
        *,
        telegram: TelegramSender | None = None,
    ) -> None:
        self._bot = bot
        self._store = store
        self._telegram = telegram
        self._channel_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle_channel_transition(
        self,
        *,
        channel: discord.VoiceChannel | discord.StageChannel,
        previous_count: int,
        current_count: int,
        actor_user_id: int | None = None,
        actor_joined: bool | None = None,
    ) -> None:
        if previous_count == current_count:
            return

        async with self._channel_locks[channel.id]:
            subscriptions = await self._store.list_for_channel(
                guild_id=channel.guild.id,
                channel_id=channel.id,
            )
            for subscription in subscriptions:
                subscriber_is_present = any(
                    member.id == subscription.user_id for member in channel.members
                )
                if subscriber_is_present:
                    continue
                current_for_subscriber = current_count
                subscriber_was_present: bool = False
                if actor_user_id == subscription.user_id and actor_joined is not None:
                    subscriber_was_present = not actor_joined
                previous_for_subscriber = previous_count - int(subscriber_was_present)
                transition = detect_transition(
                    previous_count=previous_for_subscriber,
                    current_count=current_for_subscriber,
                    threshold=subscription.threshold,
                    notify_on_reach=subscription.notify_on_reach,
                    notify_on_drop=subscription.notify_on_drop,
                )
                if transition is not None:
                    await self._send_alert(
                        channel=channel,
                        subscription=subscription,
                        transition=transition,
                        current_count=current_for_subscriber,
                    )

    async def _send_alert(
        self,
        *,
        channel: discord.VoiceChannel | discord.StageChannel,
        subscription: Subscription,
        transition: Transition,
        current_count: int,
    ) -> None:
        bot_member = channel.guild.me
        if bot_member is None or not channel.permissions_for(bot_member).view_channel:
            LOGGER.warning(
                "Slopbot cannot view channel %s; skipped subscription %s",
                channel.id,
                subscription.id,
            )
            return
        try:
            # Fetch fresh roles instead of trusting a possibly stale member cache.
            member = await channel.guild.fetch_member(subscription.user_id)
        except discord.NotFound:
            await self._delete_inaccessible(subscription)
            return
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Could not verify channel access for subscriber %s; skipped subscription %s",
                subscription.user_id,
                subscription.id,
            )
            return
        if not channel.permissions_for(member).view_channel:
            await self._delete_inaccessible(subscription)
            return
        if any(channel_member.id == subscription.user_id for channel_member in channel.members):
            return
        if await self._store.notification_downtime_active(
            guild_id=subscription.guild_id,
            user_id=subscription.user_id,
        ):
            LOGGER.info(
                "Suppressed voice alert for user %s during notification downtime",
                subscription.user_id,
            )
            return

        if transition is Transition.REACHED:
            title = f"{channel.name} is active"
            description = (
                f"**{current_count} people** are now in {channel.mention} "
                f"on **{channel.guild.name}**."
            )
            colour = discord.Colour.green()
        else:
            title = f"{channel.name} quieted down"
            description = (
                f"{channel.mention} is now below your **{subscription.threshold}-person** "
                f"threshold with **{current_count} people** remaining."
            )
            colour = discord.Colour.orange()

        embed = discord.Embed(title=title, description=description, colour=colour)
        embed.add_field(
            name="Who's in the channel",
            value=format_member_list(
                channel,
                include_games=transition is Transition.REACHED,
            ),
            inline=False,
        )
        embed.set_footer(text="Change this alert with /alerts")
        delivery = await self._store.get_delivery_method(
            guild_id=subscription.guild_id,
            user_id=subscription.user_id,
        )
        channels = delivery_channels(delivery)
        discord_sent = False
        if "discord" in channels:
            try:
                await member.send(embed=embed)
                discord_sent = True
            except discord.Forbidden:
                LOGGER.info(
                    "Subscriber %s has DMs disabled; skipped subscription %s",
                    subscription.user_id,
                    subscription.id,
                )
            except discord.HTTPException:
                LOGGER.exception("Discord rejected DM for subscription %s", subscription.id)

        telegram_sent = False
        if "telegram" in channels:
            chat_id = await self._store.get_telegram_chat_id(discord_user_id=subscription.user_id)
            if chat_id is None or self._telegram is None:
                LOGGER.info(
                    "Telegram is not linked or configured for subscriber %s",
                    subscription.user_id,
                )
            else:
                telegram_sent = await self._telegram.send_message(
                    chat_id=chat_id,
                    text=format_telegram_alert(
                        channel=channel,
                        transition=transition,
                        threshold=subscription.threshold,
                        current_count=current_count,
                    ),
                )
        if "pwa" in channels:
            push = getattr(getattr(self._bot, "web", None), "push", None)
            if push is None:
                LOGGER.warning("PWA push is not configured for subscriber %s", subscription.user_id)
            else:
                await push.send(
                    discord_user_id=subscription.user_id,
                    title=title,
                    body=format_telegram_alert(
                        channel=channel,
                        transition=transition,
                        threshold=subscription.threshold,
                        current_count=current_count,
                    ),
                )
        if delivery is DeliveryMethod.TELEGRAM and not telegram_sent and not discord_sent:
            LOGGER.info(
                "Falling back to Discord for Telegram-only subscriber %s",
                subscription.user_id,
            )
            try:
                await member.send(embed=embed)
            except discord.Forbidden:
                LOGGER.info(
                    "Subscriber %s has DMs disabled; fallback failed for subscription %s",
                    subscription.user_id,
                    subscription.id,
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Discord rejected fallback DM for subscription %s",
                    subscription.id,
                )

    async def _delete_inaccessible(self, subscription: Subscription) -> None:
        await self._store.delete_for_user(
            subscription_id=subscription.id,
            guild_id=subscription.guild_id,
            user_id=subscription.user_id,
        )
        LOGGER.info(
            "Removed subscription %s because subscriber %s can no longer view its channel",
            subscription.id,
            subscription.user_id,
        )


def format_member_list(
    channel: discord.VoiceChannel | discord.StageChannel,
    *,
    include_games: bool = False,
    limit: int = 1024,
) -> str:
    """Build a safe post-event roster that fits in one Discord embed field."""
    members = [member for member in channel.members if not member.bot]
    if not members:
        return "No one is currently in the channel."

    lines: list[str] = []
    for index, member in enumerate(members):
        name = discord.utils.escape_markdown(discord.utils.escape_mentions(member.display_name))
        game = _playing_game(member) if include_games else None
        game_suffix = ""
        if game is not None:
            safe_game = discord.utils.escape_markdown(discord.utils.escape_mentions(game))
            game_suffix = f" — Playing {safe_game}"
        line = f"• {name}{game_suffix}"
        remaining = len(members) - index
        suffix = f"\n• …and {remaining} more" if remaining else ""
        candidate = "\n".join([*lines, line])
        if len(candidate) + len(suffix) > limit:
            if not lines:
                return f"• {name[: max(1, limit - 1)]}"
            visible_names = "\n".join(lines)
            return f"{visible_names}\n• …and {remaining} more"
        lines.append(line)
    return "\n".join(lines)


def format_telegram_alert(
    *,
    channel: discord.VoiceChannel | discord.StageChannel,
    transition: Transition,
    threshold: int,
    current_count: int,
) -> str:
    if transition is Transition.REACHED:
        summary = f"{current_count} people are now in {channel.name}."
        title = f"{channel.name} is active"
    else:
        summary = (
            f"{channel.name} is now below your {threshold}-person threshold "
            f"with {current_count} people remaining."
        )
        title = f"{channel.name} quieted down"
    members = [member for member in channel.members if not member.bot]
    roster_lines = []
    for member in members:
        game = _playing_game(member) if transition is Transition.REACHED else None
        game_suffix = f" — Playing {game}" if game is not None else ""
        roster_lines.append(f"• {member.display_name}{game_suffix}")
    roster = "\n".join(roster_lines) or "No one is currently in the channel."
    message = (
        f"{title}\n\n{summary}\nServer: {channel.guild.name}\n\nWho's in the channel\n{roster}"
    )
    if len(message) <= 4096:
        return message
    return f"{message[:4050]}\n…member list shortened"


def _playing_game(member: discord.Member) -> str | None:
    for activity in getattr(member, "activities", ()):
        if activity.type is discord.ActivityType.playing and activity.name:
            return str(activity.name)
    return None


def human_member_count(channel: discord.VoiceChannel | discord.StageChannel) -> int:
    return sum(not member.bot for member in channel.members)
