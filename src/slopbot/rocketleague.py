from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from slopbot.charts import render_mmr_history_chart
from slopbot.database import RocketLeagueAccountConflictError, SubscriptionStore
from slopbot.engagement import send_to_destinations
from slopbot.models import (
    CommandPolicy,
    ItemShopItem,
    ItemShopPrice,
    ResponseVisibility,
    RocketLeagueAccount,
    RocketLeagueEvent,
    RocketLeaguePeakMMR,
    RocketLeagueRecord,
    RocketLeagueTrackingConfig,
    TournamentChannelAlert,
)
from slopbot.rank_estimates import RankStepEstimates, estimate_rank_steps, format_rank_steps
from slopbot.sessions import Sessions, summary
from slopbot.ui import EmbedPagesView
from slopbot.visibility import (
    RESPONSE_VISIBILITY_CHOICES,
    SHAREABLE_COMMAND_CHOICES,
    SHAREABLE_COMMANDS,
    defer_result_response,
)

if TYPE_CHECKING:
    from slopbot.bot import SlopBot

LOGGER = logging.getLogger(__name__)
MAX_LEADERBOARD_SIZE = 20
MAX_SERVICE_PLAYERS = 500
MAX_MATCH_HISTORY_MARKERS = 100
ROCKET_LEAGUE_HISTORY_DAYS = 30
PLAYER_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")

GAME_MODES: dict[str, tuple[str, int]] = {
    "1v1": ("Ranked Duel (1v1)", 10),
    "2v2": ("Ranked Doubles (2v2)", 11),
    "3v3": ("Ranked Standard (3v3)", 13),
    "hoops": ("Hoops", 27),
    "rumble": ("Rumble", 28),
    "dropshot": ("Dropshot", 29),
    "snowday": ("Snow Day", 30),
}
PLAYLIST_NAMES = {playlist: label for label, playlist in GAME_MODES.values()}
STANDARD_RANK_MODES = ("1v1", "2v2", "3v3")
FALLBACK_RANKED_PLAYLIST_DATA = (
    (10, "RankedDuel"),
    (11, "RankedDoubles"),
    (13, "RankedStandard"),
    (27, "Hoops"),
    (28, "Rumble"),
    (29, "Dropshot"),
    (30, "SnowDay"),
)
EVENT_NOTIFICATION_LABELS = {
    "match": "Matches and result gaps",
    "rank_change": "Rank changes",
    "peak_mmr": "New MMR peaks",
    "session_streak": "Session streaks",
    "all_time_streak": "All-time streaks",
    "leaderboard_movement": "Rank leaderboard movements",
    "streak_leaderboard_competitive": "Competitive streak leaderboard entries",
    "streak_leaderboard_all_ranked": "All-ranked streak leaderboard entries",
}
EVENT_NOTIFICATION_CHOICES = [
    app_commands.Choice(name=label, value=event_key)
    for event_key, label in EVENT_NOTIFICATION_LABELS.items()
]

PLATFORMS: dict[str, tuple[str, str]] = {
    "epic": ("Epic Games", "Epic"),
    "steam": ("Steam", "Steam"),
    "playstation": ("PlayStation", "PS4"),
    "xbox": ("Xbox", "XboxOne"),
    "switch": ("Nintendo Switch", "Switch"),
}


class RocketLeagueServiceError(RuntimeError):
    """Raised when the local Rocket League service cannot fulfill a request."""


@dataclass(frozen=True, slots=True)
class RocketLeagueStatus:
    connected: bool
    state: str


@dataclass(frozen=True, slots=True)
class RocketLeagueAuthorization:
    verification_uri: str
    user_code: str
    expires_at: int


def rocket_league_status_detail(status: RocketLeagueStatus | None) -> str:
    if status is not None and status.state == "authorization_required":
        return "Reauthorization required; use /reauthorizerocketleague"
    return "Unavailable; check service logs"


@dataclass(frozen=True, slots=True)
class RocketLeagueProfile:
    player_id: str
    player_name: str


@dataclass(frozen=True, slots=True)
class RocketLeagueSkill:
    player_id: str
    mmr: float
    tier: int
    division: int
    rank: str
    win_streak: int = 0
    matches_played: int = 0
    placement_matches_played: int = 0
    playlist: int = 0


@dataclass(frozen=True, slots=True)
class RocketLeagueSeasonRewards:
    level: int
    wins: int


@dataclass(frozen=True, slots=True)
class RocketLeagueClubMember:
    player_id: str
    player_name: str
    role_id: int


@dataclass(frozen=True, slots=True)
class RocketLeagueClubBadge:
    stat: str
    badge: int


@dataclass(frozen=True, slots=True)
class RocketLeagueClub:
    id: int
    name: str
    tag: str
    primary_color: int
    accent_color: int
    title: str
    owner_player_id: str
    verified: bool
    members: tuple[RocketLeagueClubMember, ...]
    badges: tuple[RocketLeagueClubBadge, ...]


@dataclass(frozen=True, slots=True)
class RocketLeaguePlayerDetails:
    skills: tuple[RocketLeagueSkill, ...]
    rewards: RocketLeagueSeasonRewards
    club: RocketLeagueClub | None


@dataclass(frozen=True, slots=True)
class RocketLeagueActivity:
    player_id: str
    playlist: int
    mmr: float
    tier: int
    division: int
    rank: str
    win_streak: int
    matches_played: int


@dataclass(frozen=True, slots=True)
class RocketLeaguePlaylist:
    id: int
    node_id: str
    label: str


def playlist_label(playlist_id: int, node_id: str = "") -> str:
    known = PLAYLIST_NAMES.get(playlist_id)
    if known is not None:
        return known
    cleaned = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", node_id.replace("_", " ")).strip()
    return f"{cleaned} ({playlist_id})" if cleaned else f"Ranked playlist {playlist_id}"


def fallback_ranked_playlists() -> tuple[RocketLeaguePlaylist, ...]:
    return tuple(
        RocketLeaguePlaylist(playlist_id, node_id, playlist_label(playlist_id, node_id))
        for playlist_id, node_id in FALLBACK_RANKED_PLAYLIST_DATA
    )


@dataclass(frozen=True, slots=True)
class RocketLeagueTournament:
    id: int
    title: str
    start_time: int
    team_size: int
    game_mode: int
    max_bracket_size: int
    rank_min: int
    rank_max: int
    region: str
    schedule_id: int


RankedEntry = tuple[
    int,
    RocketLeagueAccount,
    discord.Member | None,
    RocketLeagueSkill,
]


def ranked_skill_entries(
    accounts: list[RocketLeagueAccount],
    members: dict[int, discord.Member],
    fetched: list[RocketLeagueSkill],
) -> list[RankedEntry]:
    skills = {skill.player_id: skill for skill in fetched}
    sortable: list[tuple[RocketLeagueAccount, discord.Member | None, RocketLeagueSkill]] = []
    for account in accounts:
        member = members.get(account.discord_user_id)
        if member is None and not account.unlinked:
            continue
        skill = skills.get(
            account.player_id,
            RocketLeagueSkill(account.player_id, 0.0, 0, 0, "Unranked"),
        )
        sortable.append((account, member, skill))
    sortable.sort(
        key=lambda item: (
            -item[2].mmr,
            (item[1].display_name if item[1] else item[0].player_name).casefold(),
            item[0].discord_user_id,
        )
    )
    return [
        (position, account, member, skill)
        for position, (account, member, skill) in enumerate(sortable, 1)
    ]


def leaderboard_window(
    ranked: list[RankedEntry], *, size: int, caller_id: int
) -> tuple[list[RankedEntry], int | None]:
    visible = ranked[:size]
    caller = next(
        (entry for entry in ranked if entry[2] is not None and entry[2].id == caller_id),
        None,
    )
    if caller is None or caller in visible:
        return visible, None
    visible.append(caller)
    return visible, caller[0]


def match_history_value(markers: list[str]) -> str:
    """Keep the newest observations while preserving chronological display order."""
    visible = markers[-MAX_MATCH_HISTORY_MARKERS:]
    omitted = len(markers) - len(visible)
    prefix = f"… {omitted} older observations • " if omitted else ""
    return f"{prefix}{' '.join(visible)}"[:1024]


def rank_response(
    player_name: str,
    playlist_name: str,
    skill: RocketLeagueSkill,
    estimates: RankStepEstimates | None = None,
) -> str:
    safe_name = discord.utils.escape_markdown(discord.utils.escape_mentions(player_name))
    safe_rank = discord.utils.escape_markdown(discord.utils.escape_mentions(skill.rank))
    result = f"**{safe_name} — {playlist_name}**\n**{skill.mmr:,.0f} MMR** — **{safe_rank}**"
    outlook = format_rank_steps(estimates)
    return result + (f"\n{outlook}" if outlook else "")


def rocket_league_event_line(
    event: RocketLeagueEvent,
    *,
    player_name: str | None = None,
    playlist_name: str | None = None,
) -> str:
    safe_player = discord.utils.escape_markdown(
        discord.utils.escape_mentions(player_name or event.player_name)
    )
    safe_playlist = discord.utils.escape_markdown(
        discord.utils.escape_mentions(playlist_name or playlist_label(event.playlist))
    )
    prefix = f"<t:{event.occurred_at}:f> — **{safe_player}**"
    if event.event_type == "match":
        if event.result == "gap":
            return (
                f"{prefix} played **{event.count}** {safe_playlist} "
                "games whose results could not be inferred"
            )
        verb = "won" if event.result == "win" else "lost"
        game_word = "game" if event.count == 1 else "games"
        article_or_count = event.count if event.count > 1 else "a"
        return f"{prefix} **{verb}** {article_or_count} {safe_playlist} {game_word}"
    if event.event_type in {"rank_up", "rank_down"}:
        verb = "ranked up" if event.event_type == "rank_up" else "ranked down"
        before = discord.utils.escape_markdown(event.rank_before or "an earlier rank")
        after = discord.utils.escape_markdown(event.rank_after or "a new rank")
        return f"{prefix} **{verb}** in {safe_playlist}: {before} → **{after}**"
    if event.event_type == "peak_mmr":
        peak = f"{event.mmr_after:,.0f}" if event.mmr_after is not None else "unknown"
        return f"{prefix} reached a new {safe_playlist} peak of **{peak} MMR**"
    if event.event_type in {"streak_completed", "session_streak_completed"}:
        result = "win" if event.result == "win" else "loss"
        segment_scope = "competitive" if event.scope == "main" else "special-ranked"
        permanent_scope = "competitive" if event.scope == "main" else "all-ranked-mode"
        if event.total_count is not None and event.total_count > event.count:
            return (
                f"{prefix} continued their {permanent_scope} {result} streak with "
                f"**{event.count} more** during a {segment_scope} session segment "
                f"(**{event.total_count} total**)"
            )
        return (
            f"{prefix} completed a **{event.count}-game session {result} streak** "
            f"in {segment_scope} modes"
        )
    if event.event_type == "all_time_streak_completed":
        result = "win" if event.result == "win" else "loss"
        streak_scope = (
            "competitive 1v1, 2v2, and 3v3" if event.scope == "main" else "all ranked modes"
        )
        placement = (
            f" at **leaderboard #{event.position_after}**"
            if event.position_after is not None
            else ""
        )
        return (
            f"{prefix} ended an **{event.count}-game all-time {result} streak** "
            f"across {streak_scope}{placement}"
        )
    if event.event_type == "leaderboard_movement":
        if event.position_before is None:
            movement = f"entered at **#{event.position_after}**"
        else:
            movement = f"moved **#{event.position_before} → #{event.position_after}**"
        return f"{prefix} {movement} on the {safe_playlist} leaderboard"
    if event.event_type == "streak_leaderboard_movement":
        result = "win" if event.result == "win" else "loss"
        streak_scope = "competitive" if event.scope == "main" else "all-ranked-mode"
        return (
            f"{prefix} entered the {streak_scope} all-time {result} streak leaderboard "
            f"at **#{event.position_after}** with **{event.count} games**"
        )
    safe_type = discord.utils.escape_markdown(event.event_type.replace("_", " "))
    return f"{prefix} recorded **{safe_type}** in {safe_playlist}"


def peak_rank_response(
    player_name: str, playlist_name: str, mmr: float, rank: str, observed_at: int
) -> str:
    safe_name = discord.utils.escape_markdown(discord.utils.escape_mentions(player_name))
    safe_rank = discord.utils.escape_markdown(discord.utils.escape_mentions(rank))
    return (
        f"**{safe_name} — {playlist_name} peak**\n"
        f"**{mmr:,.0f} MMR** — **{safe_rank}**\nObserved <t:{observed_at}:D>"
    )


SEASON_REWARD_LEVELS = (
    "Unranked",
    "Bronze",
    "Silver",
    "Gold",
    "Platinum",
    "Diamond",
    "Champion",
    "Grand Champion",
    "Supersonic Legend",
)


