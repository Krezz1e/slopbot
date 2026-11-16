from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from slopbot.config import Settings
from slopbot.database import SubscriptionStore
from slopbot.engagement import EngagementCog, send_to_destinations
from slopbot.info import InfoCog, validate_info_command_coverage
from slopbot.itemshop import ItemShopCog
from slopbot.models import DeliveryMethod, delivery_channels
from slopbot.notifications import VoiceAlertService, VoiceOccupancyTracker
from slopbot.rocketleague import RocketLeagueCog, RocketLeagueService, StatusCog
from slopbot.summary_alerts import SummaryCog
from slopbot.telegram import TelegramError, TelegramService
from slopbot.tournaments import TournamentAlertsCog
from slopbot.ui import AlertsHomeView, home_embed

LOGGER = logging.getLogger(__name__)


class SlopbotCommandTree(app_commands.CommandTree[discord.Client]):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        LOGGER.error(
            "Unhandled slash-command failure for %s",
            getattr(interaction.command, "qualified_name", "unknown command"),
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "Slopbot could not complete that command. Please try again shortly."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.exception("Could not send slash-command failure response")


class AlertsCog(commands.Cog):
    def __init__(self, bot: SlopBot) -> None:
        self.bot = bot

    @app_commands.command(name="alerts", description="Create and manage your voice-channel alerts")
    @app_commands.guild_only()
    async def alerts(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Use this command inside a server.",
                ephemeral=True,
            )
            return
        view = AlertsHomeView(
            owner_id=interaction.user.id,
            guild=guild,
            store=self.bot.store,
        )
        await interaction.response.send_message(embed=home_embed(), view=view, ephemeral=True)

    @app_commands.command(
        name="notifications", description="Choose where Slopbot sends your private alerts"
    )
    @app_commands.describe(
        delivery="Where Slopbot should send your alerts",
        unlink_telegram="Remove your saved Telegram link",
    )
    @app_commands.choices(
        delivery=[
            app_commands.Choice(name="Discord", value=DeliveryMethod.DISCORD.value),
            app_commands.Choice(name="Telegram", value=DeliveryMethod.TELEGRAM.value),
            app_commands.Choice(name="PWA push", value=DeliveryMethod.PWA.value),
            app_commands.Choice(name="Discord and Telegram", value=DeliveryMethod.BOTH.value),
            app_commands.Choice(
                name="Discord and PWA push", value=DeliveryMethod.DISCORD_PWA.value
            ),
            app_commands.Choice(
                name="Telegram and PWA push", value=DeliveryMethod.TELEGRAM_PWA.value
            ),
            app_commands.Choice(
                name="Discord, Telegram, and PWA push", value=DeliveryMethod.ALL.value
            ),
        ]
    )
    @app_commands.guild_only()
    async def notifications(
        self,
        interaction: discord.Interaction,
        delivery: app_commands.Choice[str] | None = None,
        unlink_telegram: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if unlink_telegram:
            removed = await self.bot.store.unlink_telegram(discord_user_id=interaction.user.id)
            current = await self.bot.store.get_delivery_method(
                guild_id=guild.id, user_id=interaction.user.id
            )
            await interaction.response.send_message(
                f"Telegram was unlinked. Your alerts now use **{_delivery_label(current)}**."
                if removed
                else f"No Telegram account was linked. Current delivery is "
                f"**{_delivery_label(current)}**.",
                ephemeral=True,
            )
            return

        if delivery is None:
            current = await self.bot.store.get_delivery_method(
                guild_id=guild.id, user_id=interaction.user.id
            )
            linked = (
                await self.bot.store.get_telegram_chat_id(discord_user_id=interaction.user.id)
                is not None
            )
            push = getattr(getattr(self.bot, "web", None), "push", None)
            push_ready = bool(
                push is not None
                and push.configured
                and await push.has_subscriptions(interaction.user.id)
            )
            await interaction.response.send_message(
                f"Current delivery: **{_delivery_label(current)}**. "
                f"Telegram is **{'linked' if linked else 'not linked'}**. "
                f"PWA push is **{'enabled' if push_ready else 'not enabled'}**. "
                "Run `/notifications` again and choose a delivery option to change it.",
                ephemeral=True,
            )
            return

        selected = DeliveryMethod(delivery.value)
        channels = delivery_channels(selected)
        uses_telegram = "telegram" in channels
        if uses_telegram and self.bot.telegram is None:
            await interaction.response.send_message(
                "Telegram delivery is not configured on this bot yet. Ask the server owner to "
                "set TELEGRAM_BOT_TOKEN.",
                ephemeral=True,
            )
            return
        if "pwa" in channels:
            push = getattr(getattr(self.bot, "web", None), "push", None)
            if (
                push is None
                or not push.configured
                or not await push.has_subscriptions(interaction.user.id)
            ):
                await interaction.response.send_message(
                    "Enable PWA push for this account from the website before selecting it.",
                    ephemeral=True,
                )
                return
        await self.bot.store.set_delivery_method(
            guild_id=guild.id,
            user_id=interaction.user.id,
            delivery_method=selected,
        )
        linked_chat = await self.bot.store.get_telegram_chat_id(discord_user_id=interaction.user.id)
        if uses_telegram and linked_chat is None:
            try:
                if self.bot.telegram is None:
                    raise TelegramError("Telegram is not configured")
                link_url = await self.bot.telegram.create_link_url(
                    discord_user_id=interaction.user.id
                )
            except TelegramError:
                LOGGER.exception("Could not create Telegram linking URL")
                await interaction.response.send_message(
                    "The preference was saved, but Telegram linking is temporarily unavailable. "
                    "Try `/notifications` again shortly.",
                    ephemeral=True,
                )
                return
            view = discord.ui.View(timeout=600)
            view.add_item(discord.ui.Button(label="Link Telegram", url=link_url))
            await interaction.response.send_message(
                f"Delivery set to **{_delivery_label(selected)}**. Select **Link Telegram** "
                "within 10 minutes, then press **Start** in Telegram.",
                view=view,
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Delivery set to **{_delivery_label(selected)}**.", ephemeral=True
        )

    @app_commands.command(
        name="testnotification",
        description="Send a private test through your configured notification destinations",
    )
    @app_commands.guild_only()
    async def test_notification(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="Slopbot test notification",
            description=f"Your notification settings for **{interaction.guild.name}** are working.",
            colour=discord.Colour.blue(),
        )
        outcome = await send_to_destinations(
            bot=self.bot,
            store=self.bot.store,
            telegram=self.bot.telegram,
            guild_id=interaction.guild.id,
            recipient_id=interaction.user.id,
            discord_embed=embed,
            telegram_text=(
                "Slopbot test notification\n"
                f"Your notification settings for {interaction.guild.name} are working."
            ),
        )
        receipts = ", ".join(f"{name}: {status}" for name, status, _ in outcome.receipts)
        await interaction.followup.send(
            ("Test sent. " if outcome.delivered else "Test was not delivered. ")
            + (receipts or outcome.detail),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        transitions = await self.bot.occupancy.apply(member=member, before=before, after=after)
        for transition in transitions:
            await self.bot.alert_service.handle_channel_transition(
                channel=transition.channel,
                previous_count=transition.previous_count,
                current_count=transition.current_count,
                actor_user_id=transition.actor_user_id,
                actor_joined=transition.actor_joined,
            )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.bot.occupancy.forget(channel.id)
        deleted = await self.bot.store.delete_channel(
            guild_id=channel.guild.id,
            channel_id=channel.id,
        )
        if deleted:
            LOGGER.info("Removed %s subscriptions for deleted channel %s", deleted, channel.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        deleted = await self.bot.store.delete_guild(guild_id=guild.id)
        if deleted:
            LOGGER.info("Removed %s subscriptions after leaving guild %s", deleted, guild.id)


class SlopBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.presences = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=SlopbotCommandTree,
        )
        self.settings = settings
        self.store = SubscriptionStore(settings.database_path)
        self.telegram = (
            TelegramService(token=settings.telegram_token, store=self.store)
            if settings.telegram_token is not None
            else None
        )
        self.rocket_league = (
            RocketLeagueService(
                base_url=settings.rocket_league_url,
                token=settings.rocket_league_token,
            )
            if settings.rocket_league_url is not None and settings.rocket_league_token is not None
            else None
        )
        self.alert_service = VoiceAlertService(self, self.store, telegram=self.telegram)
        self.occupancy = VoiceOccupancyTracker()
        # Import lazily: existing Discord-only deployments need no web configuration.
        from slopbot.web.server import WebServer, WebSettings

        web_settings = WebSettings.from_environment()
        self.web = WebServer(self, web_settings) if web_settings is not None else None
        self._command_scope_path = settings.database_path.with_suffix(
            f"{settings.database_path.suffix}.command-scope"
        )

    async def setup_hook(self) -> None:
        await self.store.initialize()
        if self.telegram is not None:
            await self.telegram.start()
        if self.rocket_league is not None:
            await self.rocket_league.start()
        await self.add_cog(AlertsCog(self))
        await self.add_cog(EngagementCog(self, self.store, self.telegram))
        await self.add_cog(RocketLeagueCog(self, self.store, self.rocket_league))
        await self.add_cog(ItemShopCog(self, self.store, self.rocket_league, self.telegram))
        await self.add_cog(TournamentAlertsCog(self, self.store, self.rocket_league, self.telegram))
        await self.add_cog(StatusCog(self))
        await self.add_cog(SummaryCog(self))
        await self.add_cog(InfoCog())

        if self.web is not None:
            from slopbot.web.bridge import command_schema

            for command in self.web.commands().values():
                command_schema(command)
            await self.web.start()

        validate_info_command_coverage(
            command.qualified_name
            for command in self.tree.walk_commands()
            if isinstance(command, app_commands.Command)
        )

        previous_scope = self._read_command_scope()
        desired_scope = self._desired_command_scope()
        cleanup_guild_ids: set[int] = set()
        if previous_scope is not None and previous_scope != desired_scope:
            previous_guild_id = _guild_id_from_scope(previous_scope)
            if previous_guild_id is not None:
                cleanup_guild_ids.add(previous_guild_id)
        if self.settings.cleanup_guild_id is not None:
            cleanup_guild_ids.add(self.settings.cleanup_guild_id)
        for guild_id in cleanup_guild_ids:
            await self._clear_guild_commands(guild_id)

        if self.settings.guild_id is not None:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info("Synced %s commands to development guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %s global commands", len(synced))
        self._write_command_scope(desired_scope)

    async def on_ready(self) -> None:
        await self.occupancy.rebuild(self.guilds)
        if self.user is not None:
            LOGGER.info(
                "Connected as %s (%s) in %s guild(s)", self.user, self.user.id, len(self.guilds)
            )

    def _desired_command_scope(self) -> str:
        return f"guild:{self.settings.guild_id}" if self.settings.guild_id is not None else "global"

    def _read_command_scope(self) -> str | None:
        try:
            return self._command_scope_path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None
        except OSError:
            LOGGER.exception("Could not read command scope state from %s", self._command_scope_path)
            return None

    def _write_command_scope(self, scope: str) -> None:
        temporary_path = Path(f"{self._command_scope_path}.tmp")
        try:
            temporary_path.write_text(f"{scope}\n", encoding="utf-8")
            temporary_path.replace(self._command_scope_path)
        except OSError:
            LOGGER.exception(
                "Could not persist command scope state to %s", self._command_scope_path
            )

    async def _clear_guild_commands(self, guild_id: int) -> None:
        guild = discord.Object(id=guild_id)
        self.tree.clear_commands(guild=guild)
        await self.tree.sync(guild=guild)
        LOGGER.info("Removed guild-specific commands from guild %s", guild_id)

    async def close(self) -> None:
        if self.web is not None:
            await self.web.close()
        if self.rocket_league is not None:
            await self.rocket_league.close()
        if self.telegram is not None:
            await self.telegram.close()
        await super().close()


def _guild_id_from_scope(scope: str) -> int | None:
    prefix = "guild:"
    if not scope.startswith(prefix):
        return None
    try:
        guild_id = int(scope.removeprefix(prefix))
    except ValueError:
        LOGGER.warning("Ignoring invalid saved command scope %r", scope)
        return None
    return guild_id if guild_id > 0 else None


def create_bot(settings: Settings) -> SlopBot:
    return SlopBot(settings)


def _delivery_label(delivery: DeliveryMethod) -> str:
    return {
        DeliveryMethod.DISCORD: "Discord",
        DeliveryMethod.TELEGRAM: "Telegram",
        DeliveryMethod.PWA: "PWA push",
        DeliveryMethod.BOTH: "Discord and Telegram",
        DeliveryMethod.DISCORD_PWA: "Discord and PWA push",
        DeliveryMethod.TELEGRAM_PWA: "Telegram and PWA push",
        DeliveryMethod.ALL: "Discord, Telegram, and PWA push",
    }[delivery]
