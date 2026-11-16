"""Authenticated native report browsing and alert configuration."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from slopbot.summaries import (
    PLAYLISTS,
    SECTIONS,
    defaults,
    period,
    report_csv,
    report_text,
    validate,
)
from slopbot.summary_alerts import SummaryCog, parse_list


class Reports:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.fallback = SummaryCog(server.bot)

    @property
    def cog(self) -> SummaryCog:
        cog = getattr(self.server.bot, "get_cog", lambda _: None)("SummaryCog")
        return cog if isinstance(cog, SummaryCog) else self.fallback

    async def context(
        self, user: dict[str, Any], raw: Any, *, allow_cached: bool = False
    ) -> tuple[int, int, Any, Any]:
        gid = int(raw)
        if gid < 0:
            if gid != -user["id"]:
                raise PermissionError("Personal reports are only available to their owner.")
            return gid, user["id"], None, None
        guild, member, _ = await self.server.context(user, gid, allow_cached=allow_cached)
        return gid, member.id, guild, member

    @staticmethod
    def public(setting: dict[str, Any]) -> dict[str, Any]:
        result = {
            **setting,
            "guild_id": str(setting["guild_id"]),
            "owner_id": str(setting["owner_id"]),
            "config": setting["config"].copy(),
        }
        if result["config"]["channel_id"]:
            result["config"]["channel_id"] = str(result["config"]["channel_id"])
        return result

    async def read(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        query = request.query
        gid, owner, guild, member = await self.context(
            user, query.get("guild_id", -user["id"]), allow_cached=True
        )
        cadence, scope = query.get("cadence", "daily"), query.get("scope", "self")
        if gid < 0 and scope != "self":
            raise PermissionError("Choose a Discord server for a server report.")
        setting = await self.cog.service.settings(gid, owner, cadence, scope)
        p = setting["config"]
        report_view = (await self.server.identities.preferences(user["id"])).get("report_view", {})
        if "timezone" not in query:
            p["timezone"] = report_view.get("timezone", "America/New_York")
        for key in (
            "timezone",
            "sections",
            "playlists",
            "weekday",
            "minimum_streak",
            "minimum_peak_gain",
            "player_order",
        ):
            if key in query:
                p[key] = (
                    parse_list(query[key], numeric=key == "playlists")
                    if key in {"sections", "playlists"}
                    else int(query[key])
                    if key in {"weekday", "minimum_streak", "minimum_peak_gain"}
                    else query[key]
                )
        validate(cadence, scope, p, owner)
        available_accounts = await self.cog.accounts(gid, owner, scope)
        accounts = available_accounts
        if "players" in query:
            selected = set(query["players"].split(","))
            if not selected or "" in selected:
                raise ValueError("Include at least one player.")
            if not selected <= available_accounts.keys():
                raise PermissionError(
                    "One or more selected players are not available in this report."
                )
            accounts = {pid: name for pid, name in available_accounts.items() if pid in selected}
        now = int(time.time())
        today = datetime.fromtimestamp(now, ZoneInfo(p["timezone"])).date()
        date_range = None
        if "range_start" in query or "range_end" in query:
            if not query.get("range_start") or not query.get("range_end"):
                raise ValueError("Choose both From and To dates.")
            first, last = (
                date.fromisoformat(query["range_start"]),
                date.fromisoformat(query["range_end"]),
            )
            if first.isoformat() != query["range_start"] or last.isoformat() != query["range_end"]:
                raise ValueError("Use YYYY-MM-DD dates.")
            date_range = (first, last)
        offset = int(query.get("offset", 1))
        if "date" in query:
            zone = ZoneInfo(p["timezone"])
            current = datetime.fromtimestamp(period(now, cadence, p, 0)[0], zone).date()
            requested = datetime.fromtimestamp(int(query["date"]), zone).date()
            difference = (current - requested).days
            offset = difference if cadence == "daily" else difference // 7
            if difference < 0:
                raise ValueError("Choose a report date within retained history.")
        report = await self.cog.service.report(
            gid,
            accounts,
            cadence,
            p,
            now,
            offset,
            date_range=date_range,
        )
        admin = bool(member and member.guild_permissions.manage_guild)
        return web.json_response(
            dict(
                report=report,
                offset=offset,
                available_players=[
                    {"id": pid, "name": name} for pid, name in available_accounts.items()
                ],
                selected_players=list(accounts),
                date_limits={
                    "min": (today - timedelta(days=29)).isoformat(),
                    "max": today.isoformat(),
                },
                date_range={"start": date_range[0].isoformat(), "end": date_range[1].isoformat()}
                if date_range
                else None,
                full_text=report_text(report, compact=False),
                csv=report_csv(report),
                config=p,
                sections=SECTIONS,
                playlists=PLAYLISTS,
                personal_settings=[
                    self.public(s) for s in await self.cog.service.list_settings(gid, owner)
                ],
                server_settings=[
                    self.public(s) for s in await self.cog.service.channel_settings(gid)
                ]
                if admin
                else [],
                deliveries=await self.cog.service.deliveries(gid, owner),
                server_deliveries=await self.cog.service.deliveries(gid, 0, channels=True)
                if admin
                else [],
                admin=admin,
                name=guild.name if guild else "Personal tracking",
                guild_id=str(gid),
                channels=[
                    dict(id=str(c.id), name=c.name)
                    for c in guild.text_channels
                    if c.permissions_for(member).view_channel
                ]
                if admin
                else [],
                periods=[
                    dict(
                        offset=i,
                        start=period(now, cadence, p, i)[0],
                        end=period(now, cadence, p, i)[1],
                    )
                    for i in range(31 if cadence == "daily" else 5)
                ],
            )
        )

    async def save(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {
            "replace_instant",
            "schedule_id",
            "action",
        } != {
            "guild_id",
            "cadence",
            "scope",
            "target",
            "config",
        }:
            raise ValueError("Invalid summary settings.")
        gid, owner, guild, member = await self.context(user, data["guild_id"])
        replace_instant = data.get("replace_instant", False)
        if type(replace_instant) is not bool or (replace_instant and data["target"] != "channel"):
            raise ValueError(
                "Replacing instant alerts is only available for server channel summaries."
            )
        if data["target"] not in {"personal", "channel"}:
            raise ValueError("Choose a personal or channel alert.")
        if not isinstance(data["config"], dict) or set(data["config"]) - set(defaults()):
            raise ValueError("Invalid summary configuration.")
        p = {**defaults(), **data["config"]}
        if p["channel_id"] is not None:
            if not isinstance(p["channel_id"], str) or not p["channel_id"].isdigit():
                raise ValueError("Choose a channel.")
            p["channel_id"] = int(p["channel_id"])
        schedule_id = int(data.get("schedule_id", 0))
        if not -(2**63) < schedule_id <= 0 or (schedule_id and data["target"] != "channel"):
            raise ValueError("Invalid channel schedule.")
        channel_alert = data["target"] == "channel"
        if channel_alert and (member is None or not member.guild_permissions.manage_guild):
            raise PermissionError("Manage Server is required for channel summaries.")
        p = validate(data["cadence"], data["scope"], p, schedule_id if channel_alert else owner)
        action = data.get("action", "save")
        if action not in {"save", "preview", "send_test"}:
            raise ValueError("Invalid report action.")
        if action == "preview":
            report = await self.cog.service.report(
                gid,
                await self.cog.accounts(gid, owner, data["scope"]),
                data["cadence"],
                p,
                int(time.time()),
            )
            return web.json_response(
                dict(
                    report=report,
                    text=report_text(report),
                    full_text=report_text(report, compact=False),
                    sections=SECTIONS,
                    playlists=PLAYLISTS,
                )
            )
        if action == "send_test":
            if guild is None:
                raise ValueError("Choose a Discord server to send a test report.")
            return web.json_response(
                await self.cog.send_preview(
                    guild, member, data["cadence"], data["scope"], p, channel_alert=channel_alert
                )
            )
        if gid < 0:
            if data["target"] != "personal":
                raise PermissionError("Channel alerts require Manage Server in a Discord server.")
            result = await self.cog.service.save(
                gid, owner, data["cadence"], data["scope"], p, int(time.time())
            )
        else:
            result = await self.cog.configure(
                guild,
                member,
                data["cadence"],
                data["scope"],
                p,
                channel_alert=data["target"] == "channel",
                replace_instant=replace_instant,
                schedule_id=schedule_id,
            )
        return web.json_response(dict(ok=True, setting=self.public(result)))

    async def snapshot(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        gid, owner, guild, member = await self.context(
            user, request.query["guild_id"], allow_cached=True
        )
        requested_owner = int(request.query["owner"])
        if requested_owner != owner and not (
            requested_owner <= 0 and member and member.guild_permissions.manage_guild
        ):
            raise PermissionError("This report snapshot is not available to this account.")
        report = await self.cog.service.snapshot(
            gid, requested_owner, int(request.query["delivery"])
        )
        if not report or report.get("generated_at", 0) < time.time() - 30 * 86400:
            raise web.HTTPNotFound(text="This snapshot is no longer available.")
        accounts = await self.cog.accounts(gid, owner, report.get("scope", "self"))
        if "profile_ids" not in report or not set(report["profile_ids"]) <= set(accounts):
            raise PermissionError("Access to this snapshot's tracked accounts has changed.")
        return web.json_response(
            dict(
                report=report,
                sections=SECTIONS,
                playlists=PLAYLISTS,
                full_text=report_text(report, compact=False),
                csv=report_csv(report),
                name=guild.name if guild else "Personal tracking",
            )
        )
