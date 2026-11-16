"""Website settings reads and atomic policy editing over shared bot storage."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

from aiohttp import web

from slopbot.database import SubscriptionStore
from slopbot.engagement import MAX_KEYWORD_LENGTH
from slopbot.models import (
    MAX_ITEM_SHOP_WATCHES_PER_USER,
    MAX_SUBSCRIPTIONS_PER_USER,
    KeywordNotificationMode,
    ResponseVisibility,
)
from slopbot.rocketleague import EVENT_NOTIFICATION_LABELS
from slopbot.sessions import Sessions
from slopbot.visibility import SHAREABLE_COMMANDS


class Controls:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.store: SubscriptionStore = server.bot.store

    async def status(self, request: web.Request) -> web.Response:
        await self.server.current_user(request)
        cog = self.server.bot.get_cog("StatusCog")
        snapshot = getattr(cog, "_latest", None)
        return web.json_response(
            {
                "discord": self.server.bot.is_ready(),
                "checked_at": snapshot.checked_at if snapshot else None,
                "healthy": snapshot.healthy if snapshot else None,
                "stale": snapshot is None or time.time() - snapshot.checked_at > 300,
                "lines": list(snapshot.lines) if snapshot else [],
            }
        )

    async def read(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        guild, member, _ = await self.server.context(
            user, int(request.match_info["guild_id"]), allow_cached=True
        )
        admin = bool(member.guild_permissions.manage_guild)
        args = {"guild_id": guild.id, "user_id": member.id}
        delivery, downtime, telegram, geton, keywords = await asyncio.gather(
            self.store.get_delivery_method(**args),
            self.store.get_notification_downtime(**args),
            self.store.get_telegram_chat_id(discord_user_id=member.id),
            self.store.get_geton_enabled(**args),
            self.store.get_keyword_profile(**args),
        )
        voice, tournament, watches, status = await asyncio.gather(
            self.store.list_for_user(**args),
            self.store.get_tournament_user_alert(**args),
            self.store.list_item_shop_watches(**args),
            self.store.has_slopbot_status_user_alert(**args),
        )
        result: dict[str, Any] = {
            "admin": admin,
            "sessions": await Sessions(self.store).settings(guild.id, member.id),
            "delivery": delivery.value,
            "downtime": asdict(downtime),
            "telegram_linked": telegram is not None,
            "geton": geton,
            "keywords": asdict(keywords),
            "voice": [asdict(v) for v in voice],
            "tournament": asdict(tournament) if tournament else None,
            "watches": [asdict(w) for w in watches],
            "item_shop_watch_limit": MAX_ITEM_SHOP_WATCHES_PER_USER,
            "status_alerts": status,
        }
        if admin:
            policies, events, tracking, status_channels, tournaments = await asyncio.gather(
                self.store.list_command_policies(guild_id=guild.id),
                self.store.list_rocket_league_event_notification_configs(guild_id=guild.id),
                self.store.get_rocket_league_tracking_config(guild_id=guild.id),
                self.store.list_slopbot_status_channel_alerts_for_guild(guild_id=guild.id),
                self.store.list_tournament_channel_alerts_for_guild(guild_id=guild.id),
            )
            result.update(
                policies=[
                    {**asdict(p), "channel_ids": [str(c) for c in p.channel_ids]} for p in policies
                ],
                commands=SHAREABLE_COMMANDS,
                event_labels=EVENT_NOTIFICATION_LABELS,
                events=[[e, str(c), n] for e, c, n in events],
                tracking=asdict(tracking),
                server_sessions=await Sessions(self.store).settings(guild.id, 0),
                status_channels=[str(c) for c in status_channels],
                tournament_channels=[
                    {**asdict(t), "channel_id": str(t.channel_id)} for t in tournaments
                ],
            )
        if admin and result["server_sessions"]["channel_id"]:
            result["server_sessions"]["channel_id"] = str(result["server_sessions"]["channel_id"])
        # Snowflakes must stay strings through JavaScript.
        for v in result["voice"]:
            v["channel_id"] = str(v["channel_id"])
        result["keywords"]["channel_ids"] = [str(c) for c in keywords.channel_ids]
        return web.json_response(result)

    async def policy(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        data = await request.json()
        guild, member, _ = await self.server.context(user, int(request.match_info["guild_id"]))
        if not member.guild_permissions.manage_guild:
            raise PermissionError("Manage Server is required to edit command permissions.")
        if not isinstance(data, dict) or set(data) != {
            "command",
            "visibility",
            "restricted",
            "channels",
        }:
            raise ValueError("Invalid command policy.")
        if not isinstance(data["command"], str) or data["command"] not in SHAREABLE_COMMANDS:
            raise ValueError("Unknown result command.")
        visibility = ResponseVisibility(data["visibility"])
        if type(data["restricted"]) is not bool or not isinstance(data["channels"], list):
            raise ValueError("Invalid channel policy.")
        if len(data["channels"]) > 500 or any(
            not isinstance(c, str) or not c.isdigit() for c in data["channels"]
        ):
            raise ValueError("Invalid channels.")
        ids = list(dict.fromkeys(int(c) for c in data["channels"]))
        for channel_id in ids:
            channel = guild.get_channel(channel_id)
            if (
                channel is None
                or channel.type.value != 0
                or not channel.permissions_for(member).view_channel
            ):
                raise PermissionError("Choose accessible text channels in this server.")
        await self.store.replace_command_policy(
            guild_id=guild.id,
            command_name=data["command"],
            visibility=visibility,
            restricted=data["restricted"],
            channel_ids=ids,
        )
        return web.json_response({"ok": True})

    async def personal(self, request: web.Request) -> web.Response:
        user = await self.server.current_user(request)
        guild, member, _ = await self.server.context(user, int(request.match_info["guild_id"]))
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid settings.")
        action = data.get("action")
        args = {"guild_id": guild.id, "user_id": member.id}

        def flag(name: str) -> bool:
            if type(data.get(name)) is not bool:
                raise ValueError("Invalid switch setting.")
            return bool(data[name])

        def channels(values: Any, types: set[int]) -> list[int]:
            if not isinstance(values, list) or len(values) > 500:
                raise ValueError("Choose valid channels.")
            ids = []
            for value in values:
                if not isinstance(value, str) or not value.isdigit():
                    raise ValueError("Invalid channel.")
                channel = guild.get_channel(int(value))
                if (
                    channel is None
                    or channel.type.value not in types
                    or not channel.permissions_for(member).view_channel
                ):
                    raise PermissionError("That channel is not accessible.")
                ids.append(channel.id)
            return list(dict.fromkeys(ids))

        if action == "keyword_options":
            enabled, mode = flag("enabled"), KeywordNotificationMode(data.get("mode"))
            ids = channels(data.get("channels"), {0, 5})
            await self.store.set_keyword_channels(**args, channel_ids=ids)
            await self.store.set_keyword_notification_mode(**args, notification_mode=mode)
            await self.store.set_keyword_enabled(**args, enabled=enabled)
        elif action in {"keyword_add", "keyword_remove"}:
            keyword = data.get("keyword")
            if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= MAX_KEYWORD_LENGTH:
                raise ValueError("Enter a valid keyword.")
            if action == "keyword_add":
                await self.store.add_keyword(**args, keyword=keyword.strip())
            else:
                await self.store.remove_keyword(**args, keyword=keyword.strip())
        elif action == "voice":
            ids = channels([data.get("channel")], {2, 13})
            reach, drop = flag("reach"), flag("drop")
            threshold = data.get("threshold")
            if type(threshold) is not int:
                raise ValueError("Enter a whole-number threshold.")
            current = await self.store.list_for_user(**args)
            if len(current) >= MAX_SUBSCRIPTIONS_PER_USER and not any(
                v.channel_id == ids[0] for v in current
            ):
                raise ValueError("You have reached the voice alert limit.")
            await self.store.save(
                **args,
                channel_id=ids[0],
                threshold=threshold,
                notify_on_reach=reach,
                notify_on_drop=drop,
            )
        elif action == "voice_remove":
            if type(data.get("id")) is not int:
                raise ValueError("Invalid voice alert.")
            await self.store.delete_for_user(**args, subscription_id=data["id"])
        elif action == "itemshop_watch":
            if self.server.bot.rocket_league is None:
                raise ValueError("Rocket League shop alerts are not configured on this bot.")
            query = data.get("query", "")
            max_price = data.get("max_price")
            if not isinstance(query, str):
                raise ValueError("Enter a valid item search.")
            if max_price is not None and type(max_price) is not int:
                raise ValueError("Enter a whole-number maximum price.")
            await self.store.save_item_shop_watch(
                **args,
                query=query,
                max_price=max_price,
            )
        elif action == "itemshop_unwatch":
            watch_id = data.get("id")
            if type(watch_id) is not int or watch_id < 1:
                raise ValueError("Invalid item-shop watch.")
            removed = await self.store.delete_item_shop_watch(
                **args,
                watch_id=watch_id,
            )
            if not removed:
                raise ValueError("That item-shop watch no longer exists.")
        else:
            raise ValueError("Unknown setting.")
        return web.json_response({"ok": True})
