from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from slopbot.database import SubscriptionStore
from slopbot.engagement import send_to_destinations
from slopbot.models import (
    MAX_TOURNAMENT_LEAD_MINUTES,
    MIN_TOURNAMENT_LEAD_MINUTES,
    TOURNAMENT_GAME_MODES,
    TOURNAMENT_TEAM_SIZES,
    TournamentChannelAlert,
    TournamentOptInDelivery,
    TournamentUserAlert,
)
from slopbot.rocketleague import (
    RocketLeagueService,
    RocketLeagueServiceError,
    RocketLeagueTournament,
)
from slopbot.telegram import TelegramService
from slopbot.visibility import (
    RESPONSE_VISIBILITY_CHOICES,
    defer_result_response,
)

if TYPE_CHECKING:
    from slopbot.bot import SlopBot

LOGGER = logging.getLogger(__name__)
DEFAULT_LEAD_MINUTES = 15
DEFAULT_TOURNAMENT_REGION = "USE"
SCHEDULE_CACHE_SECONDS = 60
STALE_SCHEDULE_SECONDS = 1800
DELIVERY_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_SCHEDULE_DISPLAY = 10
MAX_OPT_IN_DELIVERY_ATTEMPTS = 5

REGION_CHOICES = [
    app_commands.Choice(name="North America East", value="USE"),
    app_commands.Choice(name="North America Central", value="USC"),
    app_commands.Choice(name="North America West", value="USW"),
    app_commands.Choice(name="Europe", value="EU"),
    app_commands.Choice(name="South America", value="SAM"),
    app_commands.Choice(name="Middle East", value="ME"),
    app_commands.Choice(name="Oceania", value="OCE"),
    app_commands.Choice(name="Asia East", value="JPN"),
    app_commands.Choice(name="Asia Mainland", value="ASM"),
    app_commands.Choice(name="Asia Maritime", value="ASC"),
    app_commands.Choice(name="South Africa", value="SAF"),
    app_commands.Choice(name="India", value="IND"),
    app_commands.Choice(name="Automatic (service account; NAE fallback)", value="auto"),
]
GAME_MODE_LABELS = {
    "soccar": "Soccar",
    "rumble": "Rumble",
    "hoops": "Hoops",
    "dropshot": "Dropshot",
    "snow_day": "Snow Day",
    "heatseeker": "Heatseeker",
    "pentathlon": "Pentathlon",
    "other": "Other / special modes",
}


@dataclass(frozen=True, slots=True)
class CachedSchedule:
    fetched_at: int
    resolved_region: str
    tournaments: tuple[RocketLeagueTournament, ...]


class TournamentReminderButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"tournament-reminder:(?P<token>[a-f0-9]{24})",
):
    def __init__(self, token: str, lead_minutes: int | None = None) -> None:
        self.token = token
        label = "Remind me"
        if lead_minutes is not None:
            label = f"Remind me {lead_minutes} min before"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                emoji="🔔",
                custom_id=f"tournament-reminder:{token}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: Any,
    ) -> TournamentReminderButton:
        return cls(match["token"])

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This reminder can only be requested from its server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        if not isinstance(bot, commands.Bot):
            await interaction.followup.send(
                "Tournament reminders are temporarily unavailable.", ephemeral=True
            )
            return
        cog = bot.get_cog("TournamentAlertsCog")
        if not isinstance(cog, TournamentAlertsCog):
            await interaction.followup.send(
                "Tournament reminders are temporarily unavailable.", ephemeral=True
            )
            return
        result = await cog.store.subscribe_tournament_opt_in(
            token=self.token,
            guild_id=guild.id,
            user_id=interaction.user.id,
            now=int(time.time()),
        )
        messages = {
            "created": "You'll receive a one-time reminder using your `/notifications` preference.",
            "existing": "You already requested a reminder for this tournament.",
            "expired": "That tournament has already started or this button has expired.",
        }
        await interaction.followup.send(messages[result], ephemeral=True)


