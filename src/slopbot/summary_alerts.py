"""Shared summary authorization, Discord commands, and a durable delivery worker."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from slopbot.engagement import send_to_destinations
from slopbot.summaries import SECTIONS, Summaries, report_text, validate

LOGGER = logging.getLogger(__name__)
Cadence = Literal["daily", "weekly"]
Scope = Literal["self", "server"]


def parse_list(value: str, *, numeric: bool = False) -> list[Any]:
    if value.strip().lower() == "all":
        return [] if numeric else list(SECTIONS)
    values = [s.strip() for s in value.split(",") if s.strip()]
    return [int(v) for v in values] if numeric else values


def can_send(channel: Any) -> bool:
    if not isinstance(channel, discord.TextChannel) or channel.guild.me is None:
        return False
    p = channel.permissions_for(channel.guild.me)
    return bool(p.view_channel and p.send_messages and p.embed_links)


class SummaryCog(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.service = Summaries(bot.store)
        # Saving a preference cannot race an in-flight delivery on either interface.
        self.lock = asyncio.Lock()
        self.worker_started_at = int(time.time())
        self.last_success_at: int | None = None
        self.preview_sent: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.deliver_summaries.start()

    async def cog_unload(self) -> None:
        self.deliver_summaries.cancel()
        await self.service.close()

    async def send_preview(
        self,
        guild: Any,
        member: Any,
        cadence: str,
        scope: str,
        config: dict[str, Any],
        *,
        channel_alert: bool = False,
        offset: int = 1,
    ) -> dict[str, Any]:
        owner = 0 if channel_alert else member.id
        p = validate(cadence, scope, config, owner)
        channel = None
        if channel_alert:
            if not member.guild_permissions.manage_guild:
                raise PermissionError("Manage Server is required to test channel summaries.")
            channel = guild.get_channel(p["channel_id"])
            if not can_send(channel) or not channel.permissions_for(member).view_channel:
                raise PermissionError("Choose an accessible channel where Slopbot can send embeds.")
        key = (guild.id, member.id)
        now = time.monotonic()
        last_sent = self.preview_sent.get(key)
        if last_sent is not None and last_sent > now - 60:
            raise ValueError("Wait one minute before sending another test report.")
        self.preview_sent[key] = now
        self.preview_sent = {k: v for k, v in self.preview_sent.items() if v > now - 60}
        report = await self.service.report(
            guild.id,
            await self.accounts(guild.id, member.id, scope),
            cadence,
            p,
            int(time.time()),
            offset,
        )
        embed = self.embed(report, guild.name, scope)
        embed.title = "Test · " + (embed.title or "Summary")[:249]
        if channel is not None:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return dict(delivered=True, detail="Test sent to the selected channel.")
        result = await send_to_destinations(
            bot=self.bot,
            store=self.bot.store,
            telegram=self.bot.telegram,
            guild_id=guild.id,
            recipient_id=member.id,
            discord_embed=embed,
            telegram_text="Test summary\n" + report_text(report),
        )
        return dict(
            delivered=result.delivered,
            detail=result.detail,
            receipts={k: dict(status=s, detail=d) for k, s, d in result.receipts},
        )

    async def accounts(self, gid: int, owner: int, scope: str) -> dict[str, str]:
        guild = self.bot.get_guild(gid)
        result = {}
        for a in await self.bot.store.list_all_rocket_league_accounts(guild_id=gid):
            if scope == "self" and a.discord_user_id != owner:
                continue
            if gid > 0 and (
                guild is None
                or (a.discord_user_id > 0 and guild.get_member(a.discord_user_id) is None)
            ):
                continue
            result[a.player_id] = a.player_name
        return result

    async def configure(
        self,
        guild: Any,
        member: Any,
        cadence: str,
        scope: str,
        config: dict[str, Any],
        *,
        channel_alert: bool = False,
        replace_instant: bool = False,
        schedule_id: int = 0,
    ) -> dict[str, Any]:
        if not -(2**63) < schedule_id <= 0 or (schedule_id and not channel_alert):
            raise ValueError("Invalid channel schedule.")
        owner = schedule_id if channel_alert else member.id
        validate(cadence, scope, config, owner)
        if channel_alert:
            if not member.guild_permissions.manage_guild:
                raise PermissionError("Manage Server is required to configure channel summaries.")
            channel = guild.get_channel(config["channel_id"]) if config["channel_id"] else None
            if config["enabled"] and (
                channel is None
                or not can_send(channel)
                or not channel.permissions_for(member).view_channel
            ):
                raise PermissionError(
                    "Choose an accessible text channel where Slopbot can view, "
                    "send messages, and embed links."
                )
        async with self.lock:
            return await self.service.save(
                guild.id,
                owner,
                cadence,
                scope,
                config,
                int(time.time()),
                replace_instant=replace_instant,
            )

    @app_commands.command(
        name="summary", description="Preview a daily or weekly Rocket League report"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        periods_ago="0: in progress; 1: last completed; up to 4 weeks or 30 days",
        sections="Comma-separated section keys, or all; see /summaryalerts",
        playlists="Comma-separated playlist IDs (10,11,13,27,28,29,30), or all",
    )
    async def summary(
        self,
        interaction: discord.Interaction,
        cadence: Cadence = "daily",
        scope: Scope = "self",
        periods_ago: app_commands.Range[int, 0, 30] = 1,
        timezone: str | None = None,
        sections: str | None = None,
        playlists: str | None = None,
        send_test: bool = False,
    ) -> None:
        if interaction.guild is None:
            raise ValueError("Use summaries in a server.")
        await interaction.response.defer(ephemeral=True)
        p = (
            await self.service.settings(interaction.guild.id, interaction.user.id, cadence, scope)
        )["config"]
        if timezone is not None:
            p["timezone"] = timezone
        if sections is not None:
            p["sections"] = parse_list(sections)
        if playlists is not None:
            p["playlists"] = parse_list(playlists, numeric=True)
        try:
            accounts = await self.accounts(interaction.guild.id, interaction.user.id, scope)
            r = await self.service.report(
                interaction.guild.id, accounts, cadence, p, int(time.time()), periods_ago
            )
            if send_test:
                result = await self.send_preview(
                    interaction.guild, interaction.user, cadence, scope, p, offset=periods_ago
                )
                await interaction.followup.send(result["detail"], ephemeral=True)
                return
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            embed=self.embed(r, interaction.guild.name, scope), ephemeral=True
        )

    @app_commands.command(
        name="summaryalerts", description="Configure personal daily or weekly summaries"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        sections="Comma-separated keys or all; omit to keep saved sections",
        playlists="Comma-separated playlist IDs or all",
        weekday="Weekly period starts/delivers: 0 Monday through 6 Sunday",
        minimum_games="0 permits event-only reports; skip_empty still applies",
    )
    async def summary_alerts(
        self,
        interaction: discord.Interaction,
        cadence: Cadence = "daily",
        scope: Scope = "self",
        enabled: bool | None = None,
        timezone: str | None = None,
        hour: app_commands.Range[int, 0, 23] | None = None,
        minute: app_commands.Range[int, 0, 59] | None = None,
        weekday: app_commands.Range[int, 0, 6] | None = None,
        minimum_games: app_commands.Range[int, 0, 10000] | None = None,
        skip_empty: bool | None = None,
        top: app_commands.Range[int, 1, 15] | None = None,
        sections: str | None = None,
        playlists: str | None = None,
        minimum_streak: app_commands.Range[int, 1, 1000] | None = None,
        minimum_peak_gain: app_commands.Range[int, 0, 1000] | None = None,
        player_order: Literal["activity", "gains", "losses", "name"] | None = None,
    ) -> None:
        await self._configure_command(
            interaction,
            cadence,
            scope,
            False,
            None,
            enabled=enabled,
            timezone=timezone,
            hour=hour,
            minute=minute,
            weekday=weekday,
            minimum=minimum_games,
            skip_empty=skip_empty,
            top=top,
            sections=sections,
            playlists=playlists,
            minimum_streak=minimum_streak,
            minimum_peak_gain=minimum_peak_gain,
            player_order=player_order,
        )

    @app_commands.command(
        name="summarychannel", description="Configure server daily or weekly channel summaries"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        sections="Comma-separated keys or all; omit to keep saved sections",
        playlists="Comma-separated playlist IDs or all",
        weekday="Weekly period starts/delivers: 0 Monday through 6 Sunday",
        minimum_games="0 permits event-only reports; skip_empty still applies",
    )
    async def summary_channel(
        self,
        interaction: discord.Interaction,
        cadence: Cadence = "daily",
        enabled: bool | None = None,
        channel: discord.TextChannel | None = None,
        timezone: str | None = None,
        hour: app_commands.Range[int, 0, 23] | None = None,
        minute: app_commands.Range[int, 0, 59] | None = None,
        weekday: app_commands.Range[int, 0, 6] | None = None,
        minimum_games: app_commands.Range[int, 0, 10000] | None = None,
        skip_empty: bool | None = None,
        top: app_commands.Range[int, 1, 15] | None = None,
        sections: str | None = None,
        playlists: str | None = None,
        minimum_streak: app_commands.Range[int, 1, 1000] | None = None,
        minimum_peak_gain: app_commands.Range[int, 0, 1000] | None = None,
        player_order: Literal["activity", "gains", "losses", "name"] | None = None,
        additional: bool = False,
        replace_instant: bool = False,
        send_test: bool = False,
    ) -> None:
        if additional and channel is None:
            await interaction.response.send_message(
                "Choose a channel to select its additional schedule.", ephemeral=True
            )
            return
        await self._configure_command(
            interaction,
            cadence,
            "server",
            True,
            channel,
            replace_instant=replace_instant,
            schedule_id=-channel.id if additional and channel else 0,
            send_test=send_test,
            enabled=enabled,
            timezone=timezone,
            hour=hour,
            minute=minute,
            weekday=weekday,
            minimum=minimum_games,
            skip_empty=skip_empty,
            top=top,
            sections=sections,
            playlists=playlists,
            minimum_streak=minimum_streak,
            minimum_peak_gain=minimum_peak_gain,
            player_order=player_order,
        )

    async def _configure_command(
        self,
        interaction: discord.Interaction,
        cadence: str,
        scope: str,
        channel_alert: bool,
        channel: Any,
        replace_instant: bool = False,
        schedule_id: int = 0,
        send_test: bool = False,
        **changes: Any,
    ) -> None:
        guild = interaction.guild
        if guild is None or (channel_alert and not interaction.permissions.manage_guild):
            await interaction.response.send_message(
                "Manage Server is required for channel summaries; "
                "use personal alerts inside a server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        owner = schedule_id if channel_alert else interaction.user.id
        settings = await self.service.settings(guild.id, owner, cadence, scope)
        p = settings["config"]
        try:
            edited = (
                any(v is not None for v in changes.values())
                or channel is not None
                or replace_instant
            )
            for key, value in changes.items():
                if value is not None:
                    p[key] = (
                        parse_list(value, numeric=key == "playlists")
                        if key in {"sections", "playlists"}
                        else value
                    )
            if channel is not None:
                p["channel_id"] = channel.id
            if send_test:
                result = await self.send_preview(
                    guild, interaction.user, cadence, scope, p, channel_alert=channel_alert
                )
                await interaction.followup.send(result["detail"], ephemeral=True)
                return
            if edited:
                settings = await self.configure(
                    guild,
                    interaction.user,
                    cadence,
                    scope,
                    p,
                    channel_alert=channel_alert,
                    replace_instant=replace_instant,
                    schedule_id=schedule_id,
                )
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"{cadence.title()} · {scope} · {'enabled' if p['enabled'] else 'disabled'}\n"
            f"{p['hour']:02}:{p['minute']:02} {p['timezone']} · minimum {p['minimum']} games · "
            f"skip empty: {p['skip_empty']} · up to {p['top']} highlights per section\n"
            f"Sections: {', '.join(p['sections'])}\n"
            f"Playlists: {', '.join(map(str, p['playlists'])) or 'all'}\n"
            + (f"Next delivery: <t:{settings['next_at']}:F>\n" if settings["next_at"] else "")
            + "Daily reports cover the previous calendar day; weekly reports cover the previous "
            "seven-day period ending on your selected weekday. Personal delivery follows "
            "/notifications. Website Reports offers checkboxes, previews, and period history.\n"
            + "Available section keys: "
            + ", ".join(SECTIONS),
            ephemeral=True,
        )

    def embed(self, report: dict[str, Any], guild_name: str, scope: str) -> discord.Embed:
        def clean(s: str) -> str:
            return discord.utils.escape_markdown(discord.utils.escape_mentions(s))

        description = clean(report_text(report))
        if len(description) > 3900:
            description = description[:3800].rsplit("\n", 1)[0] + "\nMore details in Reports."
        embed = discord.Embed(
            title=clean(f"{guild_name[:100]} · {report['cadence']} {scope} summary")[:256],
            description=description,
            color=discord.Color.blurple(),
        )
        website = getattr(getattr(self.bot, "web", None), "settings", None)
        if website:
            from urllib.parse import urlencode

            query = dict(
                guild_id=report.get("guild_id", ""),
                cadence=report["cadence"],
                scope=scope,
                timezone=report["timezone"],
                date=report["start"],
            )
            for key in (
                "weekday",
                "sections",
                "playlists",
                "minimum_streak",
                "minimum_peak_gain",
                "player_order",
            ):
                value = report.get("config", {}).get(key)
                if value is not None:
                    query[key] = ",".join(map(str, value)) if isinstance(value, list) else value
            if report.get("delivery_id") and int(report.get("owner_id", "0")) > 0:
                query.update(delivery=report["delivery_id"], owner=report["owner_id"])
            embed.url = website.origin + "/#reports?" + urlencode(query)
        return embed

    @tasks.loop(seconds=60)
    async def deliver_summaries(self) -> None:
        try:
            await self.deliver(int(time.time()))
            self.last_success_at = int(time.time())
        except Exception:
            LOGGER.exception("Summary worker failed; the next minute will retry")

    @deliver_summaries.before_loop
    async def before_delivery(self) -> None:
        await self.bot.wait_until_ready()

    async def deliver(self, now: int) -> None:
        if not self.bot.is_ready():
            return
        for job in await self.service.due(now):
            async with self.lock:
                try:
                    await self._deliver_one(job, now)
                except Exception:
                    LOGGER.exception("Summary delivery %s failed", job["id"])
                    await self.service.finish(job, "retry", now)

    async def _deliver_one(self, job: dict[str, Any], now: int) -> None:
        gid, owner, cadence, scope = (job[k] for k in ("guild_id", "owner_id", "cadence", "scope"))
        setting = await self.service.settings(gid, owner, cadence, scope)
        p, guild = setting["config"], self.bot.get_guild(gid)
        if not guild or not p["enabled"] or setting["revision"] != job["revision"]:
            await self.service.finish(
                job, "cancelled", now, detail="Server or schedule is no longer active."
            )
            return
        if owner > 0:
            try:
                await guild.fetch_member(owner)
            except (discord.NotFound, discord.Forbidden):
                await self.service.finish(
                    job,
                    "cancelled",
                    now,
                    detail="Recipient is no longer an accessible server member.",
                )
                return
        else:
            channel = guild.get_channel(p["channel_id"])
            if not can_send(channel):
                await self.service.finish(
                    job,
                    "blocked",
                    now,
                    detail="Destination channel is missing or Slopbot cannot send embeds.",
                )
                return
        accounts = await self.accounts(gid, owner, scope)
        report = await self.service.snapshot(gid, owner, job["id"])
        if report and not set(report.get("profile_ids", [])) <= set(accounts):
            await self.service.finish(
                job, "cancelled", now, detail="Tracked account access changed."
            )
            return
        if report is None:
            report = await self.service.report(gid, accounts, cadence, p, job["due_at"])
        report.update(scope=scope, owner_id=str(owner), delivery_id=job["id"])
        report = await self.service.prepare(job, report)
        relevant_events = sum(h["count"] for h in report["highlights"].values())
        has_activity = relevant_events or (
            report["totals"]["games"]
            and any(
                k in p["sections"]
                for k in ("results", "mmr", "players", "playlists", "comparison", "activity")
            )
        )
        if report["totals"]["games"] < p["minimum"] or (p["skip_empty"] and not has_activity):
            await self.service.finish(
                job,
                "skipped",
                now,
                detail="Below minimum games or no activity in selected sections.",
            )
            return
        embed = self.embed(report, guild.name, scope)
        receipts = await self.service.receipts(job)
        detail = "Delivered"
        if owner > 0:
            result = await send_to_destinations(
                bot=self.bot,
                store=self.bot.store,
                telegram=self.bot.telegram,
                guild_id=gid,
                recipient_id=owner,
                discord_embed=embed,
                telegram_text=f"{embed.title}\n{report_text(report)}",
                completed=frozenset(k for k, v in receipts.items() if v["status"] == "delivered"),
            )
            receipts.update({k: dict(status=s, detail=d) for k, s, d in result.receipts})
            status = "retry" if result.retryable else "delivered" if result.delivered else "blocked"
            detail = result.detail
        else:
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                status = "delivered"
            except discord.Forbidden:
                status = "blocked"
                detail = "Channel permissions prevent sending."
            except discord.HTTPException:
                status = "retry"
                detail = "Discord delivery failed; retry scheduled."
            receipts["channel"] = dict(status=status, detail=detail)
        await self.service.finish(job, status, now, report, detail=detail, receipts=receipts)