def _safe_text(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def leaderboard_change_description(
    rows: list[tuple[int, str, float, int | None]],
) -> str:
    """Keep a complete small board in one card, with a clear limit for large servers."""
    lines: list[str] = []
    for position, name, mmr, old_position in rows[:25]:
        if old_position is None:
            movement = "new"
        elif old_position > position:
            movement = f"↑{old_position - position}"
        elif old_position < position:
            movement = f"↓{position - old_position}"
        else:
            movement = "—"
        safe_name = _safe_text(" ".join(name.split())[:64])
        line = f"**{position}.** {safe_name} — **{mmr:,.0f} MMR** ({movement})"
        if sum(map(len, lines)) + len(lines) + len(line) > 3700:
            break
        lines.append(line)
    if len(rows) > len(lines):
        lines.append(f"… {len(rows) - len(lines)} more players • Use /leaderboard for standings.")
    return "\n".join(lines) or "No tracked accounts are currently ranked."


def _streak_summary(streak: int) -> str:
    if streak > 0:
        return f"{streak}-game win streak"
    if streak < 0:
        return f"{abs(streak)}-game loss streak"
    return "no active streak"


def _placement_summary(skill: RocketLeagueSkill) -> str:
    if skill.tier > 0 or skill.placement_matches_played >= 10:
        return "placements complete"
    remaining = max(0, 10 - skill.placement_matches_played)
    return f"**{remaining}** placement game{'s' if remaining != 1 else ''} remaining"


def _reward_summary(rewards: RocketLeagueSeasonRewards) -> str:
    level = max(0, rewards.level)
    earned = SEASON_REWARD_LEVELS[min(level, len(SEASON_REWARD_LEVELS) - 1)]
    if level >= len(SEASON_REWARD_LEVELS) - 1:
        return f"Earned: **{earned}** — maximum reward level"
    target = SEASON_REWARD_LEVELS[level + 1]
    wins = max(0, min(rewards.wins, 10))
    return f"Earned: **{earned}** — **{wins}/10** wins toward {target}"


def club_details_embed(player_name: str, club: RocketLeagueClub) -> discord.Embed:
    safe_name = _safe_text(player_name)
    safe_club_name = _safe_text(club.name)
    safe_tag = _safe_text(club.tag)
    color_value = club.primary_color & 0xFFFFFF
    embed = discord.Embed(
        title=f"[{safe_tag}] {safe_club_name}",
        description=f"Rocket League club for **{safe_name}**",
        color=discord.Color(color_value) if color_value else discord.Color.blurple(),
    )
    owner = next(
        (member.player_name for member in club.members if member.player_id == club.owner_player_id),
        "Unknown",
    )
    identity_lines = [
        f"Owner: **{_safe_text(owner)}**",
        f"Members: **{len(club.members)}**",
        f"Colors: `#{club.primary_color & 0xFFFFFF:06X}` / `#{club.accent_color & 0xFFFFFF:06X}`",
    ]
    if club.title:
        identity_lines.append(f"Title: {_safe_text(club.title)}")
    if club.verified:
        identity_lines.append("Verified club")
    embed.add_field(name="Club details", value="\n".join(identity_lines), inline=False)
    roster = [
        f"{'👑 ' if member.player_id == club.owner_player_id else ''}"
        f"{_safe_text(member.player_name)}"
        for member in club.members
    ]
    embed.add_field(
        name="Roster",
        value="\n".join(roster)[:1024] or "No roster information available",
        inline=False,
    )
    badge_lines = [
        f"**{_safe_text(badge.stat)}:** level {badge.badge}"
        for badge in club.badges
        if badge.stat and badge.badge > 0
    ]
    embed.add_field(
        name="Badges",
        value="\n".join(badge_lines)[:1024] or "No club badges earned",
        inline=False,
    )
    return embed


def all_time_streaks_embed(
    player_name: str,
    records: list[tuple[RocketLeagueRecord, int]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{_safe_text(player_name)}'s all-time streaks",
        description=(
            "Permanent, cross-playlist records. Active streaks appear only after an opposite "
            "or unknown result ends them."
        ),
        color=discord.Color.gold(),
    )
    indexed = {
        (record.streak_class, record.result): (record, position) for record, position in records
    }
    for scope, label in (("main", "Competitive"), ("all", "All ranked modes")):
        lines: list[str] = []
        for result, result_label in (("win", "Wins"), ("loss", "Losses")):
            entry = indexed.get((scope, result))
            if entry is None:
                lines.append(f"{result_label}: —")
                continue
            record, position = entry
            lines.append(
                f"{result_label}: **{record.count} game{'s' if record.count != 1 else ''}** — "
                f"**server #{position}** — <t:{record.observed_at}:d>"
            )
        embed.add_field(name=label, value="\n".join(lines), inline=False)
    embed.set_footer(
        text="Competitive covers ranked 1v1/2v2/3v3; All ranked includes special modes."
    )
    return embed


def player_overview_embed(
    player_name: str,
    current_ranks: list[tuple[str, RocketLeagueSkill, int]],
    peak_ranks: list[tuple[str, RocketLeaguePeakMMR, int]],
    details: RocketLeaguePlayerDetails | None = None,
    estimates: dict[int, RankStepEstimates] | None = None,
) -> discord.Embed:
    safe_name = discord.utils.escape_markdown(discord.utils.escape_mentions(player_name))
    embed = discord.Embed(
        title=f"{safe_name}'s Rocket League overview",
        color=discord.Color.blurple(),
    )
    current_lines: list[str] = []
    for label, skill, position in current_ranks:
        current_lines.append(
            f"**{label}:** {skill.mmr:,.0f} MMR — {_safe_text(skill.rank)} — "
            f"**server #{position}**\n"
            f"{skill.matches_played:,} matches • "
            f"{_streak_summary(skill.win_streak)} • {_placement_summary(skill)}"
            + (
                f"\n{outlook}"
                if (outlook := format_rank_steps((estimates or {}).get(skill.playlist)))
                else ""
            )
        )
    peak_lines = [
        f"**{label}:** {peak.mmr:,.0f} MMR — "
        f"{discord.utils.escape_markdown(discord.utils.escape_mentions(peak.rank))} — "
        f"**server #{position}** — <t:{peak.observed_at}:d>"
        for label, peak, position in peak_ranks
    ]
    embed.add_field(
        name="Current competitive ranks",
        value="\n".join(current_lines) or "Currently unavailable",
        inline=False,
    )
    embed.add_field(
        name="Peak competitive ranks",
        value="\n".join(peak_lines) or "No peaks observed yet",
        inline=False,
    )
    if details is not None:
        embed.add_field(
            name="Season rewards",
            value=_reward_summary(details.rewards),
            inline=False,
        )
        if details.club is None:
            club_summary = "Not currently in a club"
        else:
            club = details.club
            club_summary = (
                f"**[{_safe_text(club.tag)}] {_safe_text(club.name)}** — "
                f"{len(club.members)} member{'s' if len(club.members) != 1 else ''}"
            )
            if club.verified:
                club_summary += " — verified"
        embed.add_field(name="Club", value=club_summary, inline=False)
    embed.set_footer(text="Use the dashboard menu for permanent all-time streak records.")
    return embed


class RocketLeagueService:
    """Authenticated client for Slopbot's loopback-only Go service."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def status(self) -> RocketLeagueStatus:
        data = await self._request("GET", "/v1/status")
        return RocketLeagueStatus(
            connected=data.get("connected") is True,
            state=str(data.get("state", "unknown")),
        )

    async def start_authorization(self) -> RocketLeagueAuthorization:
        data = await self._request("POST", "/v1/authorization")
        verification_uri = data.get("verification_uri")
        user_code = data.get("user_code")
        expires_at = data.get("expires_at")
        if (
            verification_uri != "https://www.epicgames.com/activate"
            or not isinstance(user_code, str)
            or re.fullmatch(r"[A-Z0-9]{4,16}", user_code) is None
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
        ):
            raise RocketLeagueServiceError(
                "Rocket League service returned invalid authorization details"
            )
        return RocketLeagueAuthorization(verification_uri, user_code, expires_at)

    async def profiles(self, player_ids: list[str]) -> list[RocketLeagueProfile]:
        data = await self._request("POST", "/v1/profiles", {"player_ids": player_ids})
        raw_profiles = data.get("profiles")
        if not isinstance(raw_profiles, list):
            raise RocketLeagueServiceError("Rocket League service returned invalid profiles")
        profiles: list[RocketLeagueProfile] = []
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            player_id = raw.get("player_id")
            player_name = raw.get("player_name")
            if isinstance(player_id, str) and isinstance(player_name, str):
                profiles.append(RocketLeagueProfile(player_id, player_name))
        return profiles

    async def skills(self, *, playlist: int, player_ids: list[str]) -> list[RocketLeagueSkill]:
        data = await self._request(
            "POST",
            "/v1/skills",
            {"playlist": playlist, "player_ids": player_ids},
        )
        raw_skills = data.get("skills")
        if not isinstance(raw_skills, list):
            raise RocketLeagueServiceError("Rocket League service returned invalid skills")
        skills: list[RocketLeagueSkill] = []
        for raw in raw_skills:
            if not isinstance(raw, dict):
                continue
            try:
                player_id = raw["player_id"]
                mmr = raw["mmr"]
                tier = raw["tier"]
                division = raw["division"]
                rank = raw["rank"]
            except KeyError:
                continue
            if (
                isinstance(player_id, str)
                and isinstance(mmr, int | float)
                and isinstance(tier, int)
                and isinstance(division, int)
                and isinstance(rank, str)
            ):
                skills.append(
                    RocketLeagueSkill(
                        player_id, float(mmr), tier, division, rank, playlist=playlist
                    )
                )
        return skills

    async def player_details(self, player_id: str) -> RocketLeaguePlayerDetails:
        data = await self._request("POST", "/v1/player-details", {"player_id": player_id})
        raw_skills = data.get("skills")
        raw_rewards = data.get("rewards")
        if not isinstance(raw_skills, list) or not isinstance(raw_rewards, dict):
            raise RocketLeagueServiceError("Rocket League service returned invalid player details")
        skills: list[RocketLeagueSkill] = []
        for raw in raw_skills:
            if not isinstance(raw, dict):
                continue
            detail_player_id = raw.get("player_id")
            playlist = raw.get("playlist")
            mmr = raw.get("mmr")
            tier = raw.get("tier")
            division = raw.get("division")
            rank = raw.get("rank")
            win_streak = raw.get("win_streak")
            matches_played = raw.get("matches_played")
            placement_matches_played = raw.get("placement_matches_played")
            if (
                isinstance(detail_player_id, str)
                and isinstance(playlist, int)
                and isinstance(mmr, int | float)
                and isinstance(tier, int)
                and isinstance(division, int)
                and isinstance(rank, str)
                and isinstance(win_streak, int)
                and isinstance(matches_played, int)
                and isinstance(placement_matches_played, int)
            ):
                skills.append(
                    RocketLeagueSkill(
                        detail_player_id,
                        float(mmr),
                        tier,
                        division,
                        rank,
                        win_streak,
                        matches_played,
                        placement_matches_played,
                        playlist,
                    )
                )
        reward_level = raw_rewards.get("level")
        reward_wins = raw_rewards.get("wins")
        if not isinstance(reward_level, int) or not isinstance(reward_wins, int):
            raise RocketLeagueServiceError("Rocket League service returned invalid rewards")
        club = self._parse_club(data.get("club"))
        return RocketLeaguePlayerDetails(
            tuple(skills),
            RocketLeagueSeasonRewards(reward_level, reward_wins),
            club,
        )

    @staticmethod
    def _parse_club(raw: object) -> RocketLeagueClub | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RocketLeagueServiceError("Rocket League service returned an invalid club")
        try:
            values = (
                raw["id"],
                raw["name"],
                raw["tag"],
                raw["primary_color"],
                raw["accent_color"],
                raw["title"],
                raw["owner_player_id"],
                raw["verified"],
                raw["members"],
                raw["badges"],
            )
        except KeyError as error:
            raise RocketLeagueServiceError(
                "Rocket League service returned an invalid club"
            ) from error
        if not (
            isinstance(values[0], int)
            and all(
                isinstance(value, str) for value in (values[1], values[2], values[5], values[6])
            )
            and isinstance(values[3], int)
            and isinstance(values[4], int)
            and isinstance(values[7], bool)
            and isinstance(values[8], list)
            and isinstance(values[9], list)
        ):
            raise RocketLeagueServiceError("Rocket League service returned an invalid club")
        members = tuple(
            RocketLeagueClubMember(
                str(member.get("player_id", "")),
                str(member.get("player_name", "")),
                int(member.get("role_id", 0)),
            )
            for member in values[8]
            if isinstance(member, dict)
        )
        badges = tuple(
            RocketLeagueClubBadge(str(badge.get("stat", "")), int(badge.get("badge", 0)))
            for badge in values[9]
            if isinstance(badge, dict)
        )
        return RocketLeagueClub(
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
            values[7],
            members,
            badges,
        )

    async def activity(self, player_ids: list[str]) -> list[RocketLeagueActivity]:
        data = await self._request("POST", "/v1/activity", {"player_ids": player_ids})
        raw_activity = data.get("activity")
        if not isinstance(raw_activity, list):
            raise RocketLeagueServiceError("Rocket League service returned invalid activity")
        activity: list[RocketLeagueActivity] = []
        for raw in raw_activity:
            if not isinstance(raw, dict):
                continue
            values = (
                raw.get("player_id"),
                raw.get("playlist"),
                raw.get("mmr"),
                raw.get("tier"),
                raw.get("division"),
                raw.get("rank"),
                raw.get("win_streak"),
                raw.get("matches_played"),
            )
            if (
                isinstance(values[0], str)
                and isinstance(values[1], int)
                and isinstance(values[2], int | float)
                and isinstance(values[3], int)
                and isinstance(values[4], int)
                and isinstance(values[5], str)
                and isinstance(values[6], int)
                and isinstance(values[7], int)
            ):
                activity.append(
                    RocketLeagueActivity(
                        values[0],
                        values[1],
                        float(values[2]),
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[7],
                    )
                )
        return activity

    async def active_playlists(self) -> tuple[RocketLeaguePlaylist, ...]:
        data = await self._request("GET", "/v1/playlists")
        raw_playlists = data.get("ranked_playlists")
        if not isinstance(raw_playlists, list):
            raise RocketLeagueServiceError("Rocket League service returned invalid playlists")
        playlists: list[RocketLeaguePlaylist] = []
        seen: set[int] = set()
        for raw in raw_playlists:
            if not isinstance(raw, dict):
                continue
            playlist_id = raw.get("id")
            node_id = raw.get("node_id")
            if (
                not isinstance(playlist_id, int)
                or isinstance(playlist_id, bool)
                or playlist_id <= 0
                or not isinstance(node_id, str)
                or playlist_id in seen
            ):
                continue
            seen.add(playlist_id)
            playlists.append(
                RocketLeaguePlaylist(
                    playlist_id,
                    node_id,
                    playlist_label(playlist_id, node_id),
                )
            )
        if not playlists:
            raise RocketLeagueServiceError("Rocket League service returned no ranked playlists")
        return tuple(sorted(playlists, key=lambda playlist: playlist.id))

    async def shop(self) -> list[ItemShopItem]:
        data = await self._request("GET", "/v1/shop")
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise RocketLeagueServiceError("Rocket League service returned an invalid shop")
        items: list[ItemShopItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = self._parse_shop_item(raw)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _parse_shop_item(raw: dict[str, Any]) -> ItemShopItem | None:
        try:
            key = raw["key"]
            shop_id = raw["shop_id"]
            shop_name = raw["shop_name"]
            shop_type = raw["shop_type"]
            shop_item_id = raw["shop_item_id"]
            title = raw["title"]
            description = raw["description"]
            image_url = raw["image_url"]
            starts_at = raw["start_date"]
            ends_at = raw["end_date"]
            raw_product_ids = raw["product_ids"]
            raw_attributes = raw["attributes"]
            raw_costs = raw["costs"]
        except KeyError:
            return None
        if not (
            isinstance(key, str)
            and isinstance(shop_id, int)
            and isinstance(shop_name, str)
            and isinstance(shop_type, str)
            and isinstance(shop_item_id, int)
            and isinstance(title, str)
            and isinstance(description, str)
            and isinstance(image_url, str)
            and isinstance(starts_at, int)
            and (ends_at is None or isinstance(ends_at, int))
            and isinstance(raw_product_ids, list)
            and isinstance(raw_attributes, list)
            and isinstance(raw_costs, list)
        ):
            return None
        product_ids = tuple(value for value in raw_product_ids if isinstance(value, int))
        attributes = tuple(
            (attribute["key"], attribute["value"])
            for attribute in raw_attributes
            if isinstance(attribute, dict)
            and isinstance(attribute.get("key"), str)
            and isinstance(attribute.get("value"), str)
        )
        prices: list[ItemShopPrice] = []
        for cost in raw_costs:
            if not isinstance(cost, dict) or not isinstance(cost.get("prices"), list):
                continue
            for price in cost["prices"]:
                if (
                    isinstance(price, dict)
                    and isinstance(price.get("currency_id"), int)
                    and isinstance(price.get("amount"), int)
                ):
                    prices.append(ItemShopPrice(price["currency_id"], price["amount"]))
        return ItemShopItem(
            key,
            shop_id,
            shop_name,
            shop_type,
            shop_item_id,
            title,
            description,
            image_url,
            starts_at,
            ends_at,
            product_ids,
            attributes,
            tuple(dict.fromkeys(prices)),
        )

    async def tournaments(
        self, *, region: str = "auto"
    ) -> tuple[str, list[RocketLeagueTournament]]:
        query = "" if region == "auto" else f"?{urlencode({'region': region})}"
        data = await self._request("GET", f"/v1/tournaments{query}")
        resolved_region = data.get("region")
        raw_tournaments = data.get("tournaments")
        if not isinstance(resolved_region, str) or not isinstance(raw_tournaments, list):
            raise RocketLeagueServiceError("Rocket League service returned invalid tournaments")
        tournaments: list[RocketLeagueTournament] = []
        for raw in raw_tournaments:
            if not isinstance(raw, dict):
                continue
            tournament_id = raw.get("id")
            title = raw.get("title")
            start_time = raw.get("start_time")
            team_size = raw.get("team_size")
            game_mode = raw.get("game_mode")
            max_bracket_size = raw.get("max_bracket_size")
            rank_min = raw.get("rank_min")
            rank_max = raw.get("rank_max")
            tournament_region = raw.get("region")
            schedule_id = raw.get("schedule_id")
            if (
                not isinstance(tournament_id, int)
                or isinstance(tournament_id, bool)
                or not isinstance(title, str)
                or not isinstance(start_time, int)
                or isinstance(start_time, bool)
                or not isinstance(team_size, int)
                or isinstance(team_size, bool)
                or not isinstance(game_mode, int)
                or isinstance(game_mode, bool)
                or not isinstance(max_bracket_size, int)
                or isinstance(max_bracket_size, bool)
                or not isinstance(rank_min, int)
                or isinstance(rank_min, bool)
                or not isinstance(rank_max, int)
                or isinstance(rank_max, bool)
                or not isinstance(tournament_region, str)
                or not isinstance(schedule_id, int)
                or isinstance(schedule_id, bool)
            ):
                continue
            tournaments.append(
                RocketLeagueTournament(
                    id=tournament_id,
                    title=title,
                    start_time=start_time,
                    team_size=team_size,
                    game_mode=game_mode,
                    max_bracket_size=max_bracket_size,
                    rank_min=rank_min,
                    rank_max=rank_max,
                    region=tournament_region,
                    schedule_id=schedule_id,
                )
            )
        tournaments.sort(key=lambda tournament: (tournament.start_time, tournament.id))
        return resolved_region, tournaments

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            raise RocketLeagueServiceError("Rocket League service client is not running")
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (TypeError, ValueError) as error:
                    raise RocketLeagueServiceError(
                        f"Rocket League service returned HTTP {response.status}"
                    ) from error
                if response.status >= 400:
                    detail = data.get("error") if isinstance(data, dict) else None
                    raise RocketLeagueServiceError(str(detail or "Rocket League service failed"))
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RocketLeagueServiceError("Rocket League service is unreachable") from error
        if not isinstance(data, dict):
            raise RocketLeagueServiceError("Rocket League service returned an invalid response")
        return data


class PlayerDashboardView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        guild_id: int,
        cog: RocketLeagueCog,
        account: RocketLeagueAccount,
        overview: discord.Embed,
        all_time_streaks: discord.Embed,
        club: discord.Embed | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.cog = cog
        self.account = account
        self.overview = overview
        self.all_time_streaks = all_time_streaks
        self.club = club
        options = [
            discord.SelectOption(label="Overview", value="overview", emoji="📋"),
            discord.SelectOption(label="1v1 MMR history", value="10", emoji="📈"),
            discord.SelectOption(label="2v2 MMR history", value="11", emoji="📈"),
            discord.SelectOption(label="3v3 MMR history", value="13", emoji="📈"),
            discord.SelectOption(label="All-time streaks", value="streaks", emoji="🔥"),
        ]
        if club is not None:
            options.append(discord.SelectOption(label="Club", value="club", emoji="🛡️"))
        self.section_select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Explore this player",
            options=options,
        )
        self.section_select.callback = self._show_section  # type: ignore[method-assign]
        self.add_item(self.section_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Only the member who opened this player dashboard can use it.",
                ephemeral=True,
            )
            return False
        return True

    async def _show_section(self, interaction: discord.Interaction) -> None:
        selection = self.section_select.values[0]
        for option in self.section_select.options:
            option.default = option.value == selection
        if selection == "overview":
            await interaction.response.edit_message(
                embed=self.overview,
                attachments=[],
                view=self,
            )
            return
        if selection == "club" and self.club is not None:
            await interaction.response.edit_message(
                embed=self.club,
                attachments=[],
                view=self,
            )
            return
        if selection == "streaks":
            await interaction.response.edit_message(
                embed=self.all_time_streaks,
                attachments=[],
                view=self,
            )
            return
        playlist = int(selection)
        await interaction.response.defer()
        embed, chart = await self.cog._mmr_history_payload(
            guild_id=self.guild_id,
            requester_id=self.owner_id,
            account=self.account,
            playlist=playlist,
            days=7,
        )
        await interaction.edit_original_response(
            embed=embed,
            attachments=[chart] if chart is not None else [],
            view=self,
        )


class RocketLeagueCog(commands.Cog):
    setup_group = app_commands.Group(
        name="setup", description="Configure Slopbot features for this server"
    )
    events_setup = app_commands.Group(
        name="events",
        description="Configure Rocket League event notifications",
        parent=setup_group,
    )
    commands_setup = app_commands.Group(
        name="commands",
        description="Configure result-command visibility and channel access",
        parent=setup_group,
    )
    rank_group = app_commands.Group(
        name="rank", description="Look up current or peak Rocket League rank details"
    )

    def __init__(
        self,
        bot: SlopBot,
        store: SubscriptionStore,
        service: RocketLeagueService | None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.service = service
        self._playlist_names = dict(PLAYLIST_NAMES)
        self.worker_started_at = int(time.time())
        self.last_tracking_success_at: int | None = None

    async def cog_load(self) -> None:
        if self.service is not None:
            self.track_rocket_league_activity.add_exception_type(Exception)
            self.track_rocket_league_activity.start()

    async def cog_unload(self) -> None:
        self.track_rocket_league_activity.cancel()

    @app_commands.command(
        name="sessions", description="View server members' current or recent sessions"
    )
    @app_commands.describe(
        account="A tracked server member whose session history to view",
        all_members="Show the current or most recent session for every tracked server member",
    )
    @app_commands.guild_only()
    async def play_sessions(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        all_members: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if account is not None and all_members:
            await interaction.response.send_message(
                "Choose a member or all members, not both.", ephemeral=True
            )
            return
        service = Sessions(self.store)
        if all_members:
            accounts = await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
            eligible = {
                tracked.player_id: tracked
                for tracked in accounts
                if tracked.unlinked or guild.get_member(tracked.discord_user_id) is not None
            }
            rows = [
                row
                for row in await service.latest_server_sessions(guild.id, now=int(time.time()))
                if row["player_id"] in eligible
            ]
            if not rows:
                await interaction.response.send_message(
                    "No current or recent server sessions are available yet.", ephemeral=True
                )
                return
            pages = self._server_session_embeds(guild, rows)
            if len(pages) > 1:
                await interaction.response.send_message(
                    embed=pages[0],
                    view=EmbedPagesView(owner_id=interaction.user.id, pages=pages),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        selected = await self._resolve_account(
            guild=guild, requester=interaction.user, selection=account
        )
        if selected is None:
            await interaction.response.send_message(
                "That Rocket League account is not tracked in this server."
                if account is not None
                else "Link your Rocket League account first.",
                ephemeral=True,
            )
            return
        owner = selected.discord_user_id if selected.discord_user_id == interaction.user.id else 0
        rows = await service.history(selected.guild_id, selected.player_id, owner, limit=5)
        member = (
            guild.get_member(selected.discord_user_id) if selected.discord_user_id > 0 else None
        )
        title = (
            "Play session history"
            if member is None or member.id == interaction.user.id
            else f"{_safe_text(member.display_name)} · play session history"
        )
        embeds = [discord.Embed(title=title, description=summary(r, PLAYLIST_NAMES)) for r in rows]
        await interaction.response.send_message(
            content=None
            if rows
            else "No current or recent sessions are available for that member.",
            embeds=embeds,
            ephemeral=True,
        )

    @play_sessions.autocomplete("account")
    async def play_sessions_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    def _server_session_embeds(
        self, guild: discord.Guild, rows: list[dict[str, Any]]
    ) -> list[discord.Embed]:
        pages: list[discord.Embed] = []
        for offset in range(0, len(rows), 20):
            embed = discord.Embed(
                title=f"{_safe_text(guild.name)} · server play sessions",
                description=(
                    "Current sessions are shown first; otherwise this is each member's "
                    "most recent observed session."
                ),
                color=discord.Color.blurple(),
            )
            for row in rows[offset : offset + 20]:
                member = guild.get_member(row["user_id"]) if row["user_id"] > 0 else None
                name = _safe_text(member.display_name if member else row["player_name"])
                active = not row["closed"] and int(time.time()) <= row["last_at"] + row["cutoff"]
                delta = (
                    f"{row['mmr_change']:+.0f} MMR"
                    if row["mmr_change"] is not None
                    else "MMR unavailable"
                )
                unknown = f" / {row['unknown']}?" if row["unknown"] else ""
                embed.add_field(
                    name=f"{name} · {'current' if active else 'most recent'}",
                    value=(
                        f"**{row['total']} games** · {row['wins']}W / {row['losses']}L"
                        f"{unknown}\n"
                        f"{delta} · Last observed <t:{row['last_at']}:R>"
                    ),
                    inline=False,
                )
            pages.append(embed)
        return pages

    @app_commands.command(
        name="sessionpreferences", description="Personal session cutoff and summaries"
    )
    @app_commands.guild_only()
    async def session_preferences(
        self,
        interaction: discord.Interaction,
        cutoff_minutes: app_commands.Range[int, 5, 1440] | None = None,
        enabled: bool | None = None,
        minimum_games: app_commands.Range[int, 1, 100] | None = None,
        include_game_observations: bool | None = None,
    ) -> None:
        service = Sessions(self.store)
        gid, uid = interaction.guild_id or 0, interaction.user.id
        p = await service.settings(gid, uid)
        if any(
            v is not None
            for v in (cutoff_minutes, enabled, minimum_games, include_game_observations)
        ):
            await service.save(
                gid,
                uid,
                cutoff=cutoff_minutes * 60 if cutoff_minutes else p["cutoff"],
                enabled=enabled if enabled is not None else bool(p["enabled"]),
                minimum=minimum_games or p["minimum"],
                include_observations=(
                    include_game_observations
                    if include_game_observations is not None
                    else bool(p["include_observations"])
                ),
            )
            p = await service.settings(gid, uid)
        await interaction.response.send_message(
            f"Personal cutoff: **{p['cutoff'] // 60} minutes**. Summaries: "
            f"**{'on' if p['enabled'] else 'off'}**, minimum **{p['minimum']} games**. "
            f"Game observations: **{'included' if p['include_observations'] else 'hidden'}**. "
            "Cutoff changes apply to your next session. Delivery follows /notifications.",
            ephemeral=True,
        )

    @setup_group.command(name="sessions", description="Server session cutoff and summary channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_sessions(
        self,
        interaction: discord.Interaction,
        cutoff_minutes: app_commands.Range[int, 5, 1440] | None = None,
        enabled: bool | None = None,
        channel: discord.TextChannel | None = None,
        minimum_games: app_commands.Range[int, 1, 100] | None = None,
        include_game_observations: bool | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not interaction.permissions.manage_guild:
            await interaction.response.send_message("Manage Server is required.", ephemeral=True)
            return
        service = Sessions(self.store)
        p = await service.settings(guild.id, 0)
        if any(
            v is not None
            for v in (
                cutoff_minutes,
                enabled,
                channel,
                minimum_games,
                include_game_observations,
            )
        ):
            destination = channel.id if channel else p["channel_id"]
            active = enabled if enabled is not None else bool(p["enabled"])
            target = guild.get_channel(destination) if destination else None
            if active and (
                not isinstance(target, discord.TextChannel)
                or not target.permissions_for(
                    guild.get_member(interaction.user.id) or guild.me
                ).view_channel
                or not self._can_announce(target)
            ):
                await interaction.response.send_message(
                    "Choose a channel where Slopbot can view, send, and embed messages.",
                    ephemeral=True,
                )
                return
            await service.save(
                guild.id,
                0,
                cutoff=cutoff_minutes * 60 if cutoff_minutes else p["cutoff"],
                enabled=active,
                channel_id=destination,
                minimum=minimum_games or p["minimum"],
                include_observations=(
                    include_game_observations
                    if include_game_observations is not None
                    else bool(p["include_observations"])
                ),
            )
            p = await service.settings(guild.id, 0)
        await interaction.response.send_message(
            f"Server cutoff: **{p['cutoff'] // 60} minutes**. Summaries: "
            f"**{'on' if p['enabled'] else 'off'}**, minimum **{p['minimum']} games**. "
            f"Game observations: **{'included' if p['include_observations'] else 'hidden'}**. "
            "Cutoff changes apply to new sessions and their streak segments.",
            ephemeral=True,
        )

    async def _deliver_session_summaries(self, now: int) -> None:
        service = Sessions(self.store)
        for row in await service.finish(now):
            p = await service.settings(row["guild_id"], row["owner_id"])
            guild = self.bot.get_guild(row["guild_id"])
            account = await self.store.get_rocket_league_account(
                guild_id=row["guild_id"], discord_user_id=row["user_id"]
            )
            eligible = (
                guild is not None
                and p["enabled"]
                and row["total"] >= p["minimum"]
                and (
                    row["user_id"] < 0
                    or (
                        account is not None
                        and account.player_id == row["player_id"]
                        and guild.get_member(row["user_id"]) is not None
                    )
                )
            )
            if eligible and row["user_id"] < 0:
                eligible = any(
                    a.player_id == row["player_id"]
                    for a in await self.store.list_all_rocket_league_accounts(
                        guild_id=row["guild_id"]
                    )
                )
            retry = False
            if eligible:
                embed = discord.Embed(
                    title=f"{row['player_name'][:150]} · session summary",
                    description=summary(
                        row,
                        PLAYLIST_NAMES,
                        include_observations=bool(p["include_observations"]),
                    ),
                    color=discord.Color.blurple(),
                )
                if row["owner_id"]:
                    outcome = await send_to_destinations(
                        bot=self.bot,
                        store=self.store,
                        telegram=self.bot.telegram,
                        guild_id=row["guild_id"],
                        recipient_id=row["owner_id"],
                        discord_embed=embed,
                        telegram_text=f"{embed.title}\n{embed.description}",
                    )
                    retry = not outcome.delivered and outcome.retryable
                else:
                    channel = guild.get_channel(p["channel_id"]) if guild else None
                    if isinstance(channel, discord.TextChannel) and self._can_announce(channel):
                        try:
                            await channel.send(
                                embed=embed, allowed_mentions=discord.AllowedMentions.none()
                            )
                        except discord.HTTPException:
                            retry = True
            await service.delivered(
                row["id"],
                retry_at=(now + min(3600, 60 * 2 ** row["attempts"]))
                if retry and row["attempts"] < 4
                else 0,
            )

    async def _active_ranked_playlists(self) -> tuple[RocketLeaguePlaylist, ...]:
        playlists: tuple[RocketLeaguePlaylist, ...] | None = None
        if self.service is not None:
            try:
                playlists = await self.service.active_playlists()
            except RocketLeagueServiceError:
                LOGGER.warning(
                    "Active Rocket League playlist discovery failed; using the fallback set",
                    exc_info=True,
                )
        if not playlists:
            playlists = fallback_ranked_playlists()
        self._playlist_names.update((playlist.id, playlist.label) for playlist in playlists)
        return playlists

    @commands_setup.command(
        name="visibility",
        description="Set one command's default response visibility",
    )
    @app_commands.describe(
        command="Result command to configure",
        default="Visibility used when a member does not choose one",
    )
    @app_commands.choices(command=SHAREABLE_COMMAND_CHOICES, default=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_command_visibility(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        default: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to configure command policies.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        policy = await self.store.set_command_visibility(
            guild_id=guild.id,
            command_name=command.value,
            visibility=ResponseVisibility(default.value),
        )
        label = "Public" if policy.default_visibility is ResponseVisibility.PUBLIC else "Only me"
        await interaction.edit_original_response(
            content=f"{SHAREABLE_COMMANDS[command.value]} now defaults to **{label}**."
        )

    @commands_setup.command(
        name="allow",
        description="Restrict a command and allow it in one text channel",
    )
    @app_commands.describe(
        command="Result command to restrict",
        channel="Text channel where the command should be allowed",
    )
    @app_commands.choices(command=SHAREABLE_COMMAND_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_command_allow(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to configure command policies.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        policy = await self.store.allow_command_channel(
            guild_id=guild.id,
            command_name=command.value,
            channel_id=channel.id,
        )
        channels = ", ".join(f"<#{channel_id}>" for channel_id in policy.channel_ids)
        await interaction.edit_original_response(
            content=f"{SHAREABLE_COMMANDS[command.value]} is now limited to: {channels}."
        )

    @commands_setup.command(
        name="disallow",
        description="Remove one text channel from a command's allowlist",
    )
    @app_commands.describe(
        command="Restricted result command",
        channel="Text channel to remove from its allowlist",
    )
    @app_commands.choices(command=SHAREABLE_COMMAND_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_command_disallow(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to configure command policies.", ephemeral=True
            )
            return
        policy = await self.store.disallow_command_channel(
            guild_id=guild.id,
            command_name=command.value,
            channel_id=channel.id,
        )
        channels = ", ".join(f"<#{channel_id}>" for channel_id in policy.channel_ids)
        await interaction.response.send_message(
            f"{SHAREABLE_COMMANDS[command.value]} remains restricted to "
            f"{channels or '**no channels** (blocked everywhere)'}.",
            ephemeral=True,
        )

    @commands_setup.command(
        name="unrestricted",
        description="Allow a command in every text channel again",
    )
    @app_commands.describe(command="Result command whose channel restriction should be removed")
    @app_commands.choices(command=SHAREABLE_COMMAND_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_command_unrestricted(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to configure command policies.", ephemeral=True
            )
            return
        await self.store.clear_command_channel_restrictions(
            guild_id=guild.id,
            command_name=command.value,
        )
        await interaction.response.send_message(
            f"{SHAREABLE_COMMANDS[command.value]} can now be used in every text channel.",
            ephemeral=True,
        )

    @commands_setup.command(
        name="status",
        description="Show one result command's visibility and channel policy",
    )
    @app_commands.describe(command="Result command to inspect")
    @app_commands.choices(command=SHAREABLE_COMMAND_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_command_status(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to view command policies.", ephemeral=True
            )
            return
        policy = await self.store.get_command_policy(
            guild_id=guild.id,
            command_name=command.value,
        )
        visibility = (
            "Public" if policy.default_visibility is ResponseVisibility.PUBLIC else "Only me"
        )
        if policy.channel_restricted:
            channels = ", ".join(f"<#{channel_id}>" for channel_id in policy.channel_ids)
            channel_summary = channels or "no channels (blocked everywhere)"
        else:
            channel_summary = "every text channel"
        await interaction.response.send_message(
            f"**{SHAREABLE_COMMANDS[command.value]}**\n"
            f"Default visibility: **{visibility}**\n"
            f"Allowed in: **{channel_summary}**",
            ephemeral=True,
        )

    @events_setup.command(
        name="notifications",
        description="Route one Rocket League activity event type to a text channel",
    )
    @app_commands.describe(
        event="Event type to inspect or configure",
        channel="Destination text channel",
        minimum="Minimum cumulative streak; only used for streak events",
        inactivity_minutes="Session timeout in minutes; only used for session streaks",
        enabled="Turn this event notification on or off",
    )
    @app_commands.choices(event=EVENT_NOTIFICATION_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_event_notifications(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | None = None,
        minimum: app_commands.Range[int, 1, 100] | None = None,
        inactivity_minutes: app_commands.Range[int, 5, 1440] | None = None,
        enabled: bool | None = None,
    ) -> None:
        guild = interaction.guild
        if not self._is_guild_admin(interaction) or guild is None:
            await interaction.response.send_message(
                "You need Manage Server to configure event notifications.", ephemeral=True
            )
            return
        configured = {
            event_key: (channel_id, minimum_count)
            for event_key, channel_id, minimum_count in (
                await self.store.list_rocket_league_event_notification_configs(guild_id=guild.id)
            )
        }
        if event is None:
            if any(value is not None for value in (channel, minimum, inactivity_minutes, enabled)):
                await interaction.response.send_message(
                    "Choose an event type when changing notification settings.",
                    ephemeral=True,
                )
                return
            tracking = await self.store.get_rocket_league_tracking_config(guild_id=guild.id)
            lines = []
            for event_key, label in EVENT_NOTIFICATION_LABELS.items():
                current = configured.get(event_key)
                if current is None:
                    lines.append(f"**{label}:** disabled")
                else:
                    threshold = (
                        f" • minimum {current[1]} games"
                        if event_key in {"session_streak", "all_time_streak"}
                        else ""
                    )
                    lines.append(f"**{label}:** <#{current[0]}>{threshold}")
            lines.append(
                f"**Session inactivity timer:** {tracking.inactivity_seconds // 60} minutes"
            )
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return

        event_key = event.value
        current = configured.get(event_key)
        if event_key != "session_streak" and inactivity_minutes is not None:
            await interaction.response.send_message(
                "The inactivity timer applies only to session streak events.", ephemeral=True
            )
            return
        if channel is None and minimum is None and inactivity_minutes is None and enabled is None:
            if current is None:
                message = f"**{EVENT_NOTIFICATION_LABELS[event_key]}** notifications are disabled."
            elif event_key in {"session_streak", "all_time_streak"}:
                message = (
                    f"**{EVENT_NOTIFICATION_LABELS[event_key]}** notifications go to "
                    f"<#{current[0]}> with a minimum of **{current[1]}**."
                )
            else:
                message = (
                    f"**{EVENT_NOTIFICATION_LABELS[event_key]}** notifications go to "
                    f"<#{current[0]}>."
                )
            if event_key == "session_streak":
                tracking = await self.store.get_rocket_league_tracking_config(guild_id=guild.id)
                message += (
                    f" The inactivity timer is **{tracking.inactivity_seconds // 60} minutes**."
                )
            await interaction.response.send_message(message, ephemeral=True)
            return
        should_enable = (
            enabled if enabled is not None else channel is not None or current is not None
        )
        selected_channel_id = channel.id if channel is not None else current[0] if current else None
        if should_enable and selected_channel_id is None:
            await interaction.response.send_message(
                "Choose a text channel when enabling this event.", ephemeral=True
            )
            return
        selected_channel = channel
        if selected_channel is None and selected_channel_id is not None:
            candidate = guild.get_channel(selected_channel_id)
            selected_channel = candidate if isinstance(candidate, discord.TextChannel) else None
        if should_enable and (selected_channel is None or not self._can_announce(selected_channel)):
            await interaction.response.send_message(
                "Slopbot needs View Channel, Send Messages, and Embed Links in that channel.",
                ephemeral=True,
            )
            return
        minimum_count = (
            minimum if event_key in {"session_streak", "all_time_streak"} and minimum else 1
        )
        if minimum is None and current is not None:
            minimum_count = current[1]
        if event_key == "session_streak" and not 3 <= minimum_count <= 20:
            await interaction.response.send_message(
                "Session streak minimums must be between 3 and 20 games.", ephemeral=True
            )
            return
        if event_key == "session_streak":
            tracking = await self.store.get_rocket_league_tracking_config(guild_id=guild.id)
            await self.store.configure_rocket_league_streaks(
                guild_id=guild.id,
                channel_id=selected_channel_id if should_enable else None,
                threshold=minimum_count,
                inactivity_seconds=(
                    inactivity_minutes * 60
                    if inactivity_minutes is not None
                    else tracking.inactivity_seconds
                ),
            )
        else:
            await self.store.configure_rocket_league_event_notification(
                guild_id=guild.id,
                event_key=event_key,
                channel_id=selected_channel_id if should_enable else None,
                minimum_count=minimum_count,
            )
        if should_enable:
            message = (
                f"**{EVENT_NOTIFICATION_LABELS[event_key]}** notifications now go to "
                f"<#{selected_channel_id}>"
                + (
                    f" at **{minimum_count}+ cumulative games**."
                    if event_key in {"session_streak", "all_time_streak"}
                    else "."
                )
            )
        else:
            message = f"**{EVENT_NOTIFICATION_LABELS[event_key]}** notifications are disabled."
        if event_key == "session_streak" and inactivity_minutes is not None:
            message += f" Session inactivity is **{inactivity_minutes} minutes**."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="today",
        description="Summarize this server's tracked Rocket League activity",
    )
    @app_commands.describe(events="Number of recent events to show; defaults to 10, maximum 100")
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def today(
        self,
        interaction: discord.Interaction,
        events: app_commands.Range[int, 1, 100] = 10,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="today",
            visibility=None,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /today in this channel."
            )
            return
        recent_events = await self.store.list_rocket_league_events(
            guild_id=guild.id,
            since=int(time.time()) - ROCKET_LEAGUE_HISTORY_DAYS * 86400,
            limit=events,
        )
        if not recent_events:
            embed = discord.Embed(
                title="Latest Rocket League activity",
                description=(
                    "No tracked Rocket League activity was observed in the last "
                    f"{ROCKET_LEAGUE_HISTORY_DAYS} days."
                ),
                color=discord.Color.blurple(),
            )
            await interaction.edit_original_response(embed=embed)
            return

        lines: list[str] = []
        for event in recent_events:
            member = guild.get_member(event.discord_user_id) if event.discord_user_id > 0 else None
            display_name = (
                member.display_name
                if member is not None
                else f"{event.player_name} (unlinked Epic)"
                if event.discord_user_id < 0
                else event.player_name
            )
            lines.append(
                rocket_league_event_line(
                    event,
                    player_name=display_name,
                    playlist_name=self._playlist_names.get(
                        event.playlist, playlist_label(event.playlist)
                    ),
                )
            )

        pages: list[list[str]] = [[]]
        page_length = 0
        for line in lines:
            if pages[-1] and page_length + len(line) + 1 > 3800:
                pages.append([])
                page_length = 0
            pages[-1].append(line)
            page_length += len(line) + 1
        embeds: list[discord.Embed] = []
        shown = 0
        for page_number, page in enumerate(pages, 1):
            page_start = shown + 1
            shown += len(page)
            embed = discord.Embed(
                title=(
                    "Latest Rocket League activity"
                    if page_number == 1
                    else "Latest Rocket League activity — continued"
                ),
                description="\n".join(page),
                color=discord.Color.blurple(),
            )
            embed.set_footer(
                text=(
                    f"Events {page_start}-{shown} of {len(recent_events)} returned • "
                    f"{ROCKET_LEAGUE_HISTORY_DAYS}-day retention • most recent first • "
                    "results are inferred"
                )
            )
            embeds.append(embed)

        await interaction.edit_original_response(
            embed=embeds[0],
            view=EmbedPagesView(owner_id=interaction.user.id, pages=embeds)
            if len(embeds) > 1
            else None,
        )

    @app_commands.command(
        name="matchhistory",
        description="Show up to 30 days of observed Rocket League results",
    )
    @app_commands.describe(
        days="How many days to report; defaults to 1",
        account="Tracked member or unlinked Epic account; defaults to you",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(visibility=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def match_history(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, ROCKET_LEAGUE_HISTORY_DAYS] = 1,
        account: str | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="matchhistory",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /matchhistory in this channel."
            )
            return
        selected_account = await self._resolve_account(
            guild=interaction.guild,
            requester=interaction.user,
            selection=account,
        )
        if selected_account is None:
            await interaction.edit_original_response(
                content=(
                    "That Rocket League account is not tracked in this server."
                    if account is not None
                    else f"{interaction.user.mention} has not linked a Rocket League account "
                    "in this server."
                )
            )
            return
        now = int(time.time())
        events = await self.store.list_rocket_league_match_history(
            guild_id=interaction.guild.id,
            discord_user_id=selected_account.discord_user_id,
            since=now - days * 86400,
        )
        wins = sum(event.match_count for event in events if event.result == "win")
        losses = sum(event.match_count for event in events if event.result == "loss")
        gaps = sum(event.match_count for event in events if event.result == "gap")
        embed = discord.Embed(
            title=f"{selected_account.player_name}'s observed match history",
            description=(
                f"Last **{days} day{'s' if days != 1 else ''}** • "
                f"**{wins} W** • **{losses} L**" + (f" • **{gaps} unclassified**" if gaps else "")
            ),
            color=discord.Color.blurple(),
        )
        by_playlist: defaultdict[int, list[str]] = defaultdict(list)
        for event in events:
            marker = {"win": "W", "loss": "L", "gap": "?"}[event.result]
            marker = f"{marker}x{event.match_count}" if event.match_count > 1 else marker
            by_playlist[event.playlist].append(marker)
        for playlist, markers in sorted(
            by_playlist.items(),
            key=lambda item: self._playlist_names.get(item[0], str(item[0])),
        ):
            embed.add_field(
                name=self._playlist_names.get(playlist, playlist_label(playlist)),
                value=match_history_value(markers),
                inline=False,
            )
        if not events:
            embed.add_field(
                name="No observed matches",
                value="Tracking begins with the first successful sample and cannot backfill.",
                inline=False,
            )
        embed.set_footer(
            text="Results are inferred from skill counters; ? marks matches missed between polls."
        )
        await interaction.edit_original_response(embed=embed)

    async def _mmr_history_payload(
        self,
        *,
        guild_id: int,
        requester_id: int,
        account: RocketLeagueAccount,
        playlist: int,
        days: int,
    ) -> tuple[discord.Embed, discord.File | None]:
        playlist_name = self._playlist_names.get(playlist, playlist_label(playlist))
        points = await self.store.list_rocket_league_mmr_history(
            guild_id=guild_id,
            player_id=account.player_id,
            playlist=playlist,
            since=int(time.time()) - days * 86400,
        )
        title = f"{account.player_name} — {playlist_name} MMR history"
        if not points:
            return (
                discord.Embed(
                    title=title,
                    description=(
                        f"No MMR history has been observed in the last **{days} "
                        f"day{'s' if days != 1 else ''}**. History begins with the first poll "
                        "after this feature is deployed."
                    ),
                    color=discord.Color.blurple(),
                ),
                None,
            )
        theme_preferences: dict[str, str] = {}
        web = getattr(self.bot, "web", None)
        if web is not None:
            try:
                theme_preferences = await web.identities.theme_preferences_for_discord(
                    str(requester_id)
                )
            except Exception:
                LOGGER.exception("Unable to read web theme preferences for MMR chart")
        output = await asyncio.to_thread(
            render_mmr_history_chart,
            player_name=account.player_name,
            playlist_name=playlist_name,
            points=points,
            theme_preset=theme_preferences.get("theme_preset"),
            theme_mode=theme_preferences.get("theme_mode"),
        )
        start_mmr = points[0][1]
        end_mmr = points[-1][1]
        delta = end_mmr - start_mmr
        embed = discord.Embed(
            title=title,
            description=(
                f"Last **{days} day{'s' if days != 1 else ''}** • "
                f"**{start_mmr:,.0f} → {end_mmr:,.0f} MMR** • "
                f"**{delta:+,.0f}**"
            ),
            color=discord.Color.green()
            if delta > 0
            else discord.Color.red()
            if delta < 0
            else discord.Color.blurple(),
        )
        embed.set_image(url="attachment://mmr-history.png")
        embed.set_footer(
            text=(
                "Change-only samples with hourly continuity points • "
                f"{ROCKET_LEAGUE_HISTORY_DAYS}-day retention"
            )
        )
        return embed, discord.File(output, filename="mmr-history.png")

    @app_commands.command(
        name="mmrhistory",
        description="Graph a tracked player's observed MMR history",
    )
    @app_commands.describe(
        account="Tracked member or unlinked Epic account; defaults to you",
        gamemode="Ranked playlist; defaults to 2v2",
        days="Days to graph; defaults to 7",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        gamemode=[
            app_commands.Choice(name=label, value=value)
            for value, (label, _playlist) in GAME_MODES.items()
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def mmr_history(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        gamemode: app_commands.Choice[str] | None = None,
        days: app_commands.Range[int, 1, ROCKET_LEAGUE_HISTORY_DAYS] = 7,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="mmrhistory",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /mmrhistory in this channel."
            )
            return
        selected_account = await self._resolve_account(
            guild=guild,
            requester=interaction.user,
            selection=account,
        )
        if selected_account is None:
            await interaction.edit_original_response(
                content=(
                    "That Rocket League account is not tracked in this server."
                    if account is not None
                    else f"{interaction.user.mention} has not linked a Rocket League account "
                    "in this server."
                )
            )
            return
        mode = gamemode.value if gamemode is not None else "2v2"
        playlist = GAME_MODES[mode][1]
        embed, chart = await self._mmr_history_payload(
            guild_id=guild.id,
            requester_id=interaction.user.id,
            account=selected_account,
            playlist=playlist,
            days=days,
        )
        await interaction.edit_original_response(
            embed=embed,
            attachments=[chart] if chart is not None else [],
        )

    @mmr_history.autocomplete("account")
    async def mmr_history_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    @app_commands.command(
        name="movers",
        description="Show the server's largest observed MMR changes",
    )
    @app_commands.describe(
        gamemode="Ranked playlist; defaults to 2v2",
        days="Comparison window; defaults to 7 days",
        size="Players to show in each direction; defaults to 5",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        gamemode=[
            app_commands.Choice(name=label, value=value)
            for value, (label, _playlist) in GAME_MODES.items()
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def movers(
        self,
        interaction: discord.Interaction,
        gamemode: app_commands.Choice[str] | None = None,
        days: app_commands.Range[int, 1, ROCKET_LEAGUE_HISTORY_DAYS] = 7,
        size: app_commands.Range[int, 1, 10] = 5,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="movers",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /movers in this channel."
            )
            return
        mode = gamemode.value if gamemode is not None else "2v2"
        playlist_name, playlist = GAME_MODES[mode]
        movers = await self.store.list_rocket_league_movers(
            guild_id=guild.id,
            playlist=playlist,
            since=int(time.time()) - days * 86400,
            limit=None,
        )
        visible: list[tuple[str, float, float]] = []
        for _player_id, player_name, discord_user_id, first_mmr, last_mmr in movers:
            member = guild.get_member(discord_user_id) if discord_user_id > 0 else None
            if discord_user_id > 0 and member is None:
                continue
            visible.append(
                (member.display_name if member is not None else player_name, first_mmr, last_mmr)
            )
        climbers = sorted(
            (entry for entry in visible if entry[2] > entry[1]),
            key=lambda entry: entry[2] - entry[1],
            reverse=True,
        )[:size]
        fallers = sorted(
            (entry for entry in visible if entry[2] < entry[1]),
            key=lambda entry: entry[2] - entry[1],
        )[:size]
        embed = discord.Embed(
            title=f"{playlist_name} MMR movers",
            description=(
                f"Observed change over the last **{days} day{'s' if days != 1 else ''}**. "
                "Players need at least two stored samples."
            ),
            color=discord.Color.gold(),
        )
        for label, entries, arrow in (
            ("Climbers", climbers, "↑"),
            ("Fallers", fallers, "↓"),
        ):
            lines = [
                f"**{index}.** {discord.utils.escape_markdown(name)} — "
                f"**{last - first:+,.0f}** ({first:,.0f} → {last:,.0f})"
                for index, (name, first, last) in enumerate(entries, 1)
            ]
            embed.add_field(
                name=f"{arrow} {label}",
                value="\n".join(lines) or "No change observed",
                inline=False,
            )
        embed.set_footer(text=f"Observed polling data • {ROCKET_LEAGUE_HISTORY_DAYS}-day retention")
        await interaction.edit_original_response(embed=embed)

    @match_history.autocomplete("account")
    async def match_history_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    @tasks.loop(seconds=30.0)
    async def track_rocket_league_activity(self) -> None:
        if self.service is None:
            return
        observed_at = int(time.time())
        configs = await self.store.list_rocket_league_tracking_configs()
        config_by_guild = {config.guild_id: config for config in configs}

        accounts = await self.store.list_linked_rocket_league_accounts()
        active_accounts = [
            account
            for account in accounts
            # Negative guild IDs are isolated personal web tracking spaces. Discord
            # snowflakes are positive, so personal histories cannot overlap a server.
            if account.guild_id < 0
            or (
                (guild := self.bot.get_guild(account.guild_id)) is not None
                and (account.unlinked or guild.get_member(account.discord_user_id) is not None)
            )
        ]
        player_ids = list(dict.fromkeys(account.player_id for account in active_accounts))
        if not player_ids:
            await self.store.prune_rocket_league_match_history(
                before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
            )
            await self.store.prune_rocket_league_events(
                before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
            )
            await self.store.prune_rocket_league_mmr_history(
                before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
            )
            await self.store.finish_inactive_rocket_league_streaks(now=observed_at)
            await self._deliver_session_summaries(observed_at)
            await self._deliver_event_notifications()
            self.last_tracking_success_at = int(time.time())
            return
        active_playlists = await self._active_ranked_playlists()
        active_playlist_ids = {playlist.id for playlist in active_playlists}
        activity: list[RocketLeagueActivity] = []
        try:
            for start in range(0, len(player_ids), MAX_SERVICE_PLAYERS):
                activity.extend(
                    await self.service.activity(player_ids[start : start + MAX_SERVICE_PLAYERS])
                )
        except RocketLeagueServiceError:
            LOGGER.warning("Rocket League activity poll failed", exc_info=True)
            await self.store.finish_inactive_rocket_league_streaks(now=observed_at)
            await self._deliver_session_summaries(observed_at)
            await self._deliver_event_notifications()
            return
        by_player: defaultdict[str, list[RocketLeagueActivity]] = defaultdict(list)
        for item in activity:
            by_player[item.player_id].append(item)
        for account in active_accounts:
            tracking_config = config_by_guild.get(
                account.guild_id,
                RocketLeagueTrackingConfig(account.guild_id, None, 3, 1800, None, (11,)),
            )
            account_activity = [
                item
                for item in by_player.get(account.player_id, [])
                if item.playlist in active_playlist_ids
            ]
            await self.store.record_rocket_league_activity_batch(
                guild_id=account.guild_id,
                discord_user_id=account.discord_user_id,
                player_id=account.player_id,
                player_name=account.player_name,
                activities=[
                    (
                        item.playlist,
                        item.matches_played,
                        item.win_streak,
                        item.mmr,
                        item.rank,
                    )
                    for item in account_activity
                ],
                rank_positions={
                    item.playlist: (item.tier, item.division) for item in account_activity
                },
                threshold=tracking_config.streak_threshold,
                observed_at=observed_at,
                inactivity_seconds=tracking_config.inactivity_seconds,
            )
        for guild_id in {account.guild_id for account in active_accounts}:
            config = config_by_guild.get(
                guild_id,
                RocketLeagueTrackingConfig(guild_id, None, 3, 1800, None, (11,)),
            )
            guild_accounts = [
                account for account in active_accounts if account.guild_id == guild_id
            ]
            for playlist in active_playlist_ids:
                await self._update_leaderboard(
                    config, playlist, guild_accounts, by_player, observed_at
                )
        await self.store.prune_rocket_league_match_history(
            before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
        )
        await self.store.prune_rocket_league_events(
            before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
        )
        await self.store.prune_rocket_league_mmr_history(
            before=observed_at - ROCKET_LEAGUE_HISTORY_DAYS * 86400
        )
        await self.store.finish_inactive_rocket_league_streaks(now=observed_at)
        await self._deliver_session_summaries(observed_at)
        await self._deliver_event_notifications()
        self.last_tracking_success_at = int(time.time())

    @track_rocket_league_activity.before_loop
    async def before_tracking_rocket_league_activity(self) -> None:
        await self.bot.wait_until_ready()

    async def _deliver_event_notifications(self) -> None:
        for (
            event,
            channel_id,
            event_key,
        ) in await self.store.list_pending_rocket_league_event_notifications(limit=100):
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or event.id is None:
                continue
            member = (
                channel.guild.get_member(event.discord_user_id)
                if event.discord_user_id > 0
                else None
            )
            player_name = member.display_name if member is not None else event.player_name
            embed = discord.Embed(
                title=(
                    f"{self._playlist_names.get(event.playlist, playlist_label(event.playlist))} "
                    "leaderboard changed"
                    if event.event_type == "leaderboard_update"
                    else EVENT_NOTIFICATION_LABELS[event_key]
                ),
                description=event.notification_text
                or rocket_league_event_line(
                    event,
                    player_name=player_name,
                    playlist_name=self._playlist_names.get(
                        event.playlist, playlist_label(event.playlist)
                    ),
                ),
                color=discord.Color.gold(),
            )
            if event.event_type == "leaderboard_update":
                embed.timestamp = datetime.fromtimestamp(event.occurred_at, UTC)
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.exception(
                    "Could not send Rocket League event notification in %s", channel.id
                )
                continue
            await self.store.mark_rocket_league_event_notification_delivered(
                event_id=event.id,
                event_key=event_key,
                channel_id=channel_id,
                delivered_at=int(time.time()),
            )

    async def _update_leaderboard(
        self,
        config: RocketLeagueTrackingConfig,
        playlist: int,
        accounts: list[RocketLeagueAccount],
        by_player: dict[str, list[RocketLeagueActivity]],
        observed_at: int,
    ) -> None:
        current: list[tuple[RocketLeagueAccount, float]] = []
        for account in accounts:
            skill = next(
                (
                    item
                    for item in by_player.get(account.player_id, [])
                    if item.playlist == playlist
                ),
                None,
            )
            current.append((account, skill.mmr if skill is not None else 0.0))
        guild = self.bot.get_guild(config.guild_id)

        def display_name(account: RocketLeagueAccount) -> str:
            member = (
                guild.get_member(account.discord_user_id)
                if guild and not account.unlinked
                else None
            )
            return member.display_name if member is not None else account.player_name

        current.sort(
            key=lambda entry: (
                -entry[1],
                display_name(entry[0]).casefold(),
                entry[0].discord_user_id,
            )
        )
        entries = [
            (account.discord_user_id, position, mmr)
            for position, (account, mmr) in enumerate(current, 1)
        ]
        previous = await self.store.get_rocket_league_leaderboard_snapshot(
            guild_id=config.guild_id,
            playlist=playlist,
        )
        if previous is None or [row[0] for row in previous] == [row[0] for row in entries]:
            await self.store.replace_rocket_league_leaderboard_snapshot(
                guild_id=config.guild_id, playlist=playlist, entries=entries
            )
            return
        old_positions = {user_id: position for user_id, position, _mmr in previous}
        movement_events = [
            RocketLeagueEvent(
                "leaderboard_movement",
                account.discord_user_id,
                account.player_id,
                account.player_name,
                playlist,
                observed_at,
                mmr_after=mmr,
                position_before=old_positions.get(account.discord_user_id),
                position_after=position,
            )
            for position, (account, mmr) in enumerate(current, 1)
            if old_positions.get(account.discord_user_id) != position
        ]
        # Persist the standings at observation time so retries never mix newer MMR with old arrows.
        movement_events.append(
            RocketLeagueEvent(
                "leaderboard_update",
                0,
                "",
                "",
                playlist,
                observed_at,
                notification_text=leaderboard_change_description(
                    [
                        (
                            position,
                            display_name(account),
                            mmr,
                            old_positions.get(account.discord_user_id),
                        )
                        for position, (account, mmr) in enumerate(current, 1)
                    ]
                ),
            )
        )
        await self.store.record_rocket_league_events(
            movement_events, guild_id=config.guild_id, leaderboard_snapshot=(playlist, entries)
        )

    @staticmethod
    def _is_guild_admin(interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and bool(
            interaction.user.guild_permissions.manage_guild
        )

    @staticmethod
    def _can_announce(channel: discord.TextChannel) -> bool:
        bot_member = channel.guild.me
        if bot_member is None:
            return False
        permissions = channel.permissions_for(bot_member)
        return permissions.view_channel and permissions.send_messages and permissions.embed_links

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Try that again in {error.retry_after:.0f} seconds."
        else:
            LOGGER.error(
                "Rocket League command failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "That Rocket League command could not be completed. Try again shortly."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @rank_group.command(
        name="current",
        description="Show a tracked Rocket League account's current ranks and MMR",
    )
    @app_commands.describe(
        account="Tracked member or unlinked Epic account; defaults to you",
        playlist="Playlist set; defaults to 1v1, 2v2, and 3v3",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        playlist=[
            app_commands.Choice(
                name="All (1v1, 2v2, 3v3)",
                value="all",
            ),
            app_commands.Choice(name="Allall (every ranked playlist)", value="allall"),
            *[
                app_commands.Choice(name=label, value=value)
                for value, (label, _playlist) in GAME_MODES.items()
            ],
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def rank_current(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        playlist: app_commands.Choice[str] | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="rank",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /rank in this channel."
            )
            return
        if self.service is None:
            await interaction.edit_original_response(
                content="Rocket League stats are not configured on this bot."
            )
            return
        selected_account = await self._resolve_account(
            guild=guild,
            requester=interaction.user,
            selection=account,
        )
        if selected_account is None:
            await interaction.edit_original_response(
                content=(
                    "That Rocket League account is not tracked in this server."
                    if account is not None
                    else f"{interaction.user.mention} has not linked a Rocket League account "
                    "in this server."
                )
            )
            return
        selected_mode = playlist.value if playlist is not None else "all"
        if selected_mode == "all":
            playlists = tuple(
                RocketLeaguePlaylist(playlist_id, mode, label)
                for mode in STANDARD_RANK_MODES
                for label, playlist_id in (GAME_MODES[mode],)
            )
        elif selected_mode == "allall":
            playlists = await self._active_ranked_playlists()
        else:
            label, playlist_id = GAME_MODES[selected_mode]
            playlists = (RocketLeaguePlaylist(playlist_id, selected_mode, label),)
        responses: list[str] = []
        try:
            skills_by_playlist: dict[int, RocketLeagueSkill] = {}
            if len(playlists) > 1:
                for activity in await self.service.activity([selected_account.player_id]):
                    skills_by_playlist[activity.playlist] = RocketLeagueSkill(
                        activity.player_id,
                        activity.mmr,
                        activity.tier,
                        activity.division,
                        activity.rank,
                        playlist=activity.playlist,
                    )
            else:
                playlist_id = playlists[0].id
                fetched = await self.service.skills(
                    playlist=playlist_id,
                    player_ids=[selected_account.player_id],
                )
                skills_by_playlist.update(
                    (playlist_id, skill)
                    for skill in fetched
                    if skill.player_id == selected_account.player_id
                )
            for ranked_playlist in playlists:
                playlist_id = ranked_playlist.id
                skill = skills_by_playlist.get(
                    playlist_id,
                    RocketLeagueSkill(
                        selected_account.player_id,
                        0.0,
                        0,
                        0,
                        "Unranked",
                        playlist=playlist_id,
                    ),
                )
                responses.append(
                    rank_response(
                        selected_account.player_name,
                        ranked_playlist.label,
                        skill,
                        await estimate_rank_steps(
                            self.store,
                            skill=skill,
                            playlist=playlist_id,
                            player_id=selected_account.player_id,
                        ),
                    )
                )
        except RocketLeagueServiceError:
            LOGGER.exception("Rocket League rank request failed in guild %s", guild.id)
            await interaction.edit_original_response(
                content="Rocket League stats are temporarily unavailable. Try again shortly."
            )
            return

        await interaction.edit_original_response(content="\n\n".join(responses))

    @rank_current.autocomplete("account")
    async def rank_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    async def _account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        normalized = current.strip().casefold()
        choices: list[tuple[str, str, str]] = []
        for account in await self.store.list_all_rocket_league_accounts(guild_id=guild.id):
            if account.unlinked:
                label = f"{account.player_name} — unlinked Epic account"
                value = f"epic:{-account.discord_user_id}"
                searchable = f"{account.player_name} unlinked epic {value}".casefold()
            else:
                member = guild.get_member(account.discord_user_id)
                if member is None or member.bot:
                    continue
                label = f"{member.display_name} — {account.player_name}"
                value = f"member:{member.id}"
                searchable = (
                    f"{member.display_name} {member.name} {account.player_name} {value}".casefold()
                )
            if normalized and normalized not in searchable:
                continue
            choices.append((label.casefold(), label[:100], value))
        choices.sort(key=lambda item: item[0])
        return [
            app_commands.Choice(name=label, value=value) for _sort_key, label, value in choices[:25]
        ]

    async def _resolve_account(
        self,
        *,
        guild: discord.Guild,
        requester: discord.Member,
        selection: str | None,
    ) -> RocketLeagueAccount | None:
        if selection is None:
            return await self.store.get_rocket_league_account(
                guild_id=guild.id,
                discord_user_id=requester.id,
            )
        kind, separator, raw_id = selection.partition(":")
        if not separator:
            return None
        try:
            selected_id = int(raw_id)
        except ValueError:
            return None
        accounts = await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
        if kind == "member":
            member = guild.get_member(selected_id)
            if member is None or member.bot:
                return None
            return next(
                (
                    account
                    for account in accounts
                    if not account.unlinked and account.discord_user_id == selected_id
                ),
                None,
            )
        if kind == "epic" and selected_id > 0:
            return next(
                (
                    account
                    for account in accounts
                    if account.unlinked and account.discord_user_id == -selected_id
                ),
                None,
            )
        return None

    @rank_group.command(
        name="peak",
        description="Show a tracked Rocket League account's highest observed ranks and MMR",
    )
    @app_commands.describe(
        account="Tracked member or unlinked Epic account; defaults to you",
        playlist="Playlist set; defaults to 1v1, 2v2, and 3v3",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        playlist=[
            app_commands.Choice(name="All (1v1, 2v2, 3v3)", value="all"),
            app_commands.Choice(name="Allall (every ranked playlist)", value="allall"),
            *[
                app_commands.Choice(name=label, value=value)
                for value, (label, _playlist) in GAME_MODES.items()
            ],
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def rank_peak(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        playlist: app_commands.Choice[str] | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="rank",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /rank in this channel."
            )
            return
        selected_account = await self._resolve_account(
            guild=guild,
            requester=interaction.user,
            selection=account,
        )
        if selected_account is None:
            await interaction.edit_original_response(
                content=(
                    "That Rocket League account is not tracked in this server."
                    if account is not None
                    else f"{interaction.user.mention} has not linked a Rocket League account "
                    "in this server."
                )
            )
            return

        selected_mode = playlist.value if playlist is not None else "all"
        if selected_mode == "all":
            playlists = tuple(
                RocketLeaguePlaylist(playlist_id, mode, label)
                for mode in STANDARD_RANK_MODES
                for label, playlist_id in (GAME_MODES[mode],)
            )
        elif selected_mode == "allall":
            playlists = await self._active_ranked_playlists()
        else:
            label, playlist_id = GAME_MODES[selected_mode]
            playlists = (RocketLeaguePlaylist(playlist_id, selected_mode, label),)
        responses: list[str] = []
        for ranked_playlist in playlists:
            peak = await self.store.get_rocket_league_peak_mmr(
                guild_id=guild.id,
                player_id=selected_account.player_id,
                playlist=ranked_playlist.id,
            )
            if peak is not None:
                responses.append(
                    peak_rank_response(
                        selected_account.player_name,
                        ranked_playlist.label,
                        peak.mmr,
                        peak.rank,
                        peak.observed_at,
                    )
                )
        await interaction.edit_original_response(
            content=(
                "\n\n".join(responses)
                if responses
                else "No peak has been observed for that player and playlist selection yet."
            )
        )

    @rank_peak.autocomplete("account")
    async def rank_peak_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    @app_commands.command(
        name="playeroverview",
        description="Show a tracked player's ranks, peaks, and streak records",
    )
    @app_commands.describe(
        account="Tracked member or unlinked Epic account; defaults to you",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(visibility=RESPONSE_VISIBILITY_CHOICES)
    @app_commands.checks.cooldown(2, 15.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def player_overview(
        self,
        interaction: discord.Interaction,
        account: str | None = None,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="playeroverview",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /playeroverview in this channel."
            )
            return
        selected_account = await self._resolve_account(
            guild=guild,
            requester=interaction.user,
            selection=account,
        )
        if selected_account is None:
            await interaction.edit_original_response(
                content=(
                    "That Rocket League account is not tracked in this server."
                    if account is not None
                    else f"{interaction.user.mention} has not linked a Rocket League account "
                    "in this server."
                )
            )
            return

        current_ranks: list[tuple[str, RocketLeagueSkill, int]] = []
        details: RocketLeaguePlayerDetails | None = None
        if self.service is not None:
            try:
                details = await self.service.player_details(selected_account.player_id)
            except RocketLeagueServiceError:
                LOGGER.warning(
                    "Rocket League overview detail request failed for %s",
                    selected_account.player_id,
                    exc_info=True,
                )
            try:
                members = {member.id: member for member in guild.members if not member.bot}
                if interaction.user.id not in members:
                    members[interaction.user.id] = interaction.user
                accounts = await self.store.list_rocket_league_accounts(
                    guild_id=guild.id, discord_user_ids=list(members)
                )
                accounts.extend(
                    tracked
                    for tracked in await self.store.list_all_rocket_league_accounts(
                        guild_id=guild.id
                    )
                    if tracked.unlinked
                )
                if len(accounts) > MAX_SERVICE_PLAYERS:
                    raise RocketLeagueServiceError(
                        "too many tracked accounts to calculate server positions"
                    )
                modes = [GAME_MODES[mode] for mode in STANDARD_RANK_MODES]
                activity = await self.service.activity([tracked.player_id for tracked in accounts])
                for label, playlist_id in modes:
                    ranked = ranked_skill_entries(
                        accounts,
                        members,
                        [
                            RocketLeagueSkill(
                                item.player_id,
                                item.mmr,
                                item.tier,
                                item.division,
                                item.rank,
                                playlist=item.playlist,
                            )
                            for item in activity
                            if item.playlist == playlist_id
                        ],
                    )
                    selected = next(
                        (
                            (position, skill)
                            for position, tracked, _member, skill in ranked
                            if tracked.player_id == selected_account.player_id
                        ),
                        None,
                    )
                    if selected is not None:
                        current_position, skill = selected
                        if details is not None:
                            detailed_skill = next(
                                (
                                    item
                                    for item in details.skills
                                    if item.player_id == selected_account.player_id
                                    and item.playlist == playlist_id
                                ),
                                None,
                            )
                            if detailed_skill is not None:
                                skill = detailed_skill
                        current_ranks.append((label, skill, current_position))
            except RocketLeagueServiceError:
                LOGGER.warning(
                    "Rocket League overview rank request failed in guild %s",
                    guild.id,
                    exc_info=True,
                )

        peak_ranks: list[tuple[str, RocketLeaguePeakMMR, int]] = []
        for mode in STANDARD_RANK_MODES:
            label, playlist_id = GAME_MODES[mode]
            peak = await self.store.get_rocket_league_peak_mmr(
                guild_id=guild.id,
                player_id=selected_account.player_id,
                playlist=playlist_id,
            )
            if peak is not None:
                peak_position = await self.store.get_rocket_league_peak_mmr_position(
                    guild_id=guild.id,
                    player_id=selected_account.player_id,
                    playlist=playlist_id,
                )
                if peak_position is not None:
                    peak_ranks.append((label, peak, peak_position))
        streak_records = await self.store.list_rocket_league_player_records(
            guild_id=guild.id,
            player_id=selected_account.player_id,
        )
        streak_records_with_positions: list[tuple[RocketLeagueRecord, int]] = []
        for record in streak_records:
            record_position = await self.store.get_rocket_league_record_position(
                guild_id=guild.id, record_id=record.id
            )
            if record_position is not None:
                streak_records_with_positions.append((record, record_position))
        rank_outlook = {
            skill.playlist: estimate
            for _label, skill, _position in current_ranks
            if (
                estimate := await estimate_rank_steps(
                    self.store,
                    skill=skill,
                    playlist=skill.playlist,
                    player_id=selected_account.player_id,
                )
            )
            is not None
        }
        overview = player_overview_embed(
            selected_account.player_name,
            current_ranks,
            peak_ranks,
            details,
            rank_outlook,
        )
        all_time_streaks = all_time_streaks_embed(
            selected_account.player_name,
            streak_records_with_positions,
        )
        club_embed = (
            club_details_embed(selected_account.player_name, details.club)
            if details is not None and details.club is not None
            else None
        )
        await interaction.edit_original_response(
            embed=overview,
            view=PlayerDashboardView(
                owner_id=interaction.user.id,
                guild_id=guild.id,
                cog=self,
                account=selected_account,
                overview=overview,
                all_time_streaks=all_time_streaks,
                club=club_embed,
            ),
        )

    @player_overview.autocomplete("account")
    async def player_overview_account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._account_autocomplete(interaction, current)

    @app_commands.command(
        name="leaderboard",
        description="Show the Rocket League leaderboard for tracked accounts in this server",
    )
    @app_commands.describe(
        gamemode="Ranked playlist; defaults to 2v2",
        size="Number of leading members to show; defaults to 3",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        gamemode=[
            app_commands.Choice(name=label, value=value)
            for value, (label, _playlist) in GAME_MODES.items()
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        gamemode: app_commands.Choice[str] | None = None,
        size: app_commands.Range[int, 1, MAX_LEADERBOARD_SIZE] = 3,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="leaderboard",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /leaderboard in this channel."
            )
            return
        if self.service is None:
            await interaction.edit_original_response(
                content="Rocket League stats are not configured on this bot."
            )
            return
        mode = gamemode.value if gamemode is not None else "2v2"
        mode_label, playlist = GAME_MODES[mode]
        members = {member.id: member for member in guild.members if not member.bot}
        if interaction.user.id not in members and isinstance(interaction.user, discord.Member):
            members[interaction.user.id] = interaction.user
        accounts = await self.store.list_rocket_league_accounts(
            guild_id=guild.id, discord_user_ids=list(members)
        )
        accounts.extend(
            account
            for account in await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
            if account.unlinked
        )
        if not accounts:
            await interaction.edit_original_response(
                content=(
                    "No Rocket League accounts are tracked in this server yet. "
                    "Use `/linkrocketleague` or ask an admin to add an Epic account."
                )
            )
            return
        if len(accounts) > MAX_SERVICE_PLAYERS:
            await interaction.edit_original_response(
                content=(
                    "This server has too many tracked accounts for one leaderboard request. "
                    "Ask an administrator to check the bot logs."
                )
            )
            return
        try:
            fetched = await self.service.skills(
                playlist=playlist,
                player_ids=[account.player_id for account in accounts],
            )
        except RocketLeagueServiceError:
            LOGGER.exception("Rocket League leaderboard request failed in guild %s", guild.id)
            await interaction.edit_original_response(
                content="Rocket League stats are temporarily unavailable. Try again shortly."
            )
            return

        ranked = ranked_skill_entries(accounts, members, fetched)
        visible, appended_position = leaderboard_window(
            ranked,
            size=size,
            caller_id=interaction.user.id,
        )
        lines: list[str] = []
        for position, account, member, skill in visible:
            if appended_position == position:
                lines.append("…")
            name = discord.utils.escape_markdown(
                member.display_name if member else account.player_name
            )
            suffix = " **(you)**" if member is not None and member.id == interaction.user.id else ""
            lines.append(f"**{position}.** {name}{suffix} — **{skill.mmr:.0f} MMR** — {skill.rank}")
        embed = discord.Embed(
            title=f"{mode_label} server leaderboard",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"{len(ranked)} tracked account{'s' if len(ranked) != 1 else ''} • "
                "Use /linkrocketleague to update your account"
            )
        )
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(
        name="streakleaderboard",
        description="Show permanent cross-playlist all-time streak records",
    )
    @app_commands.describe(
        result="Win or loss streaks; defaults to wins",
        scope="Competitive or all ranked modes; defaults to competitive",
        size="Number of streak records to show; defaults to 3",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        result=[
            app_commands.Choice(name="Win streaks", value="win"),
            app_commands.Choice(name="Loss streaks", value="loss"),
        ],
        scope=[
            app_commands.Choice(name="Competitive — 1v1, 2v2, and 3v3", value="main"),
            app_commands.Choice(name="All ranked modes — special included", value="all"),
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def streak_leaderboard(
        self,
        interaction: discord.Interaction,
        result: app_commands.Choice[str] | None = None,
        scope: app_commands.Choice[str] | None = None,
        size: app_commands.Range[int, 1, MAX_LEADERBOARD_SIZE] = 3,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="streakleaderboard",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /streakleaderboard in this channel."
            )
            return

        result_name = result.value if result is not None else "win"
        streak_class = scope.value if scope is not None else "main"
        records = await self.store.list_rocket_league_records(
            guild_id=guild.id,
            streak_class=streak_class,
            result=result_name,
            limit=size,
        )
        lines: list[str] = []
        for position, record in enumerate(records, 1):
            member = (
                guild.get_member(record.discord_user_id) if record.discord_user_id > 0 else None
            )
            name = discord.utils.escape_markdown(
                member.display_name if member is not None else record.player_name
            )
            suffix = (
                " **(you)**"
                if record.discord_user_id > 0 and record.discord_user_id == interaction.user.id
                else ""
            )
            lines.append(
                f"**{position}.** {name}{suffix} — **{record.count} games** — "
                f"<t:{record.observed_at}:d>"
            )

        result_label = "win" if result_name == "win" else "loss"
        category_label = "Competitive" if streak_class == "main" else "All-ranked"
        embed = discord.Embed(
            title=f"{category_label} all-time {result_label} streak leaderboard",
            description=(
                "\n".join(lines)
                if lines
                else "No ended all-time streaks have been observed for this scope yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                "Permanent records • one player may appear more than once • "
                "session settings never affect this board"
            )
        )
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(
        name="peakleaderboard",
        description="Show this server's highest observed Rocket League MMR peaks",
    )
    @app_commands.describe(
        gamemode="Ranked playlist; defaults to 2v2",
        size="Number of player peaks to show; defaults to 3",
        visibility="Who can see the response; defaults to this server's command setting",
    )
    @app_commands.choices(
        gamemode=[
            app_commands.Choice(name=label, value=value)
            for value, (label, _playlist) in GAME_MODES.items()
        ],
        visibility=RESPONSE_VISIBILITY_CHOICES,
    )
    @app_commands.checks.cooldown(
        1,
        15.0,
        key=lambda interaction: (interaction.guild_id, interaction.user.id),
    )
    @app_commands.guild_only()
    async def peak_leaderboard(
        self,
        interaction: discord.Interaction,
        gamemode: app_commands.Choice[str] | None = None,
        size: app_commands.Range[int, 1, MAX_LEADERBOARD_SIZE] = 3,
        visibility: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        allowed, ephemeral = await defer_result_response(
            store=self.store,
            interaction=interaction,
            command_name="peakleaderboard",
            visibility=visibility,
        )
        if not allowed:
            await interaction.edit_original_response(
                content="An administrator has not allowed /peakleaderboard in this channel."
            )
            return

        mode = gamemode.value if gamemode is not None else "2v2"
        mode_label, playlist = GAME_MODES[mode]
        peaks = await self.store.list_rocket_league_peak_mmr(
            guild_id=guild.id, playlist=playlist, limit=size
        )
        lines: list[str] = []
        for position, peak in enumerate(peaks, 1):
            name = discord.utils.escape_markdown(peak.player_name)
            suffix = (
                " **(you)**"
                if peak.discord_user_id > 0 and peak.discord_user_id == interaction.user.id
                else ""
            )
            lines.append(
                f"**{position}.** {name}{suffix} — **{peak.mmr:,.0f} MMR** — "
                f"<t:{peak.observed_at}:d>"
            )

        embed = discord.Embed(
            title=f"{mode_label} peak MMR leaderboard",
            description=(
                "\n".join(lines)
                if lines
                else "No MMR peaks have been observed for this playlist yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="One all-time observed peak is retained per Rocket League player.")
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(
        name="linkrocketleague",
        description="Link a Rocket League account for server leaderboards",
    )
    @app_commands.describe(
        platform="Platform for the Rocket League account",
        account_id="Raw platform account ID, not a display name",
        member="Administrators may link another server member",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name=label, value=value)
            for value, (label, _api_name) in PLATFORMS.items()
        ]
    )
    @app_commands.checks.cooldown(2, 60.0, key=lambda interaction: interaction.user.id)
    @app_commands.guild_only()
    async def link_rocket_league(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        account_id: app_commands.Range[str, 1, 128],
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        target = member or interaction.user
        if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to link an account for another member.", ephemeral=True
            )
            return
        if target.bot:
            await interaction.response.send_message(
                "Rocket League accounts cannot be linked to bots.", ephemeral=True
            )
            return
        if self.service is None:
            await interaction.response.send_message(
                "Rocket League stats are not configured on this bot.", ephemeral=True
            )
            return
        normalized_id = account_id.strip()
        if not PLAYER_ACCOUNT_ID_PATTERN.fullmatch(normalized_id):
            await interaction.response.send_message(
                "That account ID contains unsupported characters.", ephemeral=True
            )
            return
        _label, api_platform = PLATFORMS[platform.value]
        player_id = f"{api_platform}|{normalized_id}|0"
        await interaction.response.defer(ephemeral=True)
        try:
            profiles = await self.service.profiles([player_id])
        except RocketLeagueServiceError:
            LOGGER.exception("Rocket League account validation failed")
            await interaction.edit_original_response(
                content="Rocket League account validation is temporarily unavailable."
            )
            return
        profile = next((item for item in profiles if item.player_id == player_id), None)
        if profile is None or not profile.player_name.strip():
            await interaction.edit_original_response(
                content=(
                    "Rocket League could not find that account. Check the platform and use the "
                    "raw account ID rather than the display name."
                )
            )
            return
        try:
            await self.store.save_rocket_league_account(
                guild_id=interaction.guild.id,
                discord_user_id=target.id,
                player_id=player_id,
                player_name=profile.player_name.strip()[:100],
            )
        except RocketLeagueAccountConflictError as error:
            await interaction.edit_original_response(content=str(error))
            return
        safe_player_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(profile.player_name)
        )
        await interaction.edit_original_response(
            content=f"Linked **{safe_player_name}** to {target.mention}."
        )

    @app_commands.command(
        name="addepicaccount",
        description="Admin: add an Epic account without linking it to a Discord user",
    )
    @app_commands.describe(epic_id="Raw Epic account ID, not a display name")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_epic_account(
        self, interaction: discord.Interaction, epic_id: app_commands.Range[str, 1, 128]
    ) -> None:
        if interaction.guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to add an unlinked Epic account.", ephemeral=True
            )
            return
        if self.service is None:
            await interaction.response.send_message(
                "Rocket League stats are not configured on this bot.", ephemeral=True
            )
            return
        normalized_id = epic_id.strip()
        if not PLAYER_ACCOUNT_ID_PATTERN.fullmatch(normalized_id):
            await interaction.response.send_message(
                "That Epic ID contains unsupported characters.", ephemeral=True
            )
            return
        player_id = f"Epic|{normalized_id}|0"
        await interaction.response.defer(ephemeral=True)
        try:
            profiles = await self.service.profiles([player_id])
        except RocketLeagueServiceError:
            await interaction.edit_original_response(
                content="Rocket League account validation is temporarily unavailable."
            )
            return
        profile = next((item for item in profiles if item.player_id == player_id), None)
        if profile is None or not profile.player_name.strip():
            await interaction.edit_original_response(
                content="Rocket League could not find that Epic account ID."
            )
            return
        try:
            await self.store.save_unlinked_rocket_league_account(
                guild_id=interaction.guild.id,
                player_id=player_id,
                player_name=profile.player_name.strip()[:100],
            )
        except RocketLeagueAccountConflictError as error:
            await interaction.edit_original_response(content=str(error))
            return
        safe_player_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(profile.player_name)
        )
        await interaction.edit_original_response(
            content=(
                f"Added **{safe_player_name}** as an unlinked "
                "Epic account. It will be tracked, but never mentioned."
            )
        )

    @app_commands.command(
        name="removeepicaccount",
        description="Admin: stop tracking an unlinked Epic account",
    )
    @app_commands.describe(epic_id="Raw Epic account ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_epic_account(
        self, interaction: discord.Interaction, epic_id: app_commands.Range[str, 1, 128]
    ) -> None:
        if interaction.guild is None or not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "You need Manage Server to remove an unlinked Epic account.", ephemeral=True
            )
            return
        player_id = f"Epic|{epic_id.strip()}|0"
        removed = await self.store.unlink_unlinked_rocket_league_account(
            guild_id=interaction.guild.id, player_id=player_id
        )
        await interaction.response.send_message(
            "Stopped tracking that unlinked Epic account."
            if removed
            else "No unlinked Epic account with that ID is tracked here.",
            ephemeral=True,
        )

    @app_commands.command(
        name="unlinkrocketleague",
        description="Remove a Rocket League account from server leaderboards",
    )
    @app_commands.describe(member="Administrators may unlink another server member")
    @app_commands.guild_only()
    async def unlink_rocket_league(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        target = member or interaction.user
        if target.id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to unlink another member.", ephemeral=True
            )
            return
        removed = await self.store.unlink_rocket_league_account(
            guild_id=interaction.guild.id,
            discord_user_id=target.id,
        )
        message = (
            f"Removed the Rocket League link for {target.mention}."
            if removed
            else f"No Rocket League account was linked for {target.mention}."
        )
        await interaction.response.send_message(message, ephemeral=True)


@dataclass(frozen=True, slots=True)
class SlopbotStatusSnapshot:
    lines: tuple[str, ...]
    healthy: bool
    checked_at: int

    @property
    def fingerprint(self) -> str:
        problems = tuple(
            re.sub(r"<t:\d+:[A-Za-z]>", "<timestamp>", line)
            for line in self.lines
            if line.startswith("❌")
        )
        return "healthy" if self.healthy else "unhealthy|" + "|".join(problems)

    def embed(self, *, alert: bool = False) -> discord.Embed:
        title = (
            "Slopbot recovered"
            if alert and self.healthy
            else "Slopbot issue detected"
            if alert
            else "Slopbot status"
        )
        embed = discord.Embed(
            title=title,
            description="\n".join(self.lines),
            color=discord.Color.green() if self.healthy else discord.Color.orange(),
        )
        embed.set_footer(text=f"Checked <t:{self.checked_at}:R> • Automatic checks every 2 minutes")
        return embed


def background_worker_status(
    *,
    running: bool,
    started_at: int | None,
    last_success_at: int | None,
    now: int,
    stale_after: int,
    active_detail: str,
) -> tuple[bool, str]:
    if not running:
        return False, "Worker unavailable"
    if last_success_at is None:
        if started_at is not None and now - started_at > stale_after:
            return False, f"No successful run since startup <t:{started_at}:R>"
        return True, "Starting; awaiting first successful run"
    if now - last_success_at > stale_after:
        return False, f"Last successful run <t:{last_success_at}:R> (stale)"
    return True, f"{active_detail}; last success <t:{last_success_at}:R>"


def cloudflare_tunnel_connections(metrics: str) -> int | None:
    """Return the connector's active HA count from cloudflared Prometheus metrics."""

    values = re.findall(
        r"^cloudflared_tunnel_ha_connections(?:\{[^}]*\})?\s+([0-9]+(?:\.[0-9]+)?)$",
        metrics,
        flags=re.MULTILINE,
    )
    return round(sum(float(value) for value in values)) if values else None


class StatusCog(commands.Cog):
    def __init__(self, bot: SlopBot) -> None:
        self.bot = bot
        self._latest: SlopbotStatusSnapshot | None = None

    async def cog_load(self) -> None:
        self.poll_slopbot_status.start()

    async def cog_unload(self) -> None:
        self.poll_slopbot_status.cancel()

    @app_commands.command(
        name="summarizeserver",
        description="Summarize Slopbot's server-level configuration",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def summarize_server(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if (
            guild is None
            or not isinstance(interaction.user, discord.Member)
            or not (interaction.user.guild_permissions.manage_guild)
        ):
            await interaction.response.send_message(
                "You need Manage Server to view Slopbot's server configuration.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        (
            tracking,
            event_notifications,
            tournament_alerts,
            policies,
            status_channel_ids,
        ) = await asyncio.gather(
            self.bot.store.get_rocket_league_tracking_config(guild_id=guild.id),
            self.bot.store.list_rocket_league_event_notification_configs(guild_id=guild.id),
            self.bot.store.list_tournament_channel_alerts_for_guild(guild_id=guild.id),
            self.bot.store.list_command_policies(guild_id=guild.id),
            self.bot.store.list_slopbot_status_channel_alerts_for_guild(guild_id=guild.id),
        )
        config_embed = server_configuration_embed(
            guild_name=guild.name,
            tracking=tracking,
            event_notifications=event_notifications,
            tournament_alerts=tournament_alerts,
            policies=policies,
            status_channel_ids=status_channel_ids,
        )
        from slopbot.summaries import Summaries

        summary_settings = await Summaries(self.bot.store).list_settings(guild.id, 0)
        config_embed.add_field(
            name="Daily and weekly summaries",
            value="\n".join(
                f"{s['cadence'].title()}: "
                + (
                    f"<#{s['config']['channel_id']}> · {s['config']['hour']:02}:"
                    f"{s['config']['minute']:02} {s['config']['timezone']} · "
                    f"{len(s['config']['sections'])} sections"
                    if s["config"]["enabled"]
                    else "Disabled"
                )
                for s in summary_settings
            ),
            inline=False,
        )
        await interaction.edit_original_response(embed=config_embed)

    @app_commands.command(
        name="slopbotstatus",
        description="Show whether Slopbot's configured services are operational",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def slopbot_status(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need Manage Server to view Slopbot's service status.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        snapshot = await self._collect_status()
        await interaction.edit_original_response(embed=snapshot.embed())

    @app_commands.command(
        name="reauthorizerocketleague",
        description="Start the Epic device login when Rocket League authorization expires",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reauthorize_rocket_league(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need Manage Server to reauthorize Rocket League.", ephemeral=True
            )
            return
        if self.bot.rocket_league is None:
            await interaction.response.send_message(
                "Rocket League is not configured on this bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            authorization = await self.bot.rocket_league.start_authorization()
        except RocketLeagueServiceError as error:
            await interaction.edit_original_response(content=str(error))
            return
        await interaction.edit_original_response(
            content=(
                f"Open [Epic Games activation]({authorization.verification_uri}) and enter "
                f"code **{authorization.user_code}**. This code expires "
                f"<t:{authorization.expires_at}:R>. Slopbot reconnects automatically after login."
            )
        )

    @app_commands.command(
        name="slopbotstatusalerts",
        description="Configure your private Slopbot issue and recovery alerts",
    )
    @app_commands.describe(enabled="Turn your personal status alerts on or off; omit to view")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def slopbot_status_alerts(
        self, interaction: discord.Interaction, enabled: bool | None = None
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if (
            guild is None
            or not isinstance(member, discord.Member)
            or not member.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need Manage Server to configure Slopbot status alerts.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if enabled is not None:
            await self.bot.store.set_slopbot_status_user_alert(
                guild_id=guild.id, user_id=member.id, enabled=enabled
            )
        current = await self.bot.store.has_slopbot_status_user_alert(
            guild_id=guild.id, user_id=member.id
        )
        await interaction.edit_original_response(
            content=(
                f"Your Slopbot issue and recovery alerts are **{'on' if current else 'off'}**. "
                "Private delivery follows `/notifications`."
            )
        )

    @app_commands.command(
        name="slopbotstatuschannel",
        description="Configure Slopbot issue and recovery alerts in a text channel",
    )
    @app_commands.describe(channel="Text channel to configure", enabled="Turn alerts on or off")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def slopbot_status_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        enabled: bool = True,
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if (
            guild is None
            or not isinstance(member, discord.Member)
            or not member.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need Manage Server to configure Slopbot status channels.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        bot_member = guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        if enabled and (
            channel.guild.id != guild.id
            or permissions is None
            or not permissions.view_channel
            or not permissions.send_messages
            or not permissions.embed_links
        ):
            await interaction.edit_original_response(
                content="Choose a channel where Slopbot can view, send messages, and embed links."
            )
            return
        await self.bot.store.set_slopbot_status_channel_alert(
            guild_id=guild.id,
            channel_id=channel.id,
            configured_by_user_id=member.id,
            enabled=enabled,
        )
        await interaction.edit_original_response(
            content=(
                f"Slopbot status alerts are **{'on' if enabled else 'off'}** in {channel.mention}."
            )
        )

    @tasks.loop(minutes=2.0)
    async def poll_slopbot_status(self) -> None:
        try:
            snapshot = await self._collect_status()
            self._latest = snapshot
            previous = await self.bot.store.replace_slopbot_status_state(
                fingerprint=snapshot.fingerprint
            )
            if previous is not None and previous != snapshot.fingerprint:
                await self._deliver_status_change(snapshot)
        except Exception:
            LOGGER.exception("Slopbot status poll failed; the next poll will retry")

    @poll_slopbot_status.before_loop
    async def before_poll_slopbot_status(self) -> None:
        await self.bot.wait_until_ready()

    async def _collect_status(self) -> SlopbotStatusSnapshot:
        now = int(time.time())
        database_ok, rocket_status, public_web_status, tunnel_status = await asyncio.gather(
            self.bot.store.healthcheck(),
            self._rocket_league_status(),
            self._public_web_status(),
            self._cloudflare_tunnel_status(),
        )
        discord_ok = self.bot.is_ready()
        engagement = self.bot.get_cog("EngagementCog")
        delivery_loop = getattr(engagement, "deliver_scheduled_getons", None)
        geton_ok, geton_detail = background_worker_status(
            running=bool(delivery_loop is not None and delivery_loop.is_running()),
            started_at=getattr(engagement, "worker_started_at", None),
            last_success_at=getattr(engagement, "last_geton_delivery_success_at", None),
            now=now,
            stale_after=60,
            active_detail="Processing scheduled deliveries",
        )
        telegram_status = (
            "Not configured"
            if self.bot.telegram is None
            else (
                f"Operational (@{self.bot.telegram.username})"
                if self.bot.telegram.username is not None
                else "Unavailable; reconnecting"
            )
        )
        rocket_cog = self.bot.get_cog("RocketLeagueCog")
        tracking_loop = getattr(rocket_cog, "track_rocket_league_activity", None)
        tracking_ok, tracking_detail = background_worker_status(
            running=bool(tracking_loop is not None and tracking_loop.is_running()),
            started_at=getattr(rocket_cog, "worker_started_at", None),
            last_success_at=getattr(rocket_cog, "last_tracking_success_at", None),
            now=now,
            stale_after=180,
            active_detail="Polling tracked accounts",
        )
        item_shop_cog = self.bot.get_cog("ItemShopCog")
        item_shop_loop = getattr(item_shop_cog, "poll_item_shop", None)
        item_shop_delivery_loop = getattr(item_shop_cog, "deliver_item_shop_changes", None)
        item_shop_poll_ok, item_shop_poll_detail = background_worker_status(
            running=bool(item_shop_loop is not None and item_shop_loop.is_running()),
            started_at=getattr(item_shop_cog, "worker_started_at", None),
            last_success_at=getattr(item_shop_cog, "last_shop_poll_success_at", None),
            now=now,
            stale_after=15 * 60,
            active_detail="Polling catalogue",
        )
        item_shop_delivery_ok, item_shop_delivery_detail = background_worker_status(
            running=bool(
                item_shop_delivery_loop is not None and item_shop_delivery_loop.is_running()
            ),
            started_at=getattr(item_shop_cog, "worker_started_at", None),
            last_success_at=getattr(item_shop_cog, "last_shop_delivery_success_at", None),
            now=now,
            stale_after=120,
            active_detail="Processing watch deliveries",
        )
        tournament_cog = self.bot.get_cog("TournamentAlertsCog")
        tournament_loop = getattr(tournament_cog, "deliver_tournament_alerts", None)
        tournament_ok, tournament_detail = background_worker_status(
            running=bool(tournament_loop is not None and tournament_loop.is_running()),
            started_at=getattr(tournament_cog, "worker_started_at", None),
            last_success_at=getattr(tournament_cog, "last_tournament_delivery_success_at", None),
            now=now,
            stale_after=180,
            active_detail="Polling schedules and processing reminders",
        )
        voice_alerts_ok = discord_ok and database_ok and self.bot.intents.voice_states
        keyword_notifications_ok = discord_ok and database_ok and self.bot.intents.message_content
        entries = [
            ("Discord gateway", discord_ok, "Operational" if discord_ok else "Unavailable"),
            ("SQLite database", database_ok, "Operational" if database_ok else "Unavailable"),
            (
                "Voice alerts",
                voice_alerts_ok,
                (
                    "Operational"
                    if voice_alerts_ok
                    else (
                        "Required Discord intent unavailable"
                        if not self.bot.intents.voice_states
                        else "Discord or database unavailable"
                    )
                ),
            ),
            (
                "Keyword notifications",
                keyword_notifications_ok,
                (
                    "Operational"
                    if keyword_notifications_ok
                    else (
                        "Required Discord intent unavailable"
                        if not self.bot.intents.message_content
                        else "Discord or database unavailable"
                    )
                ),
            ),
            (
                "Scheduled get-on requests",
                database_ok and geton_ok,
                geton_detail if database_ok else "Database unavailable",
            ),
        ]
        if public_web_status is not None:
            entries.insert(1, ("Public website", *public_web_status))
        if tunnel_status is not None:
            entries.insert(2, ("Cloudflare tunnel", *tunnel_status))
        summaries = self.bot.get_cog("SummaryCog")
        from slopbot.summary_alerts import SummaryCog

        if isinstance(summaries, SummaryCog):
            summary_ok, summary_detail = background_worker_status(
                running=summaries.deliver_summaries.is_running(),
                started_at=summaries.worker_started_at,
                last_success_at=summaries.last_success_at,
                now=now,
                stale_after=300,
                active_detail="Daily/weekly delivery operational",
            )
            entries.append(
                ("Daily and weekly summaries", database_ok and summary_ok, summary_detail)
            )
        lines = [f"{'✅' if ok else '❌'} **{name}:** {detail}" for name, ok, detail in entries]
        telegram_ok = self.bot.telegram is not None and self.bot.telegram.username is not None
        lines.append(
            f"{'✅' if telegram_ok else '⚪' if self.bot.telegram is None else '❌'} "
            f"**Telegram delivery:** {telegram_status}"
        )
        if self.bot.rocket_league is None:
            lines.append("⚪ **Rocket League service:** Not configured")
        elif rocket_status is not None and rocket_status.connected:
            lines.append("✅ **Rocket League service:** Operational and authenticated")
        else:
            lines.append(
                f"❌ **Rocket League service:** {rocket_league_status_detail(rocket_status)}"
            )
        if self.bot.rocket_league is not None:
            lines.append(
                f"{'✅' if tracking_ok else '❌'} **Rocket League tracking:** {tracking_detail}"
            )
            lines.append(
                f"{'✅' if item_shop_poll_ok else '❌'} **Rocket League item-shop catalogue:** "
                f"{item_shop_poll_detail}"
            )
            lines.append(
                f"{'✅' if item_shop_delivery_ok else '❌'} "
                f"**Rocket League item-shop deliveries:** {item_shop_delivery_detail}"
            )
            lines.append(
                f"{'✅' if tournament_ok else '❌'} **Tournament alerts:** {tournament_detail}"
            )
        configured_services_ok = (self.bot.telegram is None or telegram_ok) and (
            self.bot.rocket_league is None
            or bool(
                rocket_status
                and rocket_status.connected
                and tracking_ok
                and item_shop_poll_ok
                and item_shop_delivery_ok
                and tournament_ok
            )
        )
        healthy = all(ok for _name, ok, _detail in entries) and configured_services_ok
        return SlopbotStatusSnapshot(tuple(lines), healthy, now)

    async def _public_web_status(self) -> tuple[bool, str] | None:
        """Probe the user-facing route so a healthy origin cannot hide a failed tunnel."""

        website = getattr(self.bot, "web", None)
        if website is None:
            return None
        session = getattr(website, "http", None)
        origin = getattr(getattr(website, "settings", None), "origin", "")
        if session is None or not origin:
            return False, "Listener or health probe unavailable"
        try:
            async with session.get(
                f"{origin}/healthz",
                headers={"Cache-Control": "no-cache"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    return False, f"Public endpoint returned HTTP {response.status}"
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict) or payload.get("web") != "ok":
                    return False, "Public endpoint returned an invalid health response"
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return False, "Public endpoint or tunnel unavailable"
        return True, "Operational through the public tunnel"

    async def _cloudflare_tunnel_status(self) -> tuple[bool, str] | None:
        """Read cloudflared's loopback metrics when this deployment exposes them."""

        website = getattr(self.bot, "web", None)
        session = getattr(website, "http", None) if website is not None else None
        if session is None:
            return None
        for port in range(20241, 20246):
            try:
                async with session.get(
                    f"http://127.0.0.1:{port}/metrics",
                    timeout=aiohttp.ClientTimeout(total=1),
                ) as response:
                    if response.status != 200:
                        continue
                    count = cloudflare_tunnel_connections(await response.text())
                    if count is None:
                        continue
                    if count < 4:
                        return False, f"Degraded ({count} of 4 connections active)"
                    return True, f"Operational ({count} connections active)"
            except (aiohttp.ClientError, TimeoutError):
                continue
        # Other reverse proxies are supported; the public probe remains authoritative.
        return None

    async def _deliver_status_change(self, snapshot: SlopbotStatusSnapshot) -> None:
        embed = snapshot.embed(alert=True)
        text = f"{embed.title}\n\n" + "\n".join(line.replace("**", "") for line in snapshot.lines)
        for guild_id, user_id in await self.bot.store.list_slopbot_status_user_alerts():
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.get_member(user_id) is None:
                await self.bot.store.set_slopbot_status_user_alert(
                    guild_id=guild_id, user_id=user_id, enabled=False
                )
                continue
            try:
                await send_to_destinations(
                    bot=self.bot,
                    store=self.bot.store,
                    telegram=self.bot.telegram,
                    guild_id=guild_id,
                    recipient_id=user_id,
                    discord_embed=embed,
                    telegram_text=text,
                )
            except Exception:
                LOGGER.exception("Could not deliver personal Slopbot status alert")
        for guild_id, channel_id in await self.bot.store.list_slopbot_status_channel_alerts():
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
                await self.bot.store.set_slopbot_status_channel_alert(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    configured_by_user_id=0,
                    enabled=False,
                )
                continue
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.Forbidden:
                await self.bot.store.set_slopbot_status_channel_alert(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    configured_by_user_id=0,
                    enabled=False,
                )
            except discord.HTTPException:
                LOGGER.exception("Could not deliver channel Slopbot status alert")

    async def _rocket_league_status(self) -> RocketLeagueStatus | None:
        if self.bot.rocket_league is None:
            return None
        try:
            return await self.bot.rocket_league.status()
        except RocketLeagueServiceError:
            return None


def server_configuration_embed(
    *,
    guild_name: str,
    tracking: RocketLeagueTrackingConfig,
    event_notifications: list[tuple[str, int, int]] | None = None,
    tournament_alerts: list[TournamentChannelAlert],
    policies: list[CommandPolicy],
    status_channel_ids: tuple[int, ...] = (),
) -> discord.Embed:
    embed = discord.Embed(
        title="Slopbot server configuration",
        description=f"Configuration summary for **{discord.utils.escape_markdown(guild_name)}**.",
        color=discord.Color.blurple(),
    )
    notification_map = {
        event_key: (channel_id, minimum_count)
        for event_key, channel_id, minimum_count in (event_notifications or [])
    }
    notification_lines = []
    for event_key, label in EVENT_NOTIFICATION_LABELS.items():
        configured = notification_map.get(event_key)
        if configured is None:
            notification_lines.append(f"**{label}:** disabled")
            continue
        minimum = (
            f" • {configured[1]}+ cumulative games"
            if event_key in {"session_streak", "all_time_streak"}
            else ""
        )
        notification_lines.append(f"**{label}:** <#{configured[0]}>{minimum}")
    embed.add_field(
        name="Rocket League event notifications",
        value="\n".join(notification_lines),
        inline=False,
    )
    embed.add_field(
        name="Session streak calculation",
        value=(
            f"**{tracking.inactivity_seconds // 60} minutes** of inactivity ends a session "
            "segment. All-time streaks ignore this timer."
        ),
        inline=False,
    )

    if not tournament_alerts:
        tournament_value = "No text-channel tournament reminders configured."
    else:
        tournament_lines = []
        for alert in tournament_alerts[:8]:
            sizes = "/".join(f"{size}v{size}" for size in alert.team_sizes)
            modes = ", ".join(mode.replace("_", " ") for mode in alert.game_modes)
            tournament_lines.append(
                f"<#{alert.channel_id}> • {alert.region} • {alert.lead_minutes} min • "
                f"button {alert.opt_in_lead_minutes} min • {sizes} • {modes}"
            )
        if len(tournament_alerts) > len(tournament_lines):
            tournament_lines.append(f"…and {len(tournament_alerts) - len(tournament_lines)} more")
        tournament_value = "\n".join(tournament_lines)[:1024]
    embed.add_field(name="Tournament channel reminders", value=tournament_value, inline=False)
    embed.add_field(
        name="Slopbot status alerts",
        value=(
            format_channel_mentions(status_channel_ids)
            if status_channel_ids
            else "No status-alert channels configured."
        ),
        inline=False,
    )

    saved_policies = {policy.command_name: policy for policy in policies}
    policy_lines = []
    for command_name, label in SHAREABLE_COMMANDS.items():
        policy = saved_policies.get(command_name)
        visibility = (
            "Only me"
            if policy is not None and policy.default_visibility is ResponseVisibility.PRIVATE
            else "Public"
        )
        if policy is None or not policy.channel_restricted:
            channels = "all channels"
        elif policy.channel_ids:
            channels = ", ".join(f"<#{channel_id}>" for channel_id in policy.channel_ids[:5])
            if len(policy.channel_ids) > 5:
                channels += f" +{len(policy.channel_ids) - 5}"
        else:
            channels = "blocked everywhere"
        policy_lines.append(f"**{label}:** {visibility} • {channels}")
    embed.add_field(
        name="Result-command policies",
        value="\n".join(policy_lines)[:1024],
        inline=False,
    )
    embed.set_footer(text="Use /setup events and /setup commands to change these settings.")
    return embed


def format_channel_mentions(channel_ids: tuple[int, ...], *, limit: int = 1024) -> str:
    visible: list[str] = []
    for index, channel_id in enumerate(channel_ids):
        remaining_after = len(channel_ids) - index - 1
        suffix = f", … (+{remaining_after} more)" if remaining_after else ""
        candidate = ", ".join([*visible, f"<#{channel_id}>"])
        if len(candidate) + len(suffix) > limit:
            omitted = len(channel_ids) - index
            return f"{', '.join(visible)}, … (+{omitted} more)"
        visible.append(f"<#{channel_id}>")
    return ", ".join(visible)