class TournamentAlertConfigView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        store: SubscriptionStore,
        current: TournamentUserAlert | TournamentChannelAlert | None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.store = store
        self.channel = channel
        self.region = current.region if current is not None else DEFAULT_TOURNAMENT_REGION
        self.lead_minutes = current.lead_minutes if current is not None else DEFAULT_LEAD_MINUTES
        self.opt_in_lead_minutes = (
            current.opt_in_lead_minutes
            if isinstance(current, TournamentChannelAlert)
            else DEFAULT_LEAD_MINUTES
        )
        self.team_sizes = (
            current.team_sizes if current is not None else tuple(TOURNAMENT_TEAM_SIZES)
        )
        self.game_modes = (
            current.game_modes if current is not None else tuple(TOURNAMENT_GAME_MODES)
        )

        self.region_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Tournament region",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=choice.name,
                    value=choice.value,
                    default=choice.value == self.region,
                )
                for choice in REGION_CHOICES
            ],
            row=0,
        )
        self.region_select.callback = self._select_region  # type: ignore[method-assign]
        self.add_item(self.region_select)

        self.lead_button: discord.ui.Button[Any] = discord.ui.Button(
            label=f"Lead time: {self.lead_minutes} min",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        self.lead_button.callback = self._set_lead  # type: ignore[method-assign]
        self.add_item(self.lead_button)

        self.team_size_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Choose one or more team sizes",
            min_values=1,
            max_values=len(TOURNAMENT_TEAM_SIZES),
            options=[
                discord.SelectOption(
                    label=f"{team_size}v{team_size}",
                    value=str(team_size),
                    default=team_size in self.team_sizes,
                )
                for team_size in TOURNAMENT_TEAM_SIZES
            ],
            row=2,
        )
        self.team_size_select.callback = self._select_team_sizes  # type: ignore[method-assign]
        self.add_item(self.team_size_select)

        self.game_mode_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Choose one or more game modes",
            min_values=1,
            max_values=len(TOURNAMENT_GAME_MODES),
            options=[
                discord.SelectOption(
                    label=GAME_MODE_LABELS[game_mode],
                    value=game_mode,
                    default=game_mode in self.game_modes,
                )
                for game_mode in TOURNAMENT_GAME_MODES
            ],
            row=3,
        )
        self.game_mode_select.callback = self._select_game_modes  # type: ignore[method-assign]
        self.add_item(self.game_mode_select)

        save_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Save and enable",
            style=discord.ButtonStyle.success,
            row=4,
        )
        save_button.callback = self._save  # type: ignore[method-assign]
        self.add_item(save_button)
        disable_button: discord.ui.Button[Any] = discord.ui.Button(
            label="Disable alerts",
            style=discord.ButtonStyle.danger,
            row=4,
        )
        disable_button.callback = self._disable  # type: ignore[method-assign]
        self.add_item(disable_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this menu can use it.", ephemeral=True
        )
        return False

    def summary(self) -> str:
        target = self.channel.mention if self.channel is not None else "your personal reminders"
        return (
            f"Configure tournament alerts for {target}. Current draft: "
            f"**{self.lead_minutes} minutes** early, region **{region_label(self.region)}**, "
            + (
                f"button reminders **{self.opt_in_lead_minutes} minutes** early, "
                if self.channel is not None
                else ""
            )
            + f"team sizes **{team_sizes_label(self.team_sizes)}**, game modes "
            f"**{game_modes_label(self.game_modes)}**. Select values, then choose "
            "**Save and enable**."
        )

    async def _update(self, interaction: discord.Interaction) -> None:
        for option in self.region_select.options:
            option.default = option.value == self.region
        self.lead_button.label = f"Lead time: {self.lead_minutes} min"
        for option in self.team_size_select.options:
            option.default = int(option.value) in self.team_sizes
        for option in self.game_mode_select.options:
            option.default = option.value in self.game_modes
        await interaction.response.edit_message(content=self.summary(), view=self)

    async def _select_region(self, interaction: discord.Interaction) -> None:
        self.region = self.region_select.values[0]
        await self._update(interaction)

    async def _set_lead(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TournamentLeadTimeModal(self))

    async def _select_team_sizes(self, interaction: discord.Interaction) -> None:
        selected = {int(value) for value in self.team_size_select.values}
        self.team_sizes = tuple(size for size in TOURNAMENT_TEAM_SIZES if size in selected)
        await self._update(interaction)

    async def _select_game_modes(self, interaction: discord.Interaction) -> None:
        selected = set(self.game_mode_select.values)
        self.game_modes = tuple(mode for mode in TOURNAMENT_GAME_MODES if mode in selected)
        await self._update(interaction)

    async def _save(self, interaction: discord.Interaction) -> None:
        if self.channel is not None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "You need Manage Server to save channel reminders.", ephemeral=True
                )
                return
            bot_member = self.channel.guild.me
            if bot_member is None or not can_post_tournament_alert(self.channel, bot_member):
                await interaction.response.send_message(
                    "Slopbot needs View Channel, Send Messages, and Embed Links in "
                    f"{self.channel.mention}.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer()
        if self.channel is None:
            saved_user = await self.store.save_tournament_user_alert(
                guild_id=self.guild_id,
                user_id=self.owner_id,
                region=self.region,
                lead_minutes=self.lead_minutes,
                team_sizes=self.team_sizes,
                game_modes=self.game_modes,
            )
            message = personal_alert_status(saved_user)
        else:
            saved_channel = await self.store.save_tournament_channel_alert(
                guild_id=self.guild_id,
                channel_id=self.channel.id,
                configured_by_user_id=self.owner_id,
                region=self.region,
                lead_minutes=self.lead_minutes,
                team_sizes=self.team_sizes,
                game_modes=self.game_modes,
                opt_in_lead_minutes=self.opt_in_lead_minutes,
            )
            message = channel_alert_status(self.channel, saved_channel)
        await interaction.edit_original_response(content=message, view=None)
        self.stop()

    async def _disable(self, interaction: discord.Interaction) -> None:
        if self.channel is not None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "You need Manage Server to disable channel reminders.", ephemeral=True
                )
                return
        await interaction.response.defer()
        if self.channel is None:
            removed = await self.store.delete_tournament_user_alert(
                guild_id=self.guild_id, user_id=self.owner_id
            )
            message = (
                "Personal tournament reminders are off."
                if removed
                else "Personal tournament reminders were already off."
            )
        else:
            removed = await self.store.delete_tournament_channel_alert(
                guild_id=self.guild_id, channel_id=self.channel.id
            )
            message = (
                f"Tournament reminders in {self.channel.mention} are off."
                if removed
                else f"Tournament reminders in {self.channel.mention} were already off."
            )
        await interaction.edit_original_response(content=message, view=None)
        self.stop()


