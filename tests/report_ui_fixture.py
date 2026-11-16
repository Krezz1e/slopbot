"""Local browser-test server: real HTTP/auth/storage with synthetic Discord and RL data."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from aiohttp import web

from slopbot.database import SubscriptionStore
from slopbot.summary_alerts import SummaryCog
from slopbot.web.server import WebServer, WebSettings


async def main():
    directory = Path(tempfile.mkdtemp(prefix="report-ui-", dir="work"))
    store = SubscriptionStore(directory / "bot.db")
    await store.initialize()
    members = {
        uid: SimpleNamespace(
            id=uid,
            name=name,
            display_name=name,
            bot=False,
            guild_permissions=discord.Permissions(manage_guild=uid == 9001),
        )
        for uid, name in ((9001, "Krezzle"), (9002, "Alex"), (9003, "Jamie"))
    }
    guild = SimpleNamespace(
        id=100,
        name="Friday Night",
        me=members[9001],
        get_member=members.get,
        fetch_member=AsyncMock(return_value=members[9001]),
        members=list(members.values()),
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.name, channel.guild = 123456789012345678, "summaries", guild
    channel.type = discord.ChannelType.text
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True
    )
    guild.channels = guild.text_channels = [channel]
    guild.get_channel = lambda cid: channel if cid == channel.id else None
    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path=directory / "bot.db"),
        store=store,
        rocket_league=None,
        is_ready=lambda: True,
        guilds=[guild],
        telegram=None,
        get_guild=lambda gid: guild if gid == 100 else None,
    )
    cog = SummaryCog(bot)
    bot.get_cog = lambda name: cog if name == "SummaryCog" else None
    bot.tree = SimpleNamespace(walk_commands=lambda: cog.__cog_app_commands__)
    server = WebServer(bot, WebSettings("https://slopbot.test"))
    bot.web = server
    await server.identities.initialize()
    token = await server.identities.login("test-subject", "Krezzle", "fixture@example.test")
    user = await server.identities.user(token)
    await server.identities.link_discord(user["id"], "9001")
    now = int(time.time())
    for uid, member in members.items():
        for gid in [100, -user["id"]] if uid == 9001 else [100]:
            owner = user["id"] if gid < 0 else uid
            await store.save_rocket_league_account(
                guild_id=gid,
                discord_user_id=owner,
                player_id=f"Epic|{uid}|0",
                player_name=member.name,
            )
            for day in range(9, 0, -1):
                await store.record_rocket_league_activity_batch(
                    guild_id=gid,
                    discord_user_id=owner,
                    player_id=f"Epic|{uid}|0",
                    player_name=member.name,
                    activities=[(11, 10 - day, 10 - day, 1000 + (10 - day) * 10, "Diamond I")],
                    threshold=3,
                    observed_at=now - day * 86400,
                )

    async def control(request):
        data = await request.json()
        members[9001].guild_permissions.manage_guild = data.get("admin", True)
        if data.get("membership") is False:
            guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "left")
        else:
            guild.fetch_member.side_effect = None
        server.rates.clear()
        return web.json_response({"ok": True})

    server.app.router.add_post("/__test/control", control)
    runner = web.AppRunner(server.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print(json.dumps(dict(port=port, token=token)), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
