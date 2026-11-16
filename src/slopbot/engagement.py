from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from slopbot.database import GetOnRateLimitError, SubscriptionStore
from slopbot.models import (
    DeliveryMethod,
    GetOnDelivery,
    KeywordNotificationMode,
    KeywordProfile,
    delivery_channels,
)
from slopbot.telegram import TelegramService

LOGGER = logging.getLogger(__name__)
MAX_KEYWORDS = 25
MAX_KEYWORD_LENGTH = 50
MAX_KEYWORD_CHANNELS = 25
MAX_GETON_RECIPIENTS = 10
MAX_GETON_DELAY_MINUTES = 10_080
MAX_GETON_DELIVERY_ATTEMPTS = 5
GETON_RETRY_BASE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivered: bool
    retryable: bool
    detail: str
    receipts: tuple[tuple[str, str, str], ...] = ()


class EngagementCog(commands.Cog):
    def __init__(
        self,
        bot: discord.Client,
        store: SubscriptionStore,
        telegram: TelegramService | None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.telegram = telegram
        self.worker_started_at = int(time.time())
        self.last_geton_delivery_success_at: int | None = None

    async def cog_load(self) -> None:
        self.deliver_scheduled_getons.add_exception_type(Exception)
        self.deliver_scheduled_getons.start()

    async def cog_unload(self) -> None:
        self.deliver_scheduled_getons.cancel()

    @app_commands.command(
        name="configurekeywords",
        description="Configure your private keyword notifications",
    )
    @app_commands.guild_only()
    async def configure_keywords(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        view = await KeywordConfigView.create(
            owner=interaction.user,
            guild=interaction.guild,
            store=self.store,
        )
        await interaction.edit_original_response(embed=view.embed(), view=view)

    @app_commands.command(
        name="geton",
        description="Ask selected server members to get on now or later",
    )
    @app_commands.describe(
        message="Message to send; defaults to '<your name> says get on Discord'",
        minutes="Minutes from now to send it; defaults to immediately",
    )
    @app_commands.guild_only()
    async def geton(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 0, MAX_GETON_DELAY_MINUTES] = 0,
        message: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        view = GetOnView(
            sender=interaction.user,
            guild=interaction.guild,
            store=self.store,
            message=message,
            minutes=minutes,
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(
        name="getonnotifications",
        description="Turn get-on requests on or off for yourself",
    )
    @app_commands.describe(enabled="Whether other members may send you get-on notifications")
    @app_commands.guild_only()
    async def geton_notifications(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if enabled is not None:
            await self.store.set_geton_enabled(
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                enabled=enabled,
            )
        current = await self.store.get_geton_enabled(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
        )
        await interaction.edit_original_response(
            content=(
                f"Get-on notifications are **{'on' if current else 'off'}** for you in this server."
            )
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.content:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        watchers = await self.store.list_keyword_watchers(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
        )
        normalized_content = message.content.casefold()
        for watcher in watchers:
            matched = tuple(
                keyword for keyword in watcher.keywords if keyword.casefold() in normalized_content
            )
            if not matched:
                continue
            member = await _fetch_member(message.guild, watcher.user_id)
            if member is None:
                continue
            cached_member = message.guild.get_member(watcher.user_id)
            recipient_status = cached_member.status if cached_member is not None else member.status
            if not keyword_notification_allowed(
                author_id=message.author.id,
                recipient_id=watcher.user_id,
                notification_mode=watcher.notification_mode,
                recipient_status=recipient_status,
            ):
                continue
            if not message.channel.permissions_for(member).view_channel:
                continue
            await send_keyword_notification(
                bot=self.bot,
                store=self.store,
                telegram=self.telegram,
                recipient_id=watcher.user_id,
                message=message,
                matched=matched,
            )

    @tasks.loop(seconds=5.0)
    async def deliver_scheduled_getons(self) -> None:
        now = int(time.time())
        deliveries = await self.store.list_due_geton_deliveries(now=now)
        for delivery in deliveries:
            enabled = await self.store.get_geton_enabled(
                guild_id=delivery.guild_id,
                user_id=delivery.recipient_id,
            )
            if not enabled:
                await self.store.mark_geton_delivered(
                    request_id=delivery.request_id,
                    user_id=delivery.recipient_id,
                )
                continue
            try:
                outcome = await send_geton_notification(
                    bot=self.bot,
                    store=self.store,
                    telegram=self.telegram,
                    delivery=delivery,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to deliver get-on request %s to user %s",
                    delivery.request_id,
                    delivery.recipient_id,
                )
                await self._retry_geton_delivery(
                    delivery,
                    now=now,
                    detail="unexpected delivery failure",
                )
                continue
            if outcome.delivered or not outcome.retryable:
                if not outcome.delivered:
                    LOGGER.warning(
                        "Permanently failed get-on request %s for user %s: %s",
                        delivery.request_id,
                        delivery.recipient_id,
                        outcome.detail,
                    )
                await self.store.mark_geton_delivered(
                    request_id=delivery.request_id,
                    user_id=delivery.recipient_id,
                )
                continue
            await self._retry_geton_delivery(
                delivery,
                now=now,
                detail=outcome.detail,
            )
        self.last_geton_delivery_success_at = int(time.time())

    async def _retry_geton_delivery(
        self, delivery: GetOnDelivery, *, now: int, detail: str
    ) -> None:
        if delivery.attempt_count + 1 >= MAX_GETON_DELIVERY_ATTEMPTS:
            LOGGER.error(
                "Exhausted retries for get-on request %s to user %s: %s",
                delivery.request_id,
                delivery.recipient_id,
                detail,
            )
            await self.store.mark_geton_delivered(
                request_id=delivery.request_id,
                user_id=delivery.recipient_id,
            )
            return
        retry_delay = min(
            3600,
            GETON_RETRY_BASE_SECONDS * (2**delivery.attempt_count),
        )
        await self.store.retry_geton_delivery(
            request_id=delivery.request_id,
            user_id=delivery.recipient_id,
            next_attempt_at=now + retry_delay,
            error=detail,
        )

    @deliver_scheduled_getons.before_loop
    async def before_deliver_scheduled_getons(self) -> None:
        await self.bot.wait_until_ready()


class KeywordConfigView(discord.ui.View):
    def __init__(
        self,
        *,
        owner: discord.Member,
        guild: discord.Guild,
        store: SubscriptionStore,
        profile: KeywordProfile,
    ) -> None:
        super().__init__(timeout=300)
        self.owner = owner
        self.guild = guild
        self.store = store
        self.profile = profile
        self.add_item(KeywordEnabledButton(self))
        self.add_item(KeywordModeButton(self))
        self.add_item(AddKeywordButton(self))
        if profile.keywords:
            self.add_item(RemoveKeywordSelect(self))
        self.add_item(KeywordChannelSelect(self))

    @classmethod
    async def create(
        cls,
        *,
        owner: discord.Member,
        guild: discord.Guild,
        store: SubscriptionStore,
    ) -> KeywordConfigView:
        profile = await store.get_keyword_profile(guild_id=guild.id, user_id=owner.id)
        return cls(owner=owner, guild=guild, store=store, profile=profile)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message(
            "This keyword panel belongs to someone else. Run `/configurekeywords` yourself.",
            ephemeral=True,
        )
        return False

    def embed(self) -> discord.Embed:
        keywords = "\n".join(f"• `{keyword}`" for keyword in self.profile.keywords)
        channels = []
        for channel_id in self.profile.channel_ids:
            channel = self.guild.get_channel(channel_id)
            channels.append(
                channel.mention if channel is not None else f"Deleted channel ({channel_id})"
            )
        mode = (
            "Always"
            if self.profile.notification_mode is KeywordNotificationMode.ALWAYS
            else "Only while you appear offline"
        )
        return discord.Embed(
            title="Keyword notifications",
            description=(
                f"**Status:** {'On' if self.profile.enabled else 'Off'}\n"
                f"**Notify me:** {mode}\n\n"
                f"**Keywords**\n{keywords or '*None configured*'}\n\n"
                f"**Monitored channels**\n"
                f"{' '.join(channels) if channels else '*None selected*'}"
            ),
            colour=discord.Colour.blurple(),
        )


class KeywordEnabledButton(discord.ui.Button[KeywordConfigView]):
    def __init__(self, view: KeywordConfigView) -> None:
        enabled = view.profile.enabled
        super().__init__(
            label=f"Notifications: {'On' if enabled else 'Off'}",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            row=0,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.panel.store.set_keyword_enabled(
            guild_id=self.panel.guild.id,
            user_id=self.panel.owner.id,
            enabled=not self.panel.profile.enabled,
        )
        await refresh_keyword_panel(interaction, self.panel)


class KeywordModeButton(discord.ui.Button[KeywordConfigView]):
    def __init__(self, view: KeywordConfigView) -> None:
        offline_only = view.profile.notification_mode is KeywordNotificationMode.OFFLINE_ONLY
        super().__init__(
            label="When: Offline only" if offline_only else "When: Always",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        next_mode = (
            KeywordNotificationMode.ALWAYS
            if self.panel.profile.notification_mode is KeywordNotificationMode.OFFLINE_ONLY
            else KeywordNotificationMode.OFFLINE_ONLY
        )
        await self.panel.store.set_keyword_notification_mode(
            guild_id=self.panel.guild.id,
            user_id=self.panel.owner.id,
            notification_mode=next_mode,
        )
        await refresh_keyword_panel(interaction, self.panel)


class AddKeywordButton(discord.ui.Button[KeywordConfigView]):
    def __init__(self, view: KeywordConfigView) -> None:
        super().__init__(
            label="Add keyword",
            style=discord.ButtonStyle.primary,
            disabled=len(view.profile.keywords) >= MAX_KEYWORDS,
            row=1,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddKeywordModal(self.panel))


class AddKeywordModal(discord.ui.Modal, title="Add a keyword"):
    keyword: discord.ui.TextInput[AddKeywordModal] = discord.ui.TextInput(
        label="Word or phrase",
        placeholder="rocket league?",
        min_length=1,
        max_length=MAX_KEYWORD_LENGTH,
    )

    def __init__(self, panel: KeywordConfigView) -> None:
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        keyword: str = str(self.keyword).strip()
        if not keyword:
            await interaction.followup.send("Enter a keyword.", ephemeral=True)
            return
        try:
            created = await self.panel.store.add_keyword(
                guild_id=self.panel.guild.id,
                user_id=self.panel.owner.id,
                keyword=keyword,
            )
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        if not created:
            await interaction.followup.send(
                "That keyword is already in your profile.",
                ephemeral=True,
            )
            return
        await refresh_keyword_panel(interaction, self.panel)


class RemoveKeywordSelect(discord.ui.Select[KeywordConfigView]):
    def __init__(self, view: KeywordConfigView) -> None:
        super().__init__(
            placeholder="Remove a keyword",
            options=[
                discord.SelectOption(label=keyword, value=keyword)
                for keyword in view.profile.keywords
            ],
            min_values=1,
            max_values=1,
            row=2,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.panel.store.remove_keyword(
            guild_id=self.panel.guild.id,
            user_id=self.panel.owner.id,
            keyword=self.values[0],
        )
        await refresh_keyword_panel(interaction, self.panel)


class KeywordChannelSelect(discord.ui.ChannelSelect[KeywordConfigView]):
    def __init__(self, view: KeywordConfigView) -> None:
        defaults = [
            discord.Object(id=channel_id)
            for channel_id in view.profile.channel_ids
            if view.guild.get_channel(channel_id) is not None
        ]
        super().__init__(
            placeholder="Choose monitored text channels",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=defaults,
            min_values=0,
            max_values=MAX_KEYWORD_CHANNELS,
            row=3,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer()
        channel_ids = []
        for selected in self.values:
            channel = self.panel.guild.get_channel(selected.id)
            if not isinstance(channel, discord.TextChannel):
                continue
            if not channel.permissions_for(interaction.user).view_channel:
                await interaction.followup.send(
                    f"You cannot monitor {channel.mention}.",
                    ephemeral=True,
                )
                return
            bot_member = self.panel.guild.me
            if bot_member is None or not channel.permissions_for(bot_member).view_channel:
                await interaction.followup.send(
                    f"Slopbot cannot view {channel.mention}. Give its server role access first.",
                    ephemeral=True,
                )
                return
            channel_ids.append(channel.id)
        await self.panel.store.set_keyword_channels(
            guild_id=self.panel.guild.id,
            user_id=self.panel.owner.id,
            channel_ids=channel_ids,
        )
        await refresh_keyword_panel(interaction, self.panel)


async def refresh_keyword_panel(
    interaction: discord.Interaction,
    panel: KeywordConfigView,
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    refreshed = await KeywordConfigView.create(
        owner=panel.owner,
        guild=panel.guild,
        store=panel.store,
    )
    await interaction.edit_original_response(embed=refreshed.embed(), view=refreshed)


class GetOnView(discord.ui.View):
    def __init__(
        self,
        *,
        sender: discord.Member,
        guild: discord.Guild,
        store: SubscriptionStore,
        message: str | None,
        minutes: int,
    ) -> None:
        super().__init__(timeout=300)
        self.sender = sender
        self.guild = guild
        self.store = store
        self.message = message or f"{sender.display_name} says get on Discord."
        self.minutes = minutes
        self.recipient_ids: tuple[int, ...] = ()
        self.add_item(GetOnRecipientSelect(self))
        self.send_button = GetOnSendButton(self)
        self.add_item(self.send_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.sender.id:
            return True
        await interaction.response.send_message(
            "This get-on request belongs to someone else.",
            ephemeral=True,
        )
        return False

    def embed(self) -> discord.Embed:
        when = "Immediately" if self.minutes == 0 else f"In {self.minutes} minute(s)"
        return discord.Embed(
            title="Send a get-on request",
            description=(
                f"**When:** {when}\n"
                f"**Message:** {self.message}\n\n"
                "Choose one or more server members, then select **Schedule notification**."
            ),
            colour=discord.Colour.blurple(),
        )


class GetOnRecipientSelect(discord.ui.UserSelect[GetOnView]):
    def __init__(self, view: GetOnView) -> None:
        super().__init__(
            placeholder="Choose people to notify",
            min_values=1,
            max_values=MAX_GETON_RECIPIENTS,
            row=0,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_ids = tuple(
            user.id for user in self.values if not user.bot and user.id != self.panel.sender.id
        )
        if not selected_ids:
            await interaction.response.send_message(
                "Choose at least one other non-bot member.",
                ephemeral=True,
            )
            return
        self.panel.recipient_ids = selected_ids
        self.panel.send_button.disabled = False
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)


class GetOnSendButton(discord.ui.Button[GetOnView]):
    def __init__(self, view: GetOnView) -> None:
        super().__init__(
            label="Schedule notification",
            style=discord.ButtonStyle.primary,
            disabled=True,
            row=1,
        )
        self.panel = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.panel.recipient_ids:
            await interaction.response.send_message("Choose recipients first.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            accepted = await self.panel.store.schedule_geton(
                guild_id=self.panel.guild.id,
                sender_id=self.panel.sender.id,
                sender_name=self.panel.sender.display_name,
                message=self.panel.message,
                deliver_at=int(time.time()) + self.panel.minutes * 60,
                recipient_ids=self.panel.recipient_ids,
            )
        except GetOnRateLimitError as error:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Request not scheduled",
                    description=str(error),
                    colour=discord.Colour.orange(),
                ),
                view=self.panel,
            )
            return
        opted_out = len(self.panel.recipient_ids) - len(accepted)
        if not accepted:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="No notifications scheduled",
                    description="All selected members have opted out of get-on requests.",
                    colour=discord.Colour.orange(),
                ),
                view=None,
            )
            return
        when = "now" if self.panel.minutes == 0 else f"in {self.panel.minutes} minute(s)"
        suffix = f" {opted_out} selected member(s) had opted out." if opted_out else ""
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Get-on request scheduled",
                description=f"Notification scheduled for {len(accepted)} member(s) {when}.{suffix}",
                colour=discord.Colour.green(),
            ),
            view=None,
        )


async def _fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def send_to_destinations(
    *,
    bot: discord.Client,
    store: SubscriptionStore,
    telegram: TelegramService | None,
    guild_id: int,
    recipient_id: int,
    discord_embed: discord.Embed,
    telegram_text: str,
    completed: frozenset[str] = frozenset(),
) -> DeliveryOutcome:
    delivery = await store.get_delivery_method(guild_id=guild_id, user_id=recipient_id)
    channels = delivery_channels(delivery)
    if await store.notification_downtime_active(guild_id=guild_id, user_id=recipient_id):
        detail = "Notification downtime is active; this alert was suppressed and not queued"
        quiet_receipts = tuple(
            (channel, "blocked", detail) for channel in sorted(channels) if channel not in completed
        )
        return DeliveryOutcome(
            delivered=bool(completed),
            retryable=False,
            detail=detail,
            receipts=quiet_receipts,
        )
    delivered = False
    retryable = False
    details: list[str] = []
    receipts: list[tuple[str, str, str]] = []
    telegram_requested = "telegram" in channels
    telegram_delivered = "telegram" in completed
    if telegram_requested and not telegram_delivered:
        chat_id = await store.get_telegram_chat_id(discord_user_id=recipient_id)
        if chat_id is None:
            details.append("Telegram is not linked")
        elif telegram is None:
            details.append("Telegram is not configured")
            retryable = True
        else:
            telegram_delivered = await telegram.send_message(chat_id=chat_id, text=telegram_text)
            if telegram_delivered:
                delivered = True
            else:
                details.append("Telegram delivery failed")
                retryable = True

        receipts.append(
            (
                "telegram",
                "delivered" if telegram_delivered else "retry" if retryable else "blocked",
                "; ".join(details) or "Delivered",
            )
        )

    push_requested = "pwa" in channels
    if push_requested and "pwa" not in completed:
        push = getattr(getattr(bot, "web", None), "push", None)
        if push is None:
            details.append("PWA push is not configured")
            retryable = True
            receipts.append(("pwa", "retry", "PWA push is not configured"))
        else:
            outcome = await push.send(
                discord_user_id=recipient_id,
                title=discord_embed.title or "Slopbot notification",
                body=discord_embed.description or telegram_text,
            )
            delivered = delivered or outcome.delivered
            retryable = retryable or outcome.retryable
            if not outcome.delivered:
                details.append(outcome.detail)
            receipts.append(
                (
                    "pwa",
                    "delivered"
                    if outcome.delivered
                    else "retry"
                    if outcome.retryable
                    else "blocked",
                    outcome.detail,
                )
            )

    discord_requested = "discord" in channels
    discord_fallback = delivery is DeliveryMethod.TELEGRAM and not telegram_delivered
    if (discord_requested or discord_fallback) and "discord" not in completed:
        if discord_fallback:
            LOGGER.info(
                "Falling back to Discord for Telegram-only user %s",
                recipient_id,
            )
        try:
            user = bot.get_user(recipient_id) or await bot.fetch_user(recipient_id)
            await user.send(embed=discord_embed)
            delivered = True
            receipts.append(("discord", "delivered", "Delivered"))
        except discord.Forbidden:
            LOGGER.info("User %s has DMs disabled", recipient_id)
            details.append("Discord DMs are disabled")
            receipts.append(("discord", "blocked", "Discord DMs are disabled"))
        except discord.HTTPException:
            LOGGER.exception("Discord rejected notification for user %s", recipient_id)
            details.append("Discord delivery failed")
            retryable = True
            receipts.append(("discord", "retry", "Discord delivery failed"))

    return DeliveryOutcome(
        delivered=delivered or bool(completed),
        retryable=retryable,
        detail="; ".join(details) or "delivered",
        receipts=tuple(receipts),
    )


def keyword_notification_allowed(
    *,
    author_id: int,
    recipient_id: int,
    notification_mode: KeywordNotificationMode,
    recipient_status: discord.Status | None,
) -> bool:
    if author_id == recipient_id:
        return False
    return notification_mode is KeywordNotificationMode.ALWAYS or recipient_status in (
        None,
        discord.Status.offline,
    )


async def send_keyword_notification(
    *,
    bot: discord.Client,
    store: SubscriptionStore,
    telegram: TelegramService | None,
    recipient_id: int,
    message: discord.Message,
    matched: Sequence[str],
) -> None:
    channel = message.channel
    guild = message.guild
    if not isinstance(channel, discord.TextChannel) or guild is None:
        return
    excerpt = discord.utils.escape_mentions(message.content[:1500])
    matched_text = format_keyword_matches(matched)
    embed = discord.Embed(
        title=f"Keyword mentioned in #{channel.name}",
        description=excerpt,
        colour=discord.Colour.blurple(),
        url=message.jump_url,
    )
    embed.add_field(name="Author", value=message.author.display_name)
    embed.add_field(name="Matched", value=matched_text)
    embed.set_footer(text=guild.name)
    telegram_text = (
        f"Keyword alert in #{channel.name}\n"
        f"Server: {guild.name}\n"
        f"Author: {message.author.display_name}\n"
        f"Matched: {', '.join(matched)}\n\n"
        f"{message.content[:3000]}\n\n{message.jump_url}"
    )
    await send_to_destinations(
        bot=bot,
        store=store,
        telegram=telegram,
        guild_id=guild.id,
        recipient_id=recipient_id,
        discord_embed=embed,
        telegram_text=telegram_text[:4096],
    )


def format_keyword_matches(matched: Sequence[str], *, limit: int = 1024) -> str:
    safe_keywords = [
        discord.utils.escape_markdown(discord.utils.escape_mentions(keyword)) for keyword in matched
    ]
    visible: list[str] = []
    for index, keyword in enumerate(safe_keywords):
        remaining_after = len(safe_keywords) - index - 1
        suffix = f", … (+{remaining_after} more)" if remaining_after else ""
        candidate = ", ".join([*visible, keyword])
        if len(candidate) + len(suffix) > limit:
            omitted = len(safe_keywords) - index
            return f"{', '.join(visible)}, … (+{omitted} more)" if visible else keyword[:limit]
        visible.append(keyword)
    return ", ".join(visible) or "Unknown keyword"


async def send_geton_notification(
    *,
    bot: discord.Client,
    store: SubscriptionStore,
    telegram: TelegramService | None,
    delivery: GetOnDelivery,
) -> DeliveryOutcome:
    guild = bot.get_guild(delivery.guild_id)
    if guild is None:
        return DeliveryOutcome(False, True, "Discord server is temporarily unavailable")
    try:
        await guild.fetch_member(delivery.recipient_id)
    except discord.NotFound:
        return DeliveryOutcome(False, False, "Recipient is no longer in the server")
    except (discord.Forbidden, discord.HTTPException):
        return DeliveryOutcome(False, True, "Could not verify recipient server membership")
    guild_name = guild.name
    embed = discord.Embed(
        title="Get on Discord",
        description=delivery.message,
        colour=discord.Colour.green(),
    )
    embed.add_field(name="From", value=delivery.sender_name)
    embed.add_field(name="Server", value=guild_name)
    telegram_text = (
        f"Get on Discord\n\n{delivery.message}\nFrom: {delivery.sender_name}\nServer: {guild_name}"
    )
    return await send_to_destinations(
        bot=bot,
        store=store,
        telegram=telegram,
        guild_id=delivery.guild_id,
        recipient_id=delivery.recipient_id,
        discord_embed=embed,
        telegram_text=telegram_text,
    )
