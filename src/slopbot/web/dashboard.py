"""Native web reads over authorized existing tracking records; no Discord interactions."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

import discord

from slopbot.database import SubscriptionStore
from slopbot.models import RocketLeagueAccount
from slopbot.rank_estimates import estimate_rank_steps
from slopbot.rocketleague import PLAYLIST_NAMES, RocketLeagueSkill
from slopbot.sessions import Sessions

WIDGETS = ("ranks", "trend", "results", "streaks", "activity", "rewards", "club")


@dataclass
class ProfileSource:
    account: RocketLeagueAccount
    label: str
    first_observed: int | None = None

    @property
    def key(self) -> str:
        return str(self.account.guild_id)

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "personal": self.account.guild_id < 0,
            "first_observed": self.first_observed,
        }


class Dashboard:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.bot = server.bot
        self.store: SubscriptionStore = self.bot.store
        self.cache: dict[str, tuple[float, Any]] = {}
        self.pending: dict[str, asyncio.Task[Any]] = {}
        self.estimate_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
        self.live_slots = asyncio.Semaphore(4)

    def available(self, key: str, fn: Any) -> Any:
        """Return recent data immediately and coalesce background refreshes by key."""
        now = time.monotonic()
        cached = self.cache.get(key)
        if (not cached or cached[0] < now - 30) and key not in self.pending:

            async def refresh() -> None:
                try:
                    async with self.live_slots:
                        value = await asyncio.wait_for(fn(), timeout=12)
                    self.cache = {
                        k: v for k, v in self.cache.items() if v[0] > time.monotonic() - 120
                    }
                    self.cache[key] = (time.monotonic(), value)
                except Exception:
                    # The regular tracker remains the source of saved observations.
                    pass
                finally:
                    self.pending.pop(key, None)

            self.pending[key] = asyncio.create_task(refresh())
        return cached[1] if cached and cached[0] > now - 120 else None

    async def close(self) -> None:
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def sources(
        self,
        user: dict[str, Any],
        *,
        desired: str | None = None,
        memberships: list[dict[str, Any]] | None = None,
    ) -> list[ProfileSource]:
        uid = user["id"]
        personal = await self.store.get_rocket_league_account(guild_id=-uid, discord_user_id=uid)
        preferences = await self.server.identities.preferences(uid)
        wanted = {
            p
            for p in (desired, preferences.get("player"), personal.player_id if personal else None)
            if p
        }
        result = [ProfileSource(personal, "Personal tracking")] if personal else []
        if memberships is None:
            memberships = await self.memberships(user)
        candidates: list[ProfileSource] = []
        if user.get("discord_id"):
            for membership in memberships:
                guild = self.bot.get_guild(int(membership["id"]))
                if guild is None:
                    continue
                accounts = await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
                for account in accounts:
                    if account.unlinked or guild.get_member(account.discord_user_id) is not None:
                        candidates.append(ProfileSource(account, guild.name))
                        if account.discord_user_id == int(user["discord_id"]):
                            wanted.add(account.player_id)
        result.extend(s for s in candidates if s.account.player_id in wanted)
        # Prefer the source with the earliest known history. Do not copy/merge records:
        # server-specific streak/session rules and incomplete observations can differ.
        observed = await asyncio.gather(*(self.first_observed(source.account) for source in result))
        for source, first_observed in zip(result, observed, strict=True):
            source.first_observed = first_observed
        result.sort(key=lambda s: (s.first_observed or 2**63, s.account.guild_id < 0, s.key))
        return result

    async def memberships(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        if not user.get("discord_id") or not self.bot.is_ready():
            return []

        async def membership(guild: Any) -> dict[str, Any] | None:
            try:
                _, member, _ = await self.server.context(user, guild.id, allow_cached=True)
            except (discord.NotFound, discord.Forbidden, PermissionError):
                return None
            return {
                "id": str(guild.id),
                "name": guild.name,
                "admin": member.guild_permissions.manage_guild,
            }

        rows = await asyncio.gather(*(membership(guild) for guild in self.bot.guilds))
        return [row for row in rows if row is not None]

    async def session_players(
        self, user: dict[str, Any], memberships: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return linked server members whose server sessions this user may view."""
        discord_id = int(user.get("discord_id") or 0)
        if not discord_id:
            return []
        result: list[dict[str, Any]] = []
        for membership in memberships:
            guild = self.bot.get_guild(int(membership["id"]))
            if guild is None:
                continue
            accounts = await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
            for account in accounts:
                if account.unlinked:
                    continue
                member = guild.get_member(account.discord_user_id)
                if member is None or getattr(member, "bot", False):
                    continue
                member_name = getattr(member, "display_name", getattr(member, "name", "member"))
                label_name = "You" if account.discord_user_id == discord_id else member_name
                result.append(
                    {
                        "key": f"{guild.id}:{account.discord_user_id}",
                        "guild_id": guild.id,
                        "user_id": account.discord_user_id,
                        "player_id": account.player_id,
                        "member_name": member_name,
                        "player_name": account.player_name,
                        "self": account.discord_user_id == discord_id,
                        "label": f"{label_name} · {guild.name}",
                    }
                )
        result.sort(key=lambda player: player["label"].casefold())
        return result

    async def session_overview(
        self,
        memberships: list[dict[str, Any]],
        players: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return each shared member's current session, or their latest one."""
        now = int(time.time())
        allowed = {(p["guild_id"], p["player_id"]): p for p in players}
        sessions = Sessions(self.store)
        result: list[dict[str, Any]] = []
        for membership in memberships:
            guild = self.bot.get_guild(int(membership["id"]))
            if guild is None:
                continue
            for row in await sessions.latest_server_sessions(guild.id, now=now):
                player = allowed.get((guild.id, row["player_id"]))
                if player is None:
                    continue
                result.append(
                    {
                        **row,
                        "target_key": player["key"],
                        "member_name": player["member_name"],
                        "guild_name": guild.name,
                        "active": not row["closed"] and now <= row["last_at"] + row["cutoff"],
                        "rank_estimates": await self.rank_estimates(
                            RocketLeagueAccount(
                                guild.id,
                                player["user_id"],
                                player["player_id"],
                                player["player_name"],
                            )
                        ),
                    }
                )
        result.sort(
            key=lambda row: (not row["active"], -row["last_at"], row["member_name"].casefold())
        )
        return result

    async def first_observed(self, account: RocketLeagueAccount) -> int | None:
        def read(c: Any) -> int | None:
            row = c.execute(
                "SELECT MIN(t) FROM ("
                "SELECT MIN(observed_at) t FROM rocket_league_mmr_history "
                "WHERE guild_id=? AND player_id=? UNION ALL "
                "SELECT MIN(observed_at) t FROM rocket_league_peak_mmr "
                "WHERE guild_id=? AND player_id=?)",
                (account.guild_id, account.player_id, account.guild_id, account.player_id),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None

        return await self.store._run(read)

    async def samples(self, account: RocketLeagueAccount) -> list[dict[str, Any]]:
        def read(c: Any) -> list[dict[str, Any]]:
            rows = c.execute(
                "SELECT playlist,mmr,tier,division,rank,win_streak,matches_played,observed_at "
                "FROM rocket_league_activity_samples WHERE guild_id=? AND player_id=?",
                (account.guild_id, account.player_id),
            ).fetchall()
            return [
                dict(
                    zip(
                        (
                            "playlist",
                            "mmr",
                            "tier",
                            "division",
                            "rank",
                            "win_streak",
                            "matches_played",
                            "observed_at",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in rows
            ]

        return await self.store._run(read)

    async def rank_estimates(
        self, account: RocketLeagueAccount, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return authorized aggregate estimates for one visible profile."""
        cache_key = (account.guild_id, account.player_id)
        cached = self.estimate_cache.get(cache_key)
        now = time.monotonic()
        if details is None and cached and cached[0] > now - 120:
            return cached[1]
        saved = {row["playlist"]: row for row in await self.samples(account)}
        live = {row["playlist"]: row for row in (details or {}).get("skills", [])}
        result: dict[str, Any] = {}
        for playlist in PLAYLIST_NAMES:
            row = live.get(playlist) or saved.get(playlist)
            if not row or row.get("tier") is None or row.get("division") is None:
                continue
            skill = RocketLeagueSkill(
                account.player_id,
                float(row["mmr"]),
                int(row["tier"]),
                int(row["division"]),
                str(row.get("rank") or "Unranked"),
                int(row.get("win_streak", 0)),
                int(row.get("matches_played", 0)),
                int(row.get("placement_matches_played", 0)),
                playlist,
            )
            estimate = await estimate_rank_steps(
                self.store,
                skill=skill,
                playlist=playlist,
                player_id=account.player_id,
                observed_at=None if playlist in live else int(row["observed_at"]),
            )
            if estimate is not None:
                result[str(playlist)] = estimate.public()
        if details is None:
            if len(self.estimate_cache) > 500:
                self.estimate_cache = {
                    key: value for key, value in self.estimate_cache.items() if value[0] > now - 120
                }
            self.estimate_cache[cache_key] = (time.monotonic(), result)
        return result

    async def server_activity(
        self, guild: Any, *, since: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return activity for tracked members who are still visible in this server."""
        accounts = await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
        eligible = {
            account.player_id
            for account in accounts
            if account.unlinked or guild.get_member(account.discord_user_id) is not None
        }
        events = await self.store.list_rocket_league_events(
            guild_id=guild.id, since=since, limit=limit
        )
        return [asdict(event) for event in events if event.player_id in eligible]

    async def activity(self, user: dict[str, Any], query: Any) -> dict[str, Any]:
        """Read the activity feed without loading unrelated leaderboard or live stats."""
        guild, _, _ = await self.server.context(user, int(query["guild_id"]), allow_cached=True)
        days = int(query.get("days", 7))
        if days not in {1, 7, 14, 30}:
            raise ValueError("Choose a supported time range.")
        return {
            "guild": guild.name,
            "events": await self.server_activity(
                guild, since=int(time.time()) - days * 86400, limit=100
            ),
            "playlists": PLAYLIST_NAMES,
            "days": days,
        }

    async def data(self, user: dict[str, Any], query: Any) -> dict[str, Any]:
        view = query.get("view", "full")
        if view not in {"full", "overview", "history", "sessions", "account"}:
            raise ValueError("Choose a supported dashboard view.")
        prefs = await self.server.identities.preferences(user["id"])
        days = int(query.get("days", prefs.get("days", 7)))
        playlist = int(query.get("playlist", prefs.get("playlist", 11)))
        if days not in {1, 7, 14, 30} or playlist not in PLAYLIST_NAMES:
            raise ValueError("Choose a supported playlist and time range.")
        since = int(time.time()) - days * 86400
        guilds = await self.memberships(user)
        sources = await self.sources(user, memberships=guilds)
        include_session_directory = view in {"full", "sessions"}
        include_history_directory = view in {"full", "history"}
        tracked_players = (
            await self.session_players(user, guilds)
            if include_session_directory or include_history_directory
            else []
        )
        session_players = tracked_players if include_session_directory else []
        history_players = tracked_players if include_history_directory else []
        session_overview = (
            await self.session_overview(guilds, session_players)
            if include_session_directory
            else []
        )
        session_target_key = query.get("session_player") if include_session_directory else None
        session_target = next(
            (player for player in session_players if player["key"] == session_target_key), None
        )
        if session_target_key and session_target is None:
            raise PermissionError("That server member is no longer accessible.")
        history_target_key = query.get("history_player") if include_history_directory else None
        history_target = next(
            (player for player in history_players if player["key"] == history_target_key), None
        )
        if history_target_key and history_target is None:
            raise PermissionError("That server member is no longer accessible.")
        grouped: dict[str, dict[str, Any]] = {}
        for candidate in sources:
            a = candidate.account
            group = grouped.setdefault(
                a.player_id, {"player_id": a.player_id, "name": a.player_name, "sources": []}
            )
            group["sources"].append(candidate.public())
        result: dict[str, Any] = {
            "profiles": list(grouped.values()),
            "guilds": guilds,
            "preferences": prefs,
            "playlists": PLAYLIST_NAMES,
            "account": None,
            "days": days,
            "playlist": playlist,
            "session_players": session_players,
            "session_overview": session_overview,
            "session_target": session_target,
            "history_players": history_players,
            "history_target": history_target,
            "discord_pending": bool(user.get("discord_id") and not self.bot.is_ready()),
        }
        if view == "account":
            return result
        if not sources and session_target is None and history_target is None:
            return result
        if not sources and session_target is not None and history_target is None:
            sessions = Sessions(self.store)
            target_account = RocketLeagueAccount(
                session_target["guild_id"],
                session_target["user_id"],
                session_target["player_id"],
                session_target["player_name"],
            )
            result.update(
                sessions=await sessions.history(
                    session_target["guild_id"], session_target["player_id"], 0, since=since
                ),
                session_scope="server",
                session_own=False,
                session_settings=None,
                session_server_settings=await sessions.settings(session_target["guild_id"], 0),
                session_now=int(time.time()),
                rank_estimates={},
                session_rank_estimates=await self.rank_estimates(target_account),
            )
            return result
        if history_target is not None:
            a = RocketLeagueAccount(
                history_target["guild_id"],
                history_target["user_id"],
                history_target["player_id"],
                history_target["player_name"],
            )
            guild = self.bot.get_guild(a.guild_id)
            if guild is None:
                raise PermissionError("That server member is no longer accessible.")
            source = ProfileSource(a, guild.name, await self.first_observed(a))
        else:
            requested = query.get("player") or prefs.get("player")
            if query.get("player") and requested not in grouped:
                raise PermissionError("That profile is not available to your account.")
            player = requested if requested in grouped else sources[0].account.player_id
            available = [s for s in sources if s.account.player_id == player]
            selected = query.get("source") or (
                prefs.get("source") if prefs.get("player") == player else None
            )
            matching_source = next((s for s in available if s.key == selected), None)
            if query.get("source") and matching_source is None:
                raise PermissionError("That history source is no longer accessible.")
            source = matching_source or available[0]
            a = source.account
        result.update(account=asdict(a), source=source.public())

        include_history = view in {"full", "overview", "history"}
        if include_history:
            histories = {
                str(pid): await self.store.list_rocket_league_mmr_history(
                    guild_id=a.guild_id,
                    player_id=a.player_id,
                    playlist=pid,
                    since=since,
                )
                for pid in PLAYLIST_NAMES
            }
            matches = await self.store.list_rocket_league_match_history(
                guild_id=a.guild_id,
                discord_user_id=a.discord_user_id,
                since=since,
            )
            events = await self.store.list_rocket_league_events(
                guild_id=a.guild_id,
                discord_user_id=a.discord_user_id,
                since=since,
                limit=500,
                event_keys=(
                    prefs.get("history_sections")
                    if view == "history" or query.get("history") == "true"
                    else None
                ),
            )
            result.update(
                histories=histories,
                history=histories[str(playlist)],
                matches=[asdict(match) for match in matches],
                events=[asdict(event) for event in events],
                current_streaks=[
                    asdict(streak)
                    for streak in await self.store.list_rocket_league_current_streaks(
                        guild_id=a.guild_id, discord_user_id=a.discord_user_id
                    )
                ],
            )
        if view == "history":
            return result

        include_overview = view in {"full", "overview"}
        details = None
        if include_overview and self.bot.rocket_league:
            live = self.available(
                "details:" + a.player_id, lambda: self.bot.rocket_league.player_details(a.player_id)
            )
            details = asdict(live) if live is not None else None
        profile_estimates = await self.rank_estimates(a, details) if include_overview else {}
        if include_overview:
            peaks = []
            for pid in PLAYLIST_NAMES:
                peak = await self.store.get_rocket_league_peak_mmr(
                    guild_id=a.guild_id, player_id=a.player_id, playlist=pid
                )
                if peak:
                    peaks.append(asdict(peak))
            server_activity = None
            if a.guild_id > 0:
                guild = self.bot.get_guild(a.guild_id)
                if guild is not None:
                    server_activity = {
                        "guild_id": str(guild.id),
                        "name": guild.name,
                        "events": await self.server_activity(guild, since=since),
                    }
            records = await self.store.list_rocket_league_player_records(
                guild_id=a.guild_id, player_id=a.player_id
            )
            result.update(
                peaks=peaks,
                server_activity=server_activity,
                records=[asdict(record) for record in records],
                samples=await self.samples(a),
                details=details,
                rank_estimates=profile_estimates,
                live_available=bool(self.bot.rocket_league),
                service_error=(
                    None
                    if details is not None
                    else (
                        "Showing saved observations. Live details refresh automatically "
                        "when available."
                        if self.bot.rocket_league
                        else "Showing saved observations. Live details are currently unavailable."
                    )
                ),
            )

        include_sessions = view in {"full", "overview", "sessions"}
        if not include_sessions:
            return result
        estimate_account = a
        estimate_details = details
        if session_target is not None and (
            session_target["player_id"],
            session_target["guild_id"],
        ) != (a.player_id, a.guild_id):
            estimate_account = RocketLeagueAccount(
                session_target["guild_id"],
                session_target["user_id"],
                session_target["player_id"],
                session_target["player_name"],
            )
            estimate_details = None
        sessions = Sessions(self.store)
        own = a.guild_id < 0 or a.discord_user_id == int(user.get("discord_id") or 0)
        scope = query.get("session_scope", "personal" if own else "server")
        if scope not in {"personal", "server"} or (scope == "personal" and not own):
            raise PermissionError("Personal sessions are only available to their owner.")
        session_server_settings: dict[str, Any] | None = None
        if session_target is not None:
            session_rows = await sessions.history(
                session_target["guild_id"], session_target["player_id"], 0, since=since
            )
            session_scope = "server"
            session_own = False
            session_settings = None
            session_server_settings = await sessions.settings(session_target["guild_id"], 0)
        else:
            owner = a.discord_user_id if scope == "personal" or a.guild_id < 0 else 0
            session_rows = await sessions.history(
                a.guild_id,
                a.player_id,
                owner,
                since=since,
                limit=1 if view == "overview" else 200,
            )
            session_scope = scope
            session_own = own
            session_settings = (
                await sessions.settings(a.guild_id, a.discord_user_id)
                if own and view != "overview"
                else None
            )
            session_server_settings = (
                await sessions.settings(a.guild_id, 0)
                if a.guild_id > 0 and view != "overview"
                else None
            )
        session_estimates = (
            profile_estimates
            if estimate_account is a and include_overview
            else await self.rank_estimates(estimate_account, estimate_details)
        )
        result.update(
            sessions=session_rows,
            session_scope=session_scope,
            session_own=session_own,
            session_settings=session_settings,
            session_server_settings=session_server_settings,
            session_now=int(time.time()),
            session_rank_estimates=session_estimates,
        )
        return result

    async def board(self, user: dict[str, Any], query: Any) -> dict[str, Any]:
        guild, member, _ = await self.server.context(
            user, int(query["guild_id"]), allow_cached=True
        )
        playlist, days = int(query.get("playlist", 11)), int(query.get("days", 7))
        if playlist not in PLAYLIST_NAMES or days not in {1, 7, 14, 30}:
            raise ValueError("Choose a supported playlist and time range.")
        accounts = [
            a
            for a in await self.store.list_all_rocket_league_accounts(guild_id=guild.id)
            if a.unlinked or guild.get_member(a.discord_user_id) is not None
        ]
        skills = {}
        live = False
        if self.bot.rocket_league and accounts:
            ids = sorted({a.player_id for a in accounts})

            async def fetch() -> Any:
                rows = []
                for offset in range(0, len(ids), 500):
                    rows.extend(
                        await self.bot.rocket_league.skills(
                            playlist=playlist, player_ids=ids[offset : offset + 500]
                        )
                    )
                return rows

            latest = self.available(f"board:{guild.id}:{playlist}:{','.join(ids)}", fetch)
            if latest is not None:
                skills = {s.player_id: asdict(s) for s in latest}
                live = True
        movers = {
            r[0]: r[4] - r[3]
            for r in await self.store.list_rocket_league_movers(
                guild_id=guild.id,
                playlist=playlist,
                since=int(time.time()) - days * 86400,
                limit=None,
            )
        }
        rows = []
        for a in accounts:
            sample = next((s for s in await self.samples(a) if s["playlist"] == playlist), None)
            skill = skills.get(a.player_id) or sample
            peak = await self.store.get_rocket_league_peak_mmr(
                guild_id=guild.id, player_id=a.player_id, playlist=playlist
            )
            rows.append(
                {
                    "player_id": a.player_id,
                    "name": a.player_name,
                    "skill": skill,
                    "live": a.player_id in skills,
                    "peak": asdict(peak) if peak else None,
                    "change": movers.get(a.player_id),
                    "you": a.discord_user_id == member.id,
                }
            )
        rows.sort(key=lambda r: (-(r["skill"]["mmr"] if r["skill"] else -1), r["name"].casefold()))
        eligible = {a.player_id for a in accounts}
        records = {}
        for scope in ("main", "all"):
            for outcome in ("win", "loss"):
                records[f"{scope}:{outcome}"] = [
                    asdict(r)
                    for r in await self.store.list_rocket_league_records(
                        guild_id=guild.id,
                        streak_class=scope,
                        result=outcome,
                        limit=500,
                    )
                    if r.player_id in eligible
                ]
        events = await self.server_activity(guild, since=int(time.time()) - days * 86400, limit=100)
        return {
            "rows": rows,
            "events": events,
            "records": records,
            "live": live,
            "live_available": bool(self.bot.rocket_league and accounts),
            "guild": guild.name,
            "playlists": PLAYLIST_NAMES,
            "playlist": playlist,
            "days": days,
        }
