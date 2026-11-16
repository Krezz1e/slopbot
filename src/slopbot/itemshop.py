from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from slopbot.database import ItemShopWatchLimitError, SubscriptionStore
from slopbot.engagement import send_to_destinations
from slopbot.models import ItemShopChange, ItemShopDelivery, ItemShopItem, ItemShopWatch
from slopbot.rocketleague import RocketLeagueService, RocketLeagueServiceError
from slopbot.telegram import TelegramService
from slopbot.visibility import (
    RESPONSE_VISIBILITY_CHOICES,
    defer_result_response,
)

if TYPE_CHECKING:
    from slopbot.bot import SlopBot

LOGGER = logging.getLogger(__name__)
SHOP_POLL_MINUTES = 10
SHOP_HISTORY_DAYS = 30
MAX_SHOP_DELIVERY_ATTEMPTS = 5
SHOP_RETRY_BASE_SECONDS = 60


class ItemShopCog(commands.Cog):
    itemshop = app_commands.Group(
        name="itemshop", description="Browse the Rocket League item shop and manage watches"
    )

    def __init__(
        self,
        bot: SlopBot,
        store: SubscriptionStore,
        service: RocketLeagueService | None,
        telegram: TelegramService | None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.service = service
        self.telegram = telegram
        self.worker_started_at = int(time.time())
        self.last_shop_poll_success_at: int | None = None
        self.last_shop_delivery_success_at: int | None = None

    async def cog_load(self) -> None:
        if self.service is not None:
            self.poll_item_shop.add_exception_type(Exception)
            self.deliver_item_shop_changes.add_exception_type(Exception)
            self.poll_item_shop.start()
            self.deliver_item_shop_changes.start()

    async def cog_unload(self) -> None:
        self.poll_item_shop.cancel()
        self.deliver_item_shop_changes.cancel()

    @itemshop.command(name="current", description="Show items currently in the Rocket League shop")
    @app_commands.describe(
        search="Optional text matched against names, shops, descriptions, and attributes",
        max_price="Only show items with at least one price at or below this amount",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(visibility=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.checks.cooldown(1, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def current(
        self,
        interaction: discord.Interaction,
        search: app_commands.Range[str, 1, 100] | None = None,
        max_price: app_commands.Range[int, 0, 1_000_000] | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="itemshop_current",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /itemshop current in this channel."
            )
            return
        if self.service is None:
            await interaction.edit_original_response(
                content="Rocket League shop access is not configured on this bot."
            )
            return
        cached = False
        try:
            items = await self.service.shop()
            if not items:
                saved_items = await self.store.list_item_shop_snapshot()
                if saved_items:
                    items = saved_items
                    cached = True
        except RocketLeagueServiceError:
            LOGGER.warning("Live Rocket League shop request failed", exc_info=True)
            items = await self.store.list_item_shop_snapshot()
            cached = True
        matched = filter_items(items, query=search or "", max_price=max_price)
        embed = item_list_embed(
            matched,
            title="Rocket League item shop",
            empty="No current shop items match those filters.",
        )
        if cached:
            embed.set_footer(
                text="Live shop unavailable; showing Slopbot's latest saved catalogue."
            )
        await interaction.edit_original_response(embed=embed)

    @itemshop.command(name="watch", description="Notify you when matching shop items change")
    @app_commands.describe(
        search=(
            "Text matched against names, shops, descriptions, and attributes; omit for all items"
        ),
        max_price="Only notify when an item has a price at or below this amount",
    )
    @app_commands.guild_only()
    async def watch(
        self,
        interaction: discord.Interaction,
        search: app_commands.Range[str, 1, 100] | None = None,
        max_price: app_commands.Range[int, 0, 1_000_000] | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if self.service is None:
            await interaction.response.send_message(
                "Rocket League shop alerts are not configured on this bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            watch = await self.store.save_item_shop_watch(
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                query=search or "",
                max_price=max_price,
            )
        except ItemShopWatchLimitError as error:
            await interaction.edit_original_response(content=str(error))
            return
        await interaction.edit_original_response(
            content=f"Saved item-shop watch **#{watch.id}**: {watch_description(watch)}. "
            "Matching additions, removals, and updates will use your `/notifications` delivery "
            "setting for this server."
        )

    @itemshop.command(name="watches", description="List your item-shop notification watches")
    @app_commands.guild_only()
    async def watches(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        saved = await self.store.list_item_shop_watches(
            guild_id=interaction.guild.id, user_id=interaction.user.id
        )
        if not saved:
            message = (
                "You have no item-shop watches in this server. Use `/itemshop watch` to add one."
            )
        else:
            message = "\n".join(f"**#{watch.id}** — {watch_description(watch)}" for watch in saved)
        await interaction.edit_original_response(content=message)

    @itemshop.command(name="unwatch", description="Delete one of your item-shop watches")
    @app_commands.describe(watch_id="Watch number shown by /itemshop watches")
    @app_commands.guild_only()
    async def unwatch(
        self, interaction: discord.Interaction, watch_id: app_commands.Range[int, 1]
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await self.store.delete_item_shop_watch(
            watch_id=watch_id,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
        )
        await interaction.edit_original_response(
            content=f"Deleted item-shop watch **#{watch_id}**."
            if removed
            else f"You do not have item-shop watch **#{watch_id}** in this server."
        )

    @itemshop.command(name="changes", description="Show recently observed item-shop changes")
    @app_commands.describe(
        days="How many days to report; defaults to 1 and supports up to 30",
        search="Optional text matched against names, shops, descriptions, and attributes",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(visibility=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def changes(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 30] = 1,
        search: app_commands.Range[str, 1, 100] | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="itemshop_changes",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /itemshop changes in this channel."
            )
            return
        changes = await self.store.list_item_shop_changes(
            since=int(time.time()) - days * 86400,
            query=search or "",
            limit=20,
        )
        embed = change_list_embed(changes, days=days)
        await interaction.edit_original_response(embed=embed)

    @tasks.loop(minutes=SHOP_POLL_MINUTES)
    async def poll_item_shop(self) -> None:
        if self.service is None:
            return
        now = int(time.time())
        poll_succeeded = False
        try:
            items = await self.service.shop()
        except RocketLeagueServiceError:
            LOGGER.warning("Rocket League item-shop poll failed", exc_info=True)
        else:
            if items:
                changes = await self.store.replace_item_shop_snapshot(items=items, observed_at=now)
                if changes:
                    LOGGER.info("Recorded %s Rocket League item-shop changes", changes)
            else:
                LOGGER.warning("Ignoring an empty Rocket League item-shop response")
            poll_succeeded = True
        await self.store.prune_item_shop_changes(before=now - SHOP_HISTORY_DAYS * 86400)
        if poll_succeeded:
            self.last_shop_poll_success_at = int(time.time())

    @poll_item_shop.before_loop
    async def before_polling_item_shop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def deliver_item_shop_changes(self) -> None:
        await self._deliver_due_changes(int(time.time()))
        self.last_shop_delivery_success_at = int(time.time())

    @deliver_item_shop_changes.before_loop
    async def before_delivering_item_shop_changes(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_due_changes(self, now: int) -> None:
        deliveries = await self.store.list_due_item_shop_deliveries(now=now)
        for delivery in deliveries:
            await self._deliver_change(delivery, now)

    async def _deliver_change(self, delivery: ItemShopDelivery, now: int) -> None:
        guild = self.bot.get_guild(delivery.guild_id)
        if guild is None:
            await self._retry_delivery(delivery, now, "server temporarily unavailable")
            return
        member = guild.get_member(delivery.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(delivery.user_id)
            except discord.NotFound:
                member = None
            except (discord.Forbidden, discord.HTTPException):
                await self._retry_delivery(delivery, now, "could not verify server membership")
                return
        if member is None:
            await self.store.finish_item_shop_delivery(
                change_id=delivery.change.id,
                guild_id=delivery.guild_id,
                user_id=delivery.user_id,
                detail="user left server",
            )
            return
        embed = change_embed(delivery.change, guild.name)
        outcome = await send_to_destinations(
            bot=self.bot,
            store=self.store,
            telegram=self.telegram,
            guild_id=delivery.guild_id,
            recipient_id=delivery.user_id,
            discord_embed=embed,
            telegram_text=change_text(delivery.change, guild.name),
        )
        if outcome.delivered or not outcome.retryable:
            await self.store.finish_item_shop_delivery(
                change_id=delivery.change.id,
                guild_id=delivery.guild_id,
                user_id=delivery.user_id,
                detail=outcome.detail,
            )
        else:
            await self._retry_delivery(delivery, now, outcome.detail)

    async def _retry_delivery(self, delivery: ItemShopDelivery, now: int, detail: str) -> None:
        if delivery.attempt_count + 1 >= MAX_SHOP_DELIVERY_ATTEMPTS:
            await self.store.finish_item_shop_delivery(
                change_id=delivery.change.id,
                guild_id=delivery.guild_id,
                user_id=delivery.user_id,
                detail=f"gave up after {MAX_SHOP_DELIVERY_ATTEMPTS} attempts: {detail}",
            )
            return
        delay = min(SHOP_RETRY_BASE_SECONDS * (2**delivery.attempt_count), 3600)
        await self.store.retry_item_shop_delivery(
            change_id=delivery.change.id,
            guild_id=delivery.guild_id,
            user_id=delivery.user_id,
            next_attempt_at=now + delay,
            detail=detail,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Try that again in {error.retry_after:.0f} seconds."
        else:
            LOGGER.error(
                "Item-shop command failed", exc_info=(type(error), error, error.__traceback__)
            )
            message = "That item-shop command could not be completed. Try again shortly."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def filter_items(
    items: list[ItemShopItem], *, query: str, max_price: int | None
) -> list[ItemShopItem]:
    normalized_query = " ".join(query.split()).casefold()
    result = [
        item
        for item in items
        if (
            not normalized_query
            or all(term in item.searchable_text for term in normalized_query.split())
        )
        and (max_price is None or any(price.amount <= max_price for price in item.prices))
    ]
    return sorted(result, key=lambda item: (item.shop_name.casefold(), item.title.casefold()))


def watch_description(watch: ItemShopWatch) -> str:
    target = (
        f"items matching “{discord.utils.escape_markdown(watch.query)}”"
        if watch.query
        else "all items"
    )
    if watch.max_price is not None:
        target += f" priced at **{watch.max_price:,}** or less"
    return target


def price_text(item: ItemShopItem) -> str:
    if not item.prices:
        return "Price unavailable"
    return " / ".join(
        f"{price.amount:,} (currency {price.currency_id})" for price in item.prices[:4]
    )


def item_list_embed(items: list[ItemShopItem], *, title: str, empty: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blue())
    if not items:
        embed.description = empty
        return embed
    lines: list[str] = []
    for item in items[:10]:
        expiry = f" • ends <t:{item.ends_at}:R>" if item.ends_at is not None else ""
        lines.append(
            f"**{discord.utils.escape_markdown(item.title)}** — {price_text(item)}\n"
            f"{discord.utils.escape_markdown(item.shop_name)}{expiry}"
        )
    embed.description = "\n\n".join(lines)[:4096]
    if len(items) > 10:
        embed.set_footer(
            text=f"Showing 10 of {len(items)} matching items. Narrow the search to see more."
        )
    return embed


def change_list_embed(changes: list[ItemShopChange], *, days: int) -> discord.Embed:
    embed = discord.Embed(
        title="Rocket League item-shop changes",
        description=f"Changes Slopbot observed in the last {days} day{'s' if days != 1 else ''}.",
        color=discord.Color.blurple(),
    )
    if not changes:
        embed.add_field(
            name="No observed changes",
            value="Slopbot establishes a quiet baseline on its first successful shop poll.",
            inline=False,
        )
        return embed
    verbs = {"added": "Added", "updated": "Updated", "removed": "Removed"}
    embed.add_field(
        name="Latest changes",
        value="\n".join(
            f"**{verbs[change.change_type]}:** "
            f"{discord.utils.escape_markdown(change.item.title)} "
            f"(<t:{change.observed_at}:R>)"
            for change in changes
        )[:1024],
        inline=False,
    )
    if len(changes) == 20:
        embed.set_footer(text="Showing the 20 most recent matching changes.")
    return embed


def change_embed(change: ItemShopChange, guild_name: str) -> discord.Embed:
    verbs = {"added": "added", "updated": "updated", "removed": "removed"}
    colors = {
        "added": discord.Color.green(),
        "updated": discord.Color.gold(),
        "removed": discord.Color.red(),
    }
    item = change.item
    embed = discord.Embed(
        title=f"Item shop {verbs[change.change_type]}: {item.title}",
        description=item.description[:1000] or None,
        color=colors[change.change_type],
    )
    embed.add_field(name="Shop", value=item.shop_name or item.shop_type)
    embed.add_field(name="Price", value=price_text(item))
    if item.ends_at is not None:
        embed.add_field(name="Availability", value=f"Ends <t:{item.ends_at}:R>")
    if item.attributes:
        embed.add_field(
            name="Attributes",
            value=", ".join(f"{key}: {value}" for key, value in item.attributes)[:1024],
            inline=False,
        )
    if item.image_url.startswith("https://"):
        embed.set_thumbnail(url=item.image_url)
    embed.set_footer(text=f"Watch configured in {guild_name}")
    return embed


def change_text(change: ItemShopChange, guild_name: str) -> str:
    item = change.item
    verb = {"added": "Added", "updated": "Updated", "removed": "Removed"}[change.change_type]
    expiry = (
        "\nEnds: " + datetime.fromtimestamp(item.ends_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if item.ends_at is not None
        else ""
    )
    attributes = (
        "\nAttributes: " + ", ".join(f"{key}: {value}" for key, value in item.attributes)
        if item.attributes
        else ""
    )
    return (
        f"Rocket League item shop — {verb}\n\n{item.title}\nShop: {item.shop_name}\n"
        f"Price: {price_text(item)}{expiry}{attributes}\nServer: {guild_name}"
    )[:4096]