class TournamentLeadTimeModal(discord.ui.Modal, title="Set tournament lead times"):
    def __init__(self, panel: TournamentAlertConfigView) -> None:
        super().__init__()
        self.panel = panel
        self.lead_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="Minutes before the tournament",
            placeholder=(
                f"Whole number from {MIN_TOURNAMENT_LEAD_MINUTES} to {MAX_TOURNAMENT_LEAD_MINUTES}"
            ),
            default=str(panel.lead_minutes),
            min_length=1,
            max_length=len(str(MAX_TOURNAMENT_LEAD_MINUTES)),
        )
        self.add_item(self.lead_input)
        self.opt_in_lead_input: discord.ui.TextInput[Any] | None = None
        if panel.channel is not None:
            self.opt_in_lead_input = discord.ui.TextInput(
                label="Button reminder minutes before",
                placeholder="Used when a member presses Remind me",
                default=str(panel.opt_in_lead_minutes),
                min_length=1,
                max_length=len(str(MAX_TOURNAMENT_LEAD_MINUTES)),
            )
            self.add_item(self.opt_in_lead_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            lead_minutes = int(self.lead_input.value.strip())
        except ValueError:
            lead_minutes = 0
        if not MIN_TOURNAMENT_LEAD_MINUTES <= lead_minutes <= MAX_TOURNAMENT_LEAD_MINUTES:
            await interaction.response.send_message(
                f"Enter a whole number from {MIN_TOURNAMENT_LEAD_MINUTES} to "
                f"{MAX_TOURNAMENT_LEAD_MINUTES} minutes.",
                ephemeral=True,
            )
            return
        opt_in_lead_minutes = self.panel.opt_in_lead_minutes
        if self.opt_in_lead_input is not None:
            try:
                opt_in_lead_minutes = int(self.opt_in_lead_input.value.strip())
            except ValueError:
                opt_in_lead_minutes = 0
            if (
                not MIN_TOURNAMENT_LEAD_MINUTES
                <= opt_in_lead_minutes
                <= MAX_TOURNAMENT_LEAD_MINUTES
            ):
                await interaction.response.send_message(
                    f"Enter button reminder minutes from {MIN_TOURNAMENT_LEAD_MINUTES} to "
                    f"{MAX_TOURNAMENT_LEAD_MINUTES}.",
                    ephemeral=True,
                )
                return
        self.panel.lead_minutes = lead_minutes
        self.panel.opt_in_lead_minutes = opt_in_lead_minutes
        self.panel.lead_button.label = f"Lead time: {lead_minutes} min"
        await interaction.response.edit_message(content=self.panel.summary(), view=self.panel)


class TournamentAlertsCog(commands.Cog):
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
        self._schedule_cache: dict[str, CachedSchedule] = {}
        self.worker_started_at = int(time.time())
        self.last_tournament_delivery_success_at: int | None = None

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(TournamentReminderButton)
        if self.service is not None:
            # A transient database, Discord, or upstream failure must not permanently stop
            # tournament monitoring. Individual expected failures are handled closer to source;
            # this is the final supervision boundary for the recurring task.
            self.deliver_tournament_alerts.add_exception_type(Exception)
            self.deliver_tournament_alerts.start()

    async def cog_unload(self) -> None:
        self.deliver_tournament_alerts.cancel()
        self.bot.remove_dynamic_items(TournamentReminderButton)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Try that again in {error.retry_after:.0f} seconds."
        else:
            LOGGER.error(
                "Tournament command failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "That tournament command could not be completed. Try again shortly."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="tournaments",
        description="Show upcoming scheduled Rocket League tournaments",
    )
    @app_commands.describe(
        region="Tournament schedule region; automatic uses the bot account",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(region=REGION_CHOICES, visibility=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def tournaments(
        self,
        interaction: discord.Interaction,
        region: app_commands.Choice[str] | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="tournaments",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /tournaments in this channel."
            )
            return
        if self.service is None:
            await interaction.edit_original_response(
                content="Rocket League schedules are not configured on this bot."
            )
            return
        try:
            schedule = await self._get_schedule(
                region.value if region is not None else DEFAULT_TOURNAMENT_REGION
            )
        except RocketLeagueServiceError:
            LOGGER.exception("Could not load the tournament schedule")
            await interaction.edit_original_response(
                content="Rocket League tournament schedules are temporarily unavailable."
            )
            return
        now = int(time.time())
        upcoming = [
            tournament for tournament in schedule.tournaments if tournament.start_time > now
        ]
        await interaction.edit_original_response(
            embed=schedule_embed(
                upcoming[:MAX_SCHEDULE_DISPLAY],
                resolved_region=schedule.resolved_region,
                total_upcoming=len(upcoming),
            )
        )

    @app_commands.command(
        name="tournamentalerts",
        description="Configure your personal Rocket League tournament reminders",
    )
    @app_commands.guild_only()
    async def tournament_alerts(
        self,
        interaction: discord.Interaction,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if self.service is None:
            await interaction.response.send_message(
                "Rocket League schedules are not configured on this bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        current = await self.store.get_tournament_user_alert(
            guild_id=guild.id,
            user_id=interaction.user.id,
        )
        view = TournamentAlertConfigView(
            owner_id=interaction.user.id,
            guild_id=guild.id,
            store=self.store,
            current=current,
        )
        await interaction.edit_original_response(content=view.summary(), view=view)

    @app_commands.command(
        name="tournamentchannel",
        description="Configure tournament reminders in a server text channel",
    )
    @app_commands.describe(channel="Text channel to configure")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def tournament_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to configure channel reminders.", ephemeral=True
            )
            return
        if channel.guild.id != guild.id:
            await interaction.response.send_message(
                "Choose a text channel from this server.", ephemeral=True
            )
            return
        if self.service is None:
            await interaction.response.send_message(
                "Rocket League schedules are not configured on this bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        bot_member = guild.me
        if bot_member is None or not can_post_tournament_alert(channel, bot_member):
            await interaction.edit_original_response(
                content=(
                    "Slopbot needs View Channel, Send Messages, and Embed Links in "
                    f"{channel.mention}."
                )
            )
            return
        current = await self.store.get_tournament_channel_alert(
            guild_id=guild.id,
            channel_id=channel.id,
        )
        view = TournamentAlertConfigView(
            owner_id=member.id,
            guild_id=guild.id,
            store=self.store,
            current=current,
            channel=channel,
        )
        await interaction.edit_original_response(content=view.summary(), view=view)

    @tasks.loop(minutes=1.0)
    async def deliver_tournament_alerts(self) -> None:
        now = int(time.time())
        user_alerts = await self.store.list_tournament_user_alerts()
        channel_alerts = await self.store.list_tournament_channel_alerts()
        await self.store.prune_tournament_deliveries(before=now - DELIVERY_RETENTION_SECONDS)
        await self.store.prune_tournament_opt_ins(before=now - DELIVERY_RETENTION_SECONDS)
        for delivery in await self.store.list_due_tournament_opt_ins(now=now):
            await self._process_opt_in_delivery(delivery, now=now)
        if not user_alerts and not channel_alerts:
            self.last_tournament_delivery_success_at = int(time.time())
            return
        alerts_by_region: dict[
            str,
            tuple[list[TournamentUserAlert], list[TournamentChannelAlert]],
        ] = {}
        for user_alert in user_alerts:
            alerts_by_region.setdefault(user_alert.region, ([], []))[0].append(user_alert)
        for channel_alert in channel_alerts:
            alerts_by_region.setdefault(channel_alert.region, ([], []))[1].append(channel_alert)

        all_regions_succeeded = True
        for region, (regional_users, regional_channels) in alerts_by_region.items():
            try:
                schedule = await self._get_schedule(region, now=now)
            except RocketLeagueServiceError:
                LOGGER.exception("Could not poll Rocket League tournaments for region %s", region)
                all_regions_succeeded = False
                continue
            for user_alert in regional_users:
                await self._process_user_alert(user_alert, schedule, now=now)
            for channel_alert in regional_channels:
                await self._process_channel_alert(channel_alert, schedule, now=now)
        if all_regions_succeeded:
            self.last_tournament_delivery_success_at = int(time.time())

    @deliver_tournament_alerts.before_loop
    async def before_deliver_tournament_alerts(self) -> None:
        await self.bot.wait_until_ready()

    async def _get_schedule(self, region: str, *, now: int | None = None) -> CachedSchedule:
        if self.service is None:
            raise RocketLeagueServiceError("Rocket League schedules are not configured")
        checked_at = int(time.time()) if now is None else now
        cached = self._schedule_cache.get(region)
        if cached is not None and checked_at - cached.fetched_at < SCHEDULE_CACHE_SECONDS:
            return cached
        try:
            resolved_region, tournaments = await self.service.tournaments(region=region)
        except RocketLeagueServiceError:
            if cached is not None and checked_at - cached.fetched_at < STALE_SCHEDULE_SECONDS:
                LOGGER.warning("Using cached tournament schedule for region %s", region)
                return cached
            raise
        refreshed = CachedSchedule(checked_at, resolved_region, tuple(tournaments))
        self._schedule_cache[region] = refreshed
        return refreshed

    async def _process_user_alert(
        self,
        alert: TournamentUserAlert,
        schedule: CachedSchedule,
        *,
        now: int,
    ) -> None:
        claimed = await self._claim_due(
            target_type="user",
            target_id=alert.user_id,
            guild_id=alert.guild_id,
            lead_minutes=alert.lead_minutes,
            team_sizes=alert.team_sizes,
            game_modes=alert.game_modes,
            tournaments=schedule.tournaments,
            now=now,
        )
        if not claimed:
            return
        try:
            complete = await self._send_user_alert(alert, schedule.resolved_region, claimed)
        except Exception:
            LOGGER.exception(
                "Unexpected tournament alert failure for user %s in guild %s",
                alert.user_id,
                alert.guild_id,
            )
            complete = False
        if not complete:
            await self._release_claims(
                target_type="user",
                target_id=alert.user_id,
                guild_id=alert.guild_id,
                lead_minutes=alert.lead_minutes,
                tournaments=claimed,
            )
        else:
            await self._finish_claims(
                target_type="user",
                target_id=alert.user_id,
                guild_id=alert.guild_id,
                lead_minutes=alert.lead_minutes,
                tournaments=claimed,
            )

    async def _process_channel_alert(
        self,
        alert: TournamentChannelAlert,
        schedule: CachedSchedule,
        *,
        now: int,
    ) -> None:
        claimed = await self._claim_due(
            target_type="channel",
            target_id=alert.channel_id,
            guild_id=alert.guild_id,
            lead_minutes=alert.lead_minutes,
            team_sizes=alert.team_sizes,
            game_modes=alert.game_modes,
            tournaments=schedule.tournaments,
            now=now,
        )
        if not claimed:
            return
        for tournament in claimed:
            try:
                complete = await self._send_channel_alert(
                    alert, schedule.resolved_region, [tournament]
                )
            except Exception:
                LOGGER.exception(
                    "Unexpected tournament alert failure for channel %s in guild %s",
                    alert.channel_id,
                    alert.guild_id,
                )
                complete = False
            if not complete:
                await self._release_claims(
                    target_type="channel",
                    target_id=alert.channel_id,
                    guild_id=alert.guild_id,
                    lead_minutes=alert.lead_minutes,
                    tournaments=[tournament],
                )
            else:
                await self._finish_claims(
                    target_type="channel",
                    target_id=alert.channel_id,
                    guild_id=alert.guild_id,
                    lead_minutes=alert.lead_minutes,
                    tournaments=[tournament],
                )

    async def _claim_due(
        self,
        *,
        target_type: str,
        target_id: int,
        guild_id: int,
        lead_minutes: int,
        team_sizes: tuple[int, ...],
        game_modes: tuple[str, ...],
        tournaments: tuple[RocketLeagueTournament, ...],
        now: int,
    ) -> list[RocketLeagueTournament]:
        claimed: list[RocketLeagueTournament] = []
        for tournament in tournaments:
            seconds_until = tournament.start_time - now
            if seconds_until <= 0 or seconds_until > lead_minutes * 60:
                continue
            if tournament.team_size not in team_sizes:
                continue
            if tournament_game_mode(tournament) not in game_modes:
                continue
            if await self.store.claim_tournament_delivery(
                target_type=target_type,
                guild_id=guild_id,
                target_id=target_id,
                tournament_id=tournament.id,
                start_time=tournament.start_time,
                lead_minutes=lead_minutes,
                now=now,
            ):
                claimed.append(tournament)
        return claimed

    async def _release_claims(
        self,
        *,
        target_type: str,
        target_id: int,
        guild_id: int,
        lead_minutes: int,
        tournaments: list[RocketLeagueTournament],
    ) -> None:
        for tournament in tournaments:
            await self.store.release_tournament_delivery(
                target_type=target_type,
                guild_id=guild_id,
                target_id=target_id,
                tournament_id=tournament.id,
                start_time=tournament.start_time,
                lead_minutes=lead_minutes,
            )

    async def _finish_claims(
        self,
        *,
        target_type: str,
        target_id: int,
        guild_id: int,
        lead_minutes: int,
        tournaments: list[RocketLeagueTournament],
    ) -> None:
        for tournament in tournaments:
            await self.store.finish_tournament_delivery(
                target_type=target_type,
                guild_id=guild_id,
                target_id=target_id,
                tournament_id=tournament.id,
                start_time=tournament.start_time,
                lead_minutes=lead_minutes,
            )

    async def _send_user_alert(
        self,
        alert: TournamentUserAlert,
        resolved_region: str,
        tournaments: list[RocketLeagueTournament],
    ) -> bool:
        member, membership_known = await self._resolve_reminder_member(
            alert.guild_id, alert.user_id
        )
        if not membership_known:
            return False
        if member is None:
            await self.store.delete_tournament_user_alert(
                guild_id=alert.guild_id,
                user_id=alert.user_id,
            )
            return True
        embed = tournament_alert_embed(
            tournaments,
            resolved_region=resolved_region,
            lead_minutes=alert.lead_minutes,
        )
        outcome = await send_to_destinations(
            bot=self.bot,
            store=self.store,
            telegram=self.telegram,
            guild_id=alert.guild_id,
            recipient_id=member.id,
            discord_embed=embed,
            telegram_text=tournament_alert_text(
                tournaments,
                resolved_region=resolved_region,
                lead_minutes=alert.lead_minutes,
            ),
        )
        return outcome.delivered or not outcome.retryable

    async def _resolve_reminder_member(
        self, guild_id: int, user_id: int
    ) -> tuple[discord.Member | None, bool]:
        """A cache miss is not proof that a subscriber left the server."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, False
        member = guild.get_member(user_id)
        if member is not None:
            return member, True
        try:
            return await guild.fetch_member(user_id), True
        except discord.NotFound:
            return None, True
        except discord.HTTPException:
            LOGGER.warning(
                "Could not verify tournament subscriber %s in guild %s", user_id, guild_id
            )
            return None, False

    async def _send_channel_alert(
        self,
        alert: TournamentChannelAlert,
        resolved_region: str,
        tournaments: list[RocketLeagueTournament],
    ) -> bool:
        guild = self.bot.get_guild(alert.guild_id)
        if guild is None:
            return False
        channel = self.bot.get_channel(alert.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(alert.channel_id)
            except discord.NotFound:
                await self.store.delete_tournament_channel_alert(
                    guild_id=alert.guild_id, channel_id=alert.channel_id
                )
                return True
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.exception("Could not resolve tournament channel %s", alert.channel_id)
                return False
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != alert.guild_id:
            await self.store.delete_tournament_channel_alert(
                guild_id=alert.guild_id, channel_id=alert.channel_id
            )
            return True
        bot_member = channel.guild.me
        if bot_member is None or not can_post_tournament_alert(channel, bot_member):
            LOGGER.warning(
                "Tournament channel %s is temporarily inaccessible; keeping its configuration",
                alert.channel_id,
            )
            return False
        try:
            tournament = tournaments[0]
            token = hashlib.sha256(
                (
                    f"{alert.guild_id}:{alert.channel_id}:{tournament.id}:"
                    f"{tournament.start_time}:{alert.opt_in_lead_minutes}"
                ).encode()
            ).hexdigest()[:24]
            await self.store.save_tournament_opt_in_offer(
                token=token,
                guild_id=alert.guild_id,
                channel_id=alert.channel_id,
                tournament_id=tournament.id,
                tournament_title=tournament.title,
                start_time=tournament.start_time,
                region=resolved_region,
                lead_minutes=alert.opt_in_lead_minutes,
                now=int(time.time()),
            )
            view = discord.ui.View(timeout=None)
            view.add_item(TournamentReminderButton(token, alert.opt_in_lead_minutes))
            await channel.send(
                embed=tournament_alert_embed(
                    tournaments,
                    resolved_region=resolved_region,
                    lead_minutes=alert.lead_minutes,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except discord.Forbidden:
            LOGGER.warning(
                "Tournament channel %s rejected delivery; keeping its configuration", channel.id
            )
            return False
        except discord.HTTPException:
            LOGGER.exception("Discord rejected a tournament reminder for channel %s", channel.id)
            return False
        return True

    async def _process_opt_in_delivery(
        self, delivery: TournamentOptInDelivery, *, now: int
    ) -> None:
        member, membership_known = await self._resolve_reminder_member(
            delivery.guild_id, delivery.user_id
        )
        if not membership_known:
            await self._retry_opt_in_delivery(
                delivery,
                now=now,
                error="could not verify server membership",
            )
            return
        if member is None:
            await self.store.finish_tournament_opt_in(
                token=delivery.token, user_id=delivery.user_id
            )
            return
        embed = tournament_opt_in_embed(delivery)
        try:
            outcome = await send_to_destinations(
                bot=self.bot,
                store=self.store,
                telegram=self.telegram,
                guild_id=delivery.guild_id,
                recipient_id=delivery.user_id,
                discord_embed=embed,
                telegram_text=tournament_opt_in_text(delivery),
            )
        except Exception:
            LOGGER.exception("Unexpected opt-in tournament delivery failure")
            outcome = None
        if outcome is not None and (outcome.delivered or not outcome.retryable):
            await self.store.finish_tournament_opt_in(
                token=delivery.token, user_id=delivery.user_id
            )
            return
        await self._retry_opt_in_delivery(
            delivery,
            now=now,
            error=outcome.detail if outcome is not None else "unexpected delivery failure",
        )

    async def _retry_opt_in_delivery(
        self, delivery: TournamentOptInDelivery, *, now: int, error: str
    ) -> None:
        if delivery.attempt_count + 1 >= MAX_OPT_IN_DELIVERY_ATTEMPTS:
            await self.store.finish_tournament_opt_in(
                token=delivery.token, user_id=delivery.user_id
            )
            return
        delay = min(60 * (2**delivery.attempt_count), max(delivery.start_time - now - 1, 1))
        await self.store.retry_tournament_opt_in(
            token=delivery.token,
            user_id=delivery.user_id,
            next_attempt_at=now + delay,
            error=error,
        )


def can_post_tournament_alert(channel: discord.TextChannel, bot_member: discord.Member) -> bool:
    permissions = channel.permissions_for(bot_member)
    return permissions.view_channel and permissions.send_messages and permissions.embed_links


def personal_alert_status(alert: TournamentUserAlert | None) -> str:
    if alert is None:
        return "Personal tournament reminders are **off**. Run `/tournamentalerts` to configure."
    return (
        "Personal tournament reminders are **on**: "
        f"**{alert.lead_minutes} minutes** early, region **{region_label(alert.region)}**, "
        f"team sizes **{team_sizes_label(alert.team_sizes)}**, game modes "
        f"**{game_modes_label(alert.game_modes)}**. Delivery follows `/notifications`."
    )


def channel_alert_status(
    channel: discord.TextChannel,
    alert: TournamentChannelAlert | None,
) -> str:
    if alert is None:
        return f"Tournament reminders in {channel.mention} are **off**."
    return (
        f"Tournament reminders in {channel.mention} are **on**: "
        f"**{alert.lead_minutes} minutes** early, region **{region_label(alert.region)}**, "
        f"button reminders **{alert.opt_in_lead_minutes} minutes** early, "
        f"team sizes **{team_sizes_label(alert.team_sizes)}**, game modes "
        f"**{game_modes_label(alert.game_modes)}**."
    )


def schedule_embed(
    tournaments: list[RocketLeagueTournament],
    *,
    resolved_region: str,
    total_upcoming: int,
) -> discord.Embed:
    if not tournaments:
        return discord.Embed(
            title="Rocket League tournament schedule",
            description=(
                f"No upcoming scheduled tournaments were returned for **{resolved_region}**."
            ),
            color=discord.Color.orange(),
        )
    lines = [tournament_line(tournament) for tournament in tournaments]
    embed = discord.Embed(
        title="Upcoming Rocket League tournaments",
        description="\n\n".join(lines),
        color=discord.Color.blurple(),
    )
    hidden = total_upcoming - len(tournaments)
    suffix = f" • {hidden} more not shown" if hidden > 0 else ""
    embed.set_footer(text=f"Schedule region: {resolved_region}{suffix}")
    return embed


def tournament_alert_embed(
    tournaments: list[RocketLeagueTournament],
    *,
    resolved_region: str,
    lead_minutes: int,
) -> discord.Embed:
    first_start = min(tournament.start_time for tournament in tournaments)
    title = (
        "Rocket League tournament reminder"
        if len(tournaments) == 1
        else "Rocket League tournament reminders"
    )
    embed = discord.Embed(
        title=title,
        description=(
            f"The next tournament starts <t:{first_start}:R>. "
            f"These reminders are configured for **{lead_minutes} minutes** ahead."
        ),
        color=discord.Color.gold(),
    )
    for tournament in tournaments[:MAX_SCHEDULE_DISPLAY]:
        embed.add_field(
            name=safe_title(tournament.title),
            value=tournament_details(tournament),
            inline=False,
        )
    hidden = len(tournaments) - MAX_SCHEDULE_DISPLAY
    footer = f"Schedule region: {resolved_region}"
    if hidden > 0:
        footer += f" • {hidden} more tournament{'s' if hidden != 1 else ''}"
    embed.set_footer(text=footer)
    return embed


def tournament_alert_text(
    tournaments: list[RocketLeagueTournament],
    *,
    resolved_region: str,
    lead_minutes: int,
) -> str:
    lines = [
        f"Rocket League tournament reminder ({lead_minutes} minutes early)",
        f"Schedule region: {resolved_region}",
        "",
    ]
    for tournament in tournaments[:20]:
        lines.append(
            f"• {tournament.title} — {format_utc_time(tournament.start_time)} — "
            f"{team_size_label(tournament.team_size)}"
        )
    if len(tournaments) > 20:
        lines.append(f"• …and {len(tournaments) - 20} more")
    return "\n".join(lines)[:4096]


def tournament_opt_in_embed(delivery: TournamentOptInDelivery) -> discord.Embed:
    embed = discord.Embed(
        title="Your Rocket League tournament reminder",
        description=(
            f"**{safe_title(delivery.tournament_title)}** starts "
            f"<t:{delivery.start_time}:R> (<t:{delivery.start_time}:F>)."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"One-time reminder • Schedule region: {delivery.region}")
    return embed


def tournament_opt_in_text(delivery: TournamentOptInDelivery) -> str:
    starts = datetime.fromtimestamp(delivery.start_time, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "Your one-time Rocket League tournament reminder\n\n"
        f"{delivery.tournament_title}\nStarts: {starts}\nRegion: {delivery.region}"
    )


def tournament_line(tournament: RocketLeagueTournament) -> str:
    return f"**{safe_title(tournament.title)}**\n{tournament_details(tournament)}"


def tournament_details(tournament: RocketLeagueTournament) -> str:
    details = [
        f"<t:{tournament.start_time}:F> (<t:{tournament.start_time}:R>)",
        team_size_label(tournament.team_size),
    ]
    if tournament.max_bracket_size > 0:
        details.append(f"up to {tournament.max_bracket_size} teams")
    if tournament.region:
        details.append(tournament.region)
    return " • ".join(details)


def safe_title(title: str) -> str:
    cleaned = discord.utils.escape_markdown(discord.utils.escape_mentions(title.strip()))
    return (cleaned or "Scheduled tournament")[:256]


def team_size_label(team_size: int) -> str:
    return f"{team_size}v{team_size}"


def team_sizes_label(team_sizes: tuple[int, ...]) -> str:
    if set(team_sizes) == set(TOURNAMENT_TEAM_SIZES):
        return "all sizes"
    return ", ".join(team_size_label(team_size) for team_size in team_sizes)


def game_modes_label(game_modes: tuple[str, ...]) -> str:
    if set(game_modes) == set(TOURNAMENT_GAME_MODES):
        return "all modes"
    return ", ".join(GAME_MODE_LABELS[game_mode] for game_mode in game_modes)


def tournament_game_mode(tournament: RocketLeagueTournament) -> str:
    title = " ".join(tournament.title.casefold().replace("-", " ").split())
    if "pentathlon" in title:
        return "pentathlon"
    if "heatseeker" in title or "heat seeker" in title:
        return "heatseeker"
    if "rumble" in title:
        return "rumble"
    if "hoops" in title:
        return "hoops"
    if "dropshot" in title or "drop shot" in title:
        return "dropshot"
    if "snowday" in title or "snow day" in title:
        return "snow_day"
    if "soccar" in title or tournament.game_mode == 0:
        return "soccar"
    return "other"


def region_label(region: str) -> str:
    return "automatic (NAE fallback)" if region == "auto" else region


def format_utc_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
