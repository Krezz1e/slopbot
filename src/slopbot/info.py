from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands


@dataclass(frozen=True, slots=True)
class CommandInfo:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class InfoSection:
    title: str
    body: str
    commands: tuple[CommandInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityTopic:
    label: str
    summary: str
    introduction: str
    sections: tuple[InfoSection, ...]


CAPABILITY_TOPICS: dict[str, CapabilityTopic] = {
    "overview": CapabilityTopic(
        "Overview",
        "A quick tour of everything Slopbot can do.",
        (
            "Slopbot combines Discord activity alerts, private notification tools, and extensive "
            "Rocket League tracking. Choose a topic below when running `/info` again for setup "
            "instructions, limits, behavior, and the commands involved."
        ),
        (
            InfoSection(
                "About this guide",
                (
                    "Every response is visible only to you. Available Rocket League and Telegram "
                    "features depend on which services the server owner has configured."
                ),
                (CommandInfo("info", "Open this guide and select a capability topic."),),
            ),
        ),
    ),
    "voice_alerts": CapabilityTopic(
        "Voice-channel alerts",
        "Get notified when a voice channel becomes active.",
        (
            "Create private alerts for voice and stage channels you can see. Slopbot monitors "
            "human occupancy and notifies you when activity crosses your chosen threshold."
        ),
        (
            InfoSection(
                "Creating and managing alerts",
                (
                    "Choose a channel, a threshold from 2-25 people, and whether the alert fires "
                    "when activity rises to the threshold, drops below it, or both. The same panel "
                    "lets you edit or delete saved alerts; each member may keep 25 per server."
                ),
                (CommandInfo("alerts", "Open the private voice-alert management panel."),),
            ),
            InfoSection(
                "Notification behavior",
                (
                    "Alerts list the current human members and, when Discord provides it, the game "
                    "each person is playing. Bots do not count. An alert fires only when the "
                    "threshold is crossed, and Slopbot will not alert you about a channel you are "
                    "already in. Access is checked again before delivery."
                ),
            ),
        ),
    ),
    "engagement": CapabilityTopic(
        "Keywords & get-on requests",
        "Follow conversations and invite friends to play.",
        (
            "Use private keyword alerts to follow selected conversations, or send immediate and "
            "scheduled get-on requests to friends without posting publicly."
        ),
        (
            InfoSection(
                "Keyword notifications",
                (
                    "Save up to 25 case-insensitive words or phrases and choose up to 25 visible "
                    "text channels. Alerts can run always or only while you appear offline. They "
                    "include the author, channel, matched phrases, message text, and a jump link; "
                    "your own messages never notify you."
                ),
                (
                    CommandInfo(
                        "configurekeywords",
                        "Create, edit, enable, or disable your keyword profile.",
                    ),
                ),
            ),
            InfoSection(
                "Get-on requests",
                (
                    "Select up to ten members and optionally add a message or delay delivery by as "
                    "much as seven days. Scheduled requests survive restarts. Senders have a "
                    "30-second cooldown and may keep up to 20 requests awaiting delivery."
                ),
                (
                    CommandInfo("geton", "Choose recipients and ask them to get on now or later."),
                    CommandInfo(
                        "getonnotifications", "View or change whether you accept get-on requests."
                    ),
                ),
            ),
        ),
    ),
    "delivery": CapabilityTopic(
        "Notification delivery",
        "Choose where Slopbot sends private alerts.",
        (
            "Discord DMs are the default. Telegram and subscribed PWA devices can be used alone "
            "or in any combination."
        ),
        (
            InfoSection(
                "Managing delivery",
                (
                    "Your preference is saved separately for each Discord server and is shared "
                    "by voice, keyword, get-on, tournament, item-shop, and personal status alerts. "
                    "Run the command without a choice to inspect the current setting."
                ),
                (
                    CommandInfo(
                        "notifications",
                        "View or choose Discord, Telegram, PWA push, or a combination.",
                    ),
                    CommandInfo(
                        "testnotification",
                        "Send a test through every currently configured destination.",
                    ),
                ),
            ),
            InfoSection(
                "Telegram linking and fallback",
                (
                    "Choosing Telegram for the first time creates a private, single-use link that "
                    "expires after ten minutes. If Telegram is temporarily unavailable for a "
                    "Telegram-only preference, Slopbot attempts to fall back to a Discord DM."
                ),
            ),
        ),
    ),
    "rocket_league": CapabilityTopic(
        "Rocket League stats",
        "Track ranks, results, peaks, streaks, and leaderboards.",
        (
            "Slopbot tracks linked members and administrator-added Epic accounts using the "
            "optional Rocket League service. Tracking begins after an account is added and cannot "
            "backfill earlier matches."
        ),
        (
            InfoSection(
                "Accounts",
                (
                    "Members can link one verified platform account per server. Manage Server "
                    "users may link for another member or track an unlinked Epic account. "
                    "Verification confirms that the profile exists, but does not prove ownership."
                ),
                (
                    CommandInfo("linkrocketleague", "Link a platform account to a Discord member."),
                    CommandInfo("unlinkrocketleague", "Remove a member's linked account."),
                    CommandInfo("addepicaccount", "Admin: track an Epic account without a member."),
                    CommandInfo("removeepicaccount", "Admin: stop tracking an unlinked account."),
                ),
            ),
            InfoSection(
                "Player statistics",
                (
                    "Inspect current MMR and rank, all-time observed peaks, or a combined player "
                    "overview. Playlist choices include competitive and supported extra modes. "
                    "Match and MMR history retain 30 days. Ambiguous missed matches appear as gaps "
                    "instead of being guessed."
                ),
                (
                    CommandInfo(
                        "rank current", "Show current ranks and MMR for a tracked account."
                    ),
                    CommandInfo("rank peak", "Show its highest ranks and MMR observed by Slopbot."),
                    CommandInfo(
                        "sessions",
                        "View server members' current or recent sessions and open session history.",
                    ),
                    CommandInfo("sessionpreferences", "Set personal session cutoff and summaries."),
                    CommandInfo("matchhistory", "Show up to 30 days of observed results."),
                    CommandInfo("mmrhistory", "Graph up to 30 days of observed playlist MMR."),
                    CommandInfo("movers", "Show the server's largest recent MMR changes."),
                    CommandInfo(
                        "today", "Show the latest 1-100 server-wide tracked activity events."
                    ),
                    CommandInfo(
                        "playeroverview",
                        "Open ranks, peaks, permanent streak records, club, and MMR history.",
                    ),
                ),
            ),
            InfoSection(
                "Daily and weekly reports",
                "Choose personal or whole-server activity, calendar timezone, playlists, and "
                "independent content sections. Reports use retained observations, never guessed "
                "results. The website Reports tab includes period history and alert settings.",
                (
                    CommandInfo("summary", "Preview a daily or weekly report privately."),
                    CommandInfo(
                        "summaryalerts", "Configure personal daily/weekly report delivery."
                    ),
                ),
            ),
            InfoSection(
                "Server leaderboards",
                (
                    "Boards support 1-20 rows. Current and peak boards rank tracked players by a "
                    "selected playlist. Permanent streak boards cover completed competitive-only "
                    "or all-ranked-mode win and loss streaks across playlists; session settings "
                    "never change those records."
                ),
                (
                    CommandInfo("leaderboard", "Rank accounts by current playlist MMR."),
                    CommandInfo(
                        "peakleaderboard", "Rank accounts by highest observed playlist MMR."
                    ),
                    CommandInfo(
                        "streakleaderboard", "Rank permanent completed win or loss streaks."
                    ),
                ),
            ),
        ),
    ),
    "rocket_league_events": CapabilityTopic(
        "Tournaments & item shop",
        "Browse upcoming events and receive personalized reminders.",
        (
            "Browse Rocket League's scheduled tournaments and item shop, then configure reminders "
            "or watches. These tools do not require your Rocket League account to be linked."
        ),
        (
            InfoSection(
                "Tournament schedule and reminders",
                (
                    "View the next ten tournaments in a chosen region with times localized by "
                    "Discord. Personal reminders filter by region, team sizes, and modes with a "
                    "5-120 minute lead. Manage Server users can configure public reminders in a "
                    "text channel, including a one-time Remind me button."
                ),
                (
                    CommandInfo("tournaments", "Show the upcoming regional tournament schedule."),
                    CommandInfo("tournamentalerts", "Configure your private tournament reminders."),
                    CommandInfo(
                        "tournamentchannel", "Admin: configure reminders in a server text channel."
                    ),
                ),
            ),
            InfoSection(
                "Item shop browsing",
                (
                    "Search the current catalogue by text and optional maximum price. Slopbot "
                    "keeps 30 days of additions, updates, and removals. It can show its latest "
                    "saved catalogue during a temporary service outage."
                ),
                (
                    CommandInfo("itemshop current", "Browse and filter up to ten current items."),
                    CommandInfo("itemshop changes", "Review recent catalogue changes."),
                ),
            ),
            InfoSection(
                "Item shop watches",
                (
                    "Keep up to 25 private watches per server using text and/or maximum-price "
                    "filters. An empty filter watches every item. Overlapping watches are combined "
                    "so the same change is delivered only once."
                ),
                (
                    CommandInfo("itemshop watch", "Create a notification watch."),
                    CommandInfo("itemshop watches", "List your saved watches and their numbers."),
                    CommandInfo("itemshop unwatch", "Delete one watch by its number."),
                ),
            ),
        ),
    ),
    "administration": CapabilityTopic(
        "Server administration",
        "Control announcements, command access, and service monitoring.",
        (
            "Members with Manage Server can configure public Rocket League announcements, control "
            "where result commands are used, and monitor Slopbot's services. Configuration and "
            "status responses are private."
        ),
        (
            InfoSection(
                "Rocket League announcements",
                (
                    "Route every /today event category independently to text channels. Session "
                    "streaks use the configurable inactivity timer; permanent all-time streaks "
                    "ignore it. Multiple event types can share a destination."
                ),
                (
                    CommandInfo("setup sessions", "Set server session cutoff and summary channel."),
                    CommandInfo("summarychannel", "Configure daily/weekly server channel digests."),
                    CommandInfo(
                        "setup events notifications",
                        "Configure /today routes and the session inactivity timer.",
                    ),
                ),
            ),
            InfoSection(
                "Result-command policy",
                (
                    "Set each shareable result command's default to Public or Only me. An "
                    "allowlist can restrict a command to selected text channels; threads inherit "
                    "their parent channel. A per-use visibility choice never bypasses the policy."
                ),
                (
                    CommandInfo("setup commands visibility", "Set a command's default visibility."),
                    CommandInfo("setup commands allow", "Add a text channel to its allowlist."),
                    CommandInfo("setup commands disallow", "Remove one allowed text channel."),
                    CommandInfo("setup commands unrestricted", "Allow it in every channel again."),
                    CommandInfo(
                        "setup commands status", "Inspect its visibility and channel access."
                    ),
                ),
            ),
            InfoSection(
                "Health and configuration",
                (
                    "Inspect gateway, database, Telegram, Rocket League, and worker health. Status "
                    "monitoring sends only meaningful issue, change, and recovery reports—not "
                    "repetitive healthy messages. A server summary omits personal member data."
                ),
                (
                    CommandInfo("slopbotstatus", "View current service and worker health."),
                    CommandInfo(
                        "reauthorizerocketleague",
                        "Start the administrator-only Epic device login when authorization "
                        "expires.",
                    ),
                    CommandInfo("slopbotstatusalerts", "Configure your private health alerts."),
                    CommandInfo(
                        "slopbotstatuschannel", "Configure health alerts in a text channel."
                    ),
                    CommandInfo("summarizeserver", "Summarize server-level Slopbot configuration."),
                ),
            ),
        ),
    ),
}

INFO_TOPIC_CHOICES = [
    app_commands.Choice(name=topic.label, value=value) for value, topic in CAPABILITY_TOPICS.items()
]


def documented_command_names() -> set[str]:
    return {
        command.name
        for topic in CAPABILITY_TOPICS.values()
        for section in topic.sections
        for command in section.commands
    }


def validate_info_command_coverage(command_names: Iterable[str]) -> None:
    actual = set(command_names)
    documented = documented_command_names()
    if actual == documented:
        return
    missing = sorted(actual - documented)
    stale = sorted(documented - actual)
    problems = []
    if missing:
        problems.append(f"not documented in /info: {', '.join(missing)}")
    if stale:
        problems.append(f"documented but not registered: {', '.join(stale)}")
    raise RuntimeError("Slash-command catalog is out of date (" + "; ".join(problems) + ")")


def _section_value(section: InfoSection) -> str:
    command_lines = "\n".join(
        f"`/{command.name}` — {command.description}" for command in section.commands
    )
    return f"{section.body}\n\n{command_lines}" if command_lines else section.body


def capability_embed(topic_value: str) -> discord.Embed:
    topic = CAPABILITY_TOPICS[topic_value]
    embed = discord.Embed(
        title=f"Slopbot info — {topic.label}",
        description=topic.introduction,
        colour=discord.Colour.blurple(),
    )
    if topic_value == "overview":
        for value, detail in CAPABILITY_TOPICS.items():
            if value != "overview":
                embed.add_field(name=detail.label, value=detail.summary, inline=False)
    else:
        for section in topic.sections:
            embed.add_field(name=section.title, value=_section_value(section), inline=False)
    embed.set_footer(text="Run /info again and choose a topic to explore another capability.")
    return embed


class InfoCog(commands.Cog):
    @app_commands.command(name="info", description="Learn what Slopbot can do")
    @app_commands.describe(topic="Capability you want to learn about")
    @app_commands.choices(topic=INFO_TOPIC_CHOICES)
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction, topic: app_commands.Choice[str]) -> None:
        await interaction.response.send_message(
            embed=capability_embed(topic.value),
            ephemeral=True,
        )
