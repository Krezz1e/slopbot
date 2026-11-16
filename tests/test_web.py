from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands

from slopbot.bot import AlertsCog
from slopbot.database import SubscriptionStore
from slopbot.engagement import AddKeywordModal, EngagementCog, KeywordConfigView
from slopbot.info import InfoCog
from slopbot.itemshop import ItemShopCog
from slopbot.models import DeliveryMethod, NotificationDowntime
from slopbot.rocketleague import RocketLeagueCog, StatusCog
from slopbot.summary_alerts import SummaryCog
from slopbot.tournaments import TournamentAlertsCog
from slopbot.ui import EmbedPagesView
from slopbot.web.auth import WebIdentityStore, digest
from slopbot.web.bridge import (
    WebFlow,
    WebInteraction,
    command_schema,
    command_values,
    invoke_command,
    invoke_component,
    require_command_permissions,
)
from slopbot.web.server import ASSET_VERSION_TOKEN, COOKIE, STATE_COOKIE, WebServer, WebSettings

COGS = (
    SummaryCog,
    AlertsCog,
    EngagementCog,
    RocketLeagueCog,
    StatusCog,
    ItemShopCog,
    TournamentAlertsCog,
    InfoCog,
)


def test_web_settings_require_both_vapid_keys(monkeypatch):
    monkeypatch.setenv("SLOPBOT_WEB_ORIGIN", "https://slopbot.example")
    monkeypatch.setenv("SLOPBOT_VAPID_PRIVATE_KEY", "/private/key.pem")
    monkeypatch.delenv("SLOPBOT_VAPID_PUBLIC_KEY", raising=False)
    with pytest.raises(ValueError, match="Both SLOPBOT_VAPID"):
        WebSettings.from_environment()

    monkeypatch.setenv("SLOPBOT_VAPID_PUBLIC_KEY", "public")
    settings = WebSettings.from_environment()
    assert settings is not None
    assert settings.vapid_private_key == "/private/key.pem"
    assert settings.vapid_subject == "https://slopbot.example"


def all_commands():
    for cog in COGS:
        for command in cog.__cog_app_commands__:
            if isinstance(command, app_commands.Group):
                yield from (
                    c for c in command.walk_commands() if isinstance(c, app_commands.Command)
                )
            else:
                yield command


@pytest.mark.parametrize("command", list(all_commands()), ids=lambda c: c.qualified_name)
def test_every_feature_has_a_supported_web_form(command):
    schema = command_schema(command)
    assert schema["category"] != "Other"
    assert {p["name"] for p in schema["parameters"]} == {
        p.name for p in command.parameters if p.name != "visibility"
    }


async def test_google_subject_identity_session_hashing_and_replay(tmp_path):
    store = WebIdentityStore(tmp_path / "web.db")
    await store.initialize()
    token = await store.login("google-a", "Alice", "same@example.com")
    second = await store.login("google-b", "Bob", "same@example.com")
    assert (await store.user(token))["id"] != (await store.user(second))["id"]
    assert token not in (tmp_path / "web.db").read_bytes().decode(errors="ignore")
    state, verifier = await store.begin_oauth("google", token)
    assert await store.consume_oauth(state, "google", token) == verifier
    assert await store.consume_oauth(state, "google", token) is None
    state, _ = await store.begin_oauth("discord", token)
    assert await store.consume_oauth(state, "discord", second) is None
    await store.logout(token)
    assert await store.user(token) is None


async def test_discord_link_is_unique_and_never_merges_users(tmp_path):
    store = WebIdentityStore(tmp_path / "web.db")
    await store.initialize()
    a = await store.user(await store.login("a", "A", "a@example.com"))
    b = await store.user(await store.login("b", "B", "b@example.com"))
    await store.link_discord(a["id"], "123")
    with pytest.raises(ValueError, match="already linked"):
        await store.link_discord(b["id"], "123")
    with pytest.raises(ValueError, match="Unlink"):
        await store.link_discord(a["id"], "456")
    await store.unlink_discord(a["id"])
    await store.link_discord(b["id"], "123")


async def test_linked_discord_theme_preferences_can_be_read_by_commands(tmp_path):
    store = WebIdentityStore(tmp_path / "web.db")
    await store.initialize()
    user = await store.user(await store.login("theme-user", "Theme", "theme@example.com"))
    await store.link_discord(user["id"], "123")
    await store.save_preferences(
        user["id"], {"theme_preset": "xkcd", "theme_mode": "dark", "days": 14}
    )

    assert await store.theme_preferences_for_discord("123") == {
        "theme_preset": "xkcd",
        "theme_mode": "dark",
    }
    assert await store.theme_preferences_for_discord("456") == {}


def interaction(command, *, admin=True):
    guild = MagicMock()
    guild.id = 100
    member = MagicMock(spec=discord.Member)
    member.id = 200
    member.guild = guild
    member.guild_permissions = discord.Permissions(manage_guild=admin)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 300
    channel.type = discord.ChannelType.text
    channel.permissions_for.return_value = discord.Permissions(view_channel=True)
    guild.get_channel.return_value = channel
    guild.channels = [channel]
    guild.members = [member]
    guild.fetch_member = AsyncMock(return_value=member)
    flow = WebFlow(command, 100, 300, "session")
    return WebInteraction(MagicMock(), member, channel, flow)


async def test_command_checks_and_range_validation_apply_on_web():
    i = interaction(RocketLeagueCog.match_history)
    with pytest.raises(ValueError, match="at most"):
        await command_values(i.command, {"days": 31}, i)
    with pytest.raises(ValueError, match="number"):
        await command_values(i.command, {"days": True}, i)
    with pytest.raises(ValueError, match="Unknown"):
        await command_values(i.command, {"surprise": "field"}, i)
    values = await command_values(i.command, {}, i)
    assert values["visibility"].value == "private"
    assert values["days"] == 1
    denied = interaction(StatusCog.summarize_server, admin=False)
    with pytest.raises(PermissionError):
        require_command_permissions(denied.command, denied.user)
    with pytest.raises(PermissionError):
        await invoke_command(denied, {})


async def test_shared_info_callback_renders_in_browser():
    cog = InfoCog()
    command = cog.info
    command.binding = cog
    i = interaction(command)
    await invoke_command(i, {"topic": "overview"})
    data = i.flow.render(i)
    assert data["messages"][0]["embeds"][0]["title"] == "Slopbot info — Overview"


async def test_shared_notifications_command_configures_downtime_for_web_and_discord():
    store = SimpleNamespace(
        get_delivery_method=AsyncMock(return_value=DeliveryMethod.DISCORD),
        get_notification_downtime=AsyncMock(return_value=NotificationDowntime()),
        set_notification_downtime=AsyncMock(),
        get_telegram_chat_id=AsyncMock(return_value=None),
    )
    cog = AlertsCog(SimpleNamespace(store=store, telegram=None, web=None))
    command = cog.notifications
    command.binding = cog
    i = interaction(command)

    await invoke_command(
        i,
        {
            "downtime_enabled": True,
            "downtime_start": "09:00",
            "downtime_end": "17:00",
            "downtime_timezone": "America/Los_Angeles",
        },
    )

    store.set_notification_downtime.assert_awaited_once_with(
        guild_id=100,
        user_id=200,
        downtime=NotificationDowntime(True, 9 * 60, 17 * 60, "America/Los_Angeles"),
    )
    assert (
        "enabled for 09:00-17:00 America/Los_Angeles" in i.flow.render(i)["messages"][0]["content"]
    )

    store.set_notification_downtime.reset_mock()
    invalid = interaction(command)
    await invoke_command(
        invalid,
        {"downtime_enabled": True, "downtime_start": "10:00", "downtime_end": "10:00"},
    )
    store.set_notification_downtime.assert_not_awaited()
    assert "must be different" in invalid.flow.render(invalid)["messages"][0]["content"]


async def test_shared_pagination_and_stale_form_protection():
    i = interaction(InfoCog.info)
    view = EmbedPagesView(
        owner_id=200, pages=[discord.Embed(title="First"), discord.Embed(title="Second")]
    )
    i.flow.view = view
    await invoke_component(i, {"revision": 0, "index": 2})
    assert i.flow.render(i)["messages"][0]["embeds"][0]["title"] == "Second"
    with pytest.raises(ValueError, match="changed"):
        await invoke_component(i, {"revision": 0, "index": 0})


async def test_select_values_reach_existing_callback_and_reject_forgery():
    i = interaction(InfoCog.info)
    item = discord.ui.Select(options=[discord.SelectOption(label="One", value="1")])
    captured = []

    async def callback(_interaction):
        captured.extend(item.values)

    item.callback = callback
    i.flow.view = discord.ui.View()
    i.flow.view.add_item(item)
    with pytest.raises(ValueError, match="listed"):
        await invoke_component(i, {"revision": 0, "index": 0, "values": ["forged"]})
    await invoke_component(i, {"revision": 0, "index": 0, "values": ["1"]})
    assert captured == ["1"]


async def test_modal_input_reaches_shared_keyword_handler(tmp_path):
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    i = interaction(EngagementCog.configure_keywords)
    panel = await KeywordConfigView.create(owner=i.user, guild=i.guild, store=store)
    i.flow.modal = AddKeywordModal(panel)
    await invoke_component(i, {"revision": 0, "values": {"0": "rocket league"}})
    profile = await store.get_keyword_profile(guild_id=100, user_id=200)
    assert profile.keywords == ("rocket league",)


async def test_hidden_channel_and_changed_admin_permissions_are_rejected():
    i = interaction(StatusCog.slopbot_status_channel)
    i.channel.permissions_for.return_value = discord.Permissions.none()
    with pytest.raises(PermissionError):
        await command_values(i.command, {"channel": "300", "enabled": True}, i)
    i.flow.view = discord.ui.View()
    i.flow.view.add_item(discord.ui.Button(label="Save"))
    i.user.guild_permissions = discord.Permissions.none()
    with pytest.raises(PermissionError):
        await invoke_component(i, {"revision": 0, "index": 0})


@pytest.fixture
async def site(tmp_path):
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path=tmp_path / "bot.db"),
        store=store,
        rocket_league=None,
        is_ready=lambda: True,
        guilds=[],
        get_guild=lambda _: None,
    )
    settings = WebSettings(
        "https://bot.example.com",
        google_client_id="google-id",
        google_client_secret="google-secret",
        discord_client_id="discord-id",
        discord_client_secret="discord-secret",
        vapid_private_key="private-key",
        vapid_public_key="public-key",
        vapid_subject="mailto:admin@example.com",
    )
    server = WebServer(bot, settings)
    await server.identities.initialize()
    async with TestClient(TestServer(server.app)) as client:
        yield server, client


async def test_public_pages_auth_required_and_security_headers(site):
    server, client = site
    for path in (
        "/",
        "/privacy",
        "/terms",
        "/offline",
        "/manifest.webmanifest",
        "/service-worker.js",
        "/static/app.js",
        "/static/app.css",
        "/static/theme.css",
        "/static/refresh.css",
        "/static/comic-neue-regular.woff2",
        "/static/comic-neue-bold.woff2",
        "/static/icon-192.png",
        "/static/icon-512.png",
        "/healthz",
    ):
        response = await client.get(path)
        assert response.status == 200
        if path.endswith(".woff2"):
            assert response.headers["Content-Type"] == "font/woff2"
        expected_cache = (
            "public, max-age=300, stale-while-revalidate=86400"
            if path.startswith("/static/") or path == "/manifest.webmanifest"
            else "no-store, max-age=0, must-revalidate"
            if path == "/service-worker.js"
            else "no-store"
        )
        assert response.headers["Cache-Control"] == expected_cache
        assert response.headers["Server-Timing"].startswith("app;dur=")
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    for path in ("/api/personal", "/api/shop", "/api/tournaments", "/api/guilds", "/api/guild/100"):
        assert (await client.get(path)).status == 401
    assert (await client.get("/static/server.py")).status == 404
    assert (await client.get("/static/unapproved-font.woff2")).status == 404
    manifest = await (await client.get("/manifest.webmanifest")).json()
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    worker = await client.get("/service-worker.js")
    assert worker.headers["Service-Worker-Allowed"] == "/"
    assert worker.headers["CDN-Cache-Control"] == "no-store"
    worker_text = await worker.text()
    assert ASSET_VERSION_TOKEN not in worker_text
    assert server.asset_version in worker_text
    assert "fetch(request).then" in worker_text
    assert "/api/" in worker_text
    assert "notificationclick" in worker_text
    assert "showNotification" in worker_text
    controls = await (await client.get("/static/controls.js")).text()
    assert "pushManager.subscribe" in controls
    assert "testnotification" in controls

    index = await (await client.get("/")).text()
    assert ASSET_VERSION_TOKEN not in index
    assert f'data-asset-version="{server.asset_version}"' in index
    assert f"/static/app.js?v={server.asset_version}" in index
    versioned_asset = await client.get(f"/static/app.js?v={server.asset_version}")
    assert versioned_asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "updateViaCache:'none'" in await versioned_asset.text()
    health = await (await client.get("/healthz")).json()
    assert health["asset_version"] == server.asset_version


async def test_push_subscription_api_is_authenticated_csrf_protected_and_device_scoped(site):
    server, client = site
    token = await server.identities.login("push-user", "Push", "push@example.com")
    user = await server.identities.user(token)
    assert user is not None
    headers = {
        "Cookie": f"{COOKIE}={token}",
        "Origin": server.settings.origin,
        "X-CSRF-Token": digest(token + ":csrf"),
    }
    assert (await client.get("/api/push")).status == 401
    status = await (await client.get("/api/push", headers={"Cookie": headers["Cookie"]})).json()
    assert status == {"configured": True, "public_key": "public-key", "devices": 0}
    payload = {
        "action": "subscribe",
        "endpoint": "https://push.example/device",
        "keys": {"p256dh": "p" * 32, "auth": "a" * 16},
    }
    assert (
        await client.post("/api/push", json=payload, headers={"Cookie": headers["Cookie"]})
    ).status == 403
    assert (await client.post("/api/push", json=payload, headers=headers)).status == 200
    assert await server.identities.push_subscription_count(user["id"]) == 1
    assert (
        await client.post(
            "/api/push",
            json={"action": "unsubscribe", "endpoint": payload["endpoint"]},
            headers=headers,
        )
    ).status == 200
    assert await server.identities.push_subscription_count(user["id"]) == 0


async def test_csrf_and_personal_account_isolation(site):
    server, client = site
    token = await server.identities.login("alice", "Alice", "a@example.com")
    user = await server.identities.user(token)
    headers = {
        "Cookie": f"{COOKIE}={token}",
        "Origin": server.settings.origin,
        "X-CSRF-Token": digest(token + ":csrf"),
    }
    assert (
        await client.post("/api/personal/unlink", json={}, headers={"Cookie": headers["Cookie"]})
    ).status == 403
    assert (
        await client.post(
            "/api/personal/unlink", json={}, headers={**headers, "Origin": "https://evil.example"}
        )
    ).status == 403
    assert (await client.get("/api/personal", headers=headers)).status == 200
    await server.bot.store.save_rocket_league_account(
        guild_id=100, discord_user_id=user["id"], player_id="Epic|server|0", player_name="Server"
    )
    await server.bot.store.save_rocket_league_account(
        guild_id=-user["id"],
        discord_user_id=user["id"],
        player_id="Epic|personal|0",
        player_name="Personal",
    )
    data = await (await client.get("/api/personal", headers=headers)).json()
    assert data["account"]["player_name"] == "Personal"
    assert (
        await client.post("/api/account/delete", json={"confirm": "DELETE"}, headers=headers)
    ).status == 200
    assert (
        await server.bot.store.get_rocket_league_account(guild_id=100, discord_user_id=user["id"])
        is not None
    )
    assert (
        await server.bot.store.get_rocket_league_account(
            guild_id=-user["id"], discord_user_id=user["id"]
        )
        is None
    )
    assert await server.identities.user(token) is None


async def test_oauth_cookie_pkce_and_callback_state_checks(site):
    server, client = site
    response = await client.get("/auth/google/start", allow_redirects=False)
    assert response.status == 302
    assert "code_challenge_method=S256" in response.headers["Location"]
    cookie = response.cookies[STATE_COOKIE]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Lax"
    response = await client.get("/auth/google/callback?code=fake&state=fake")
    assert response.status == 400
    assert (await client.get("/auth/discord/start", allow_redirects=False)).status == 401


async def test_tournaments_use_shared_cache_and_regions(site):
    server, client = site
    token = await server.identities.login("alice", "Alice", "a@example.com")
    headers = {"Cookie": f"{COOKIE}={token}"}
    server.bot.rocket_league = object()
    schedule = SimpleNamespace(tournaments=())
    cog = SimpleNamespace(_get_schedule=AsyncMock(return_value=schedule))
    server.bot.get_cog = lambda _: cog
    response = await client.get("/api/tournaments?region=USC", headers=headers)
    assert response.status == 200
    assert "USC" in [r["value"] for r in (await response.json())["regions"]]
    cog._get_schedule.assert_awaited_once_with("USC")


async def test_personal_tracking_survives_without_discord_membership(tmp_path):
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    await store.save_rocket_league_account(
        guild_id=-1, discord_user_id=1, player_id="Epic|web|0", player_name="Web"
    )
    bot = SimpleNamespace(get_guild=lambda _: None)
    service = SimpleNamespace(
        activity=AsyncMock(return_value=[]), active_playlists=AsyncMock(return_value=())
    )
    cog = RocketLeagueCog(bot, store, service)
    cog._deliver_event_notifications = AsyncMock()
    await cog.track_rocket_league_activity.coro(cog)
    service.activity.assert_awaited_once_with(["Epic|web|0"])


def test_web_assets_are_packaged_and_no_untrusted_html_rendering():
    root = Path(__file__).parents[1] / "src/slopbot/web/static"
    assert all(
        (root / p).exists()
        for p in (
            "index.html",
            "offline.html",
            "manifest.webmanifest",
            "service-worker.js",
            "icon-192.png",
            "icon-512.png",
            "app.js",
            "app.css",
            "theme.css",
            "refresh.css",
            "comic-neue-regular.woff2",
            "comic-neue-bold.woff2",
        )
    )
    app = (root / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app
    assert "delete f.dataset.autoRefreshDirty" in app
    assert "Watch this item" in app
    assert "itemshop_watch" in app
    assert "itemshop_unwatch" in app
    assert "themesPage" in app
    assert "Clear blue" in app
    assert "XKCD" in app
    theme = (root / "theme.css").read_text(encoding="utf-8")
    assert 'data-theme-preset="clear-blue"' in theme
    assert 'data-theme-preset="xkcd"' in theme
    assert 'data-color-mode="dark"' in theme
    assert 'font-family: "Slopbot Comic Neue"' in theme
    dashboard = (root / "dashboard.js").read_text(encoding="utf-8")
    assert "sessionMMR(s.mmr_change)" in dashboard
    assert "Playlist breakdown and game observations" in dashboard
    assert "sessionObservationMMR" in dashboard
    assert "state.page==='sessions'?{session_scope:state.sessionScope" in dashboard
    assert "view:state.page" in dashboard
    assert "view=account" in dashboard
    assert "server_activity" in dashboard
    assert "serverActivityPage" in dashboard
    assert "Server activity" in app
    assert json.loads(json.dumps(command_schema(InfoCog.info)))["name"] == "info"


def dashboard_guild(server, guild_id=100, discord_id=9001):
    member = SimpleNamespace(
        id=discord_id,
        name=f"member-{discord_id}",
        display_name=f"Member {discord_id}",
        bot=False,
        guild_permissions=discord.Permissions.none(),
    )
    other = SimpleNamespace(
        id=9002,
        name="friend",
        display_name="Friend",
        bot=False,
        guild_permissions=discord.Permissions.none(),
    )
    members = {discord_id: member, 9002: other}
    guild = SimpleNamespace(
        id=guild_id,
        name=f"Friends {guild_id}",
        get_member=members.get,
        fetch_member=AsyncMock(return_value=member),
        members=list(members.values()),
    )
    server.bot.guilds.append(guild)
    server.bot.get_guild = lambda gid: next((g for g in server.bot.guilds if g.id == gid), None)
    return guild


async def dashboard_user(server, *, discord_id=None):
    token = await server.identities.login("dashboard-user", "Player", "player@example.com")
    user = await server.identities.user(token)
    if discord_id is not None:
        await server.identities.link_discord(user["id"], str(discord_id))
    return user, {
        "Cookie": f"{COOKIE}={token}",
        "Origin": server.settings.origin,
        "X-CSRF-Token": digest(token + ":csrf"),
    }


async def test_reports_authorization_personal_scope_content_and_preferences(site):
    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")
    initial_preferences = await server.identities.preferences(user["id"])
    assert initial_preferences["report_view"]["timezone"] == "America/New_York"
    for scope, expected in (("self", 1), ("server", 2)):
        response = await client.get(
            f"/api/reports?guild_id=100&scope={scope}&offset=0", headers=headers
        )
        assert response.status == 200, await response.text()
        data = await response.json()
        assert data["report"]["active_players"] == expected
        assert data["report"]["timezone"] == "America/New_York"
        assert data["server_settings"] == [] and not data["admin"]
        assert len(data["personal_settings"]) == 4
    data = await (
        await client.get("/api/reports?guild_id=100&sections=peak_mmr&offset=0", headers=headers)
    ).json()
    assert data["report"]["sections"] == ["peak_mmr"]
    assert list(data["report"]["highlights"]) == ["peak_mmr"]
    assert (await client.get("/api/reports?guild_id=999", headers=headers)).status == 403
    assert (
        await client.get(f"/api/reports?guild_id={-user['id'] - 1}", headers=headers)
    ).status == 403
    assert (await client.get("/api/reports?guild_id=100")).status == 401
    view = dict(
        guild_id="100",
        cadence="weekly",
        scope="server",
        sections=["peak_mmr"],
        offset=1,
        timezone="America/La_Paz",
    )
    assert (
        await client.post("/api/preferences", json={"report_view": view}, headers=headers)
    ).status == 200
    assert (await server.identities.preferences(user["id"]))["report_view"] == view
    persisted = await (await client.get("/api/reports?guild_id=100", headers=headers)).json()
    assert persisted["report"]["timezone"] == "America/La_Paz"
    assert (
        await client.post(
            "/api/preferences", json={"history_sections": ["peak_mmr"]}, headers=headers
        )
    ).status == 200


async def test_reports_settings_csrf_admin_channel_and_snowflakes(site):
    from slopbot.summaries import defaults

    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    member = guild.get_member(9001)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild, channel.name = 123456789012345678, guild, "summaries"
    guild.me = SimpleNamespace(id=999)
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True
    )
    guild.get_channel = lambda cid: channel if cid == channel.id else None
    guild.text_channels = [channel]
    config = {**defaults(), "enabled": True, "timezone": "America/La_Paz", "sections": ["peak_mmr"]}
    payload = dict(
        guild_id="100", cadence="weekly", scope="server", target="personal", config=config
    )
    response = await client.post("/api/reports/settings", json=payload, headers=headers)
    assert response.status == 200, await response.text()
    assert (await response.json())["setting"]["config"]["sections"] == ["peak_mmr"]
    assert (
        await client.post(
            "/api/reports/settings", json=payload, headers={"Cookie": headers["Cookie"]}
        )
    ).status == 403
    payload.update(target="channel", config={**config, "channel_id": str(channel.id)})
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 403
    member.guild_permissions.manage_guild = True
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 200
    data = await (await client.get("/api/reports?guild_id=100", headers=headers)).json()
    weekly = next(s for s in data["server_settings"] if s["cadence"] == "weekly")
    assert weekly["config"]["channel_id"] == str(channel.id)
    assert data["channels"][0]["id"] == str(channel.id)
    payload["scope"] = "self"
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 400
    payload["scope"] = "server"
    channel.permissions_for.return_value = discord.Permissions.none()
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 403
    payload["config"]["enabled"] = False  # disabling remains possible after permissions are lost
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 200


async def test_personal_tracking_reports_without_discord_and_invalid_inputs(site):
    from slopbot.summaries import defaults

    server, client = site
    user, headers = await dashboard_user(server)
    await seed_player(server, -user["id"], user["id"])
    response = await client.get("/api/reports?offset=0", headers=headers)
    assert response.status == 200, await response.text()
    assert (await response.json())["report"]["active_players"] == 1
    assert (await client.get("/api/reports?scope=server", headers=headers)).status == 403
    payload = dict(
        guild_id=str(-user["id"]),
        cadence="daily",
        scope="self",
        target="personal",
        config={**defaults(), "enabled": True},
    )
    assert (await client.post("/api/reports/settings", json=payload, headers=headers)).status == 400
    for query in (
        "offset=99",
        "timezone=Missing",
        "sections=secret",
        "playlists=999",
        "cadence=yearly",
    ):
        assert (await client.get("/api/reports?" + query, headers=headers)).status == 400


async def test_report_presets_dates_previews_and_snapshot_permissions(site):
    from slopbot.summaries import defaults

    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9001)
    payload = dict(
        guild_id="100",
        cadence="daily",
        scope="self",
        target="personal",
        config=defaults(),
        action="preview",
    )
    response = await client.post("/api/reports/settings", json=payload, headers=headers)
    assert response.status == 200, await response.text()
    report = (await response.json())["report"]
    assert not (await server.reports.cog.service.settings(100, 9001, "daily", "self"))["revision"]
    presets = {"Progress": ["mmr", "players"]}
    assert (
        await client.post("/api/preferences", json={"report_presets": presets}, headers=headers)
    ).status == 200
    assert (await server.identities.preferences(user["id"]))["report_presets"] == presets
    assert (
        await client.post(
            "/api/preferences", json={"report_presets": {"bad": ["secret"]}}, headers=headers
        )
    ).status == 400
    linked = await client.get(
        f"/api/reports?guild_id=100&date={report['start']}&timezone=UTC", headers=headers
    )
    assert (await linked.json())["report"]["start"] == report["start"]
    oldest = await client.get("/api/reports?guild_id=100&offset=30", headers=headers)
    assert oldest.status == 200 and len((await oldest.json())["periods"]) == 31
    payload.update(target="channel", scope="server", schedule_id="-99")
    for action in ("save", "preview", "send_test"):
        payload["action"] = action
        assert (
            await client.post("/api/reports/settings", json=payload, headers=headers)
        ).status == 403
    report.update(scope="self", owner_id="9001")

    def insert(c):
        c.execute(
            "INSERT INTO summary_deliveries "
            "(id,guild_id,owner_id,cadence,scope,period_end,revision,due_at,report) "
            "VALUES(123,100,9001,'daily','self',1,1,1,?)",
            (json.dumps(report),),
        )
        c.commit()

    await server.bot.store._run(insert)
    url = "/api/reports/snapshot?guild_id=100&delivery=123&owner="
    assert (await client.get(url + "9001", headers=headers)).status == 200
    assert (await client.get(url + "9002", headers=headers)).status == 403
    assert (await client.get(url + "0", headers=headers)).status == 403
    guild.get_member = lambda _: None
    assert (await client.get(url + "9001", headers=headers)).status == 403


async def test_session_history_settings_ownership_and_csrf(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert data["session_scope"] == "personal"
    assert data["sessions"][0]["total"] == 2
    assert data["sessions"][0]["mmr_change"] == 20
    payload = {
        "source": "100",
        "cutoff": 600,
        "enabled": True,
        "minimum": 2,
        "include_observations": False,
    }
    assert (
        await client.post("/api/sessions/settings", json=payload, headers=headers)
    ).status == 200
    invalid = {**payload, "cutoff": True}
    assert (
        await client.post("/api/sessions/settings", json=invalid, headers=headers)
    ).status == 400
    assert (
        await client.post(
            "/api/sessions/settings", json=payload, headers={"Cookie": headers["Cookie"]}
        )
    ).status == 403
    payload["source"] = "200"
    assert (
        await client.post("/api/sessions/settings", json=payload, headers=headers)
    ).status == 403
    data = await (await client.get("/api/dashboard?session_scope=server", headers=headers)).json()
    assert data["session_scope"] == "server"
    assert data["session_server_settings"]["cutoff"] == 1800
    assert data["session_settings"]["cutoff"] == 600
    assert data["session_settings"]["include_observations"] == 0
    await seed_player(server, 100, 9002, player="Epic|other|0")
    response = await client.get(
        "/api/dashboard?player=Epic%7Cother%7C0&session_scope=personal", headers=headers
    )
    assert response.status == 403


async def test_server_session_overview_and_member_history_are_member_scoped(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")

    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert {row["target_key"] for row in data["session_overview"]} == {
        "100:9001",
        "100:9002",
    }
    assert all(row["active"] is False for row in data["session_overview"])

    response = await client.get("/api/dashboard?session_player=100%3A9002", headers=headers)
    assert response.status == 200
    friend = await response.json()
    assert friend["session_scope"] == "server"
    assert friend["session_target"]["member_name"] == "Friend"
    assert friend["session_target"]["player_name"] == "Known Player"
    assert friend["sessions"]

    denied = await client.get("/api/dashboard?session_player=100%3A9999", headers=headers)
    assert denied.status == 403


async def test_history_can_switch_to_a_tracked_member_and_show_current_streaks(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")

    def current_streaks(connection):  # type: ignore[no-untyped-def]
        connection.executemany(
            "INSERT OR REPLACE INTO rocket_league_all_time_streak_state "
            "(guild_id,discord_user_id,scope,result,streak_count) VALUES (?,?,?,?,?)",
            [(100, 9002, "main", "loss", 2), (100, 9002, "all", "win", 4)],
        )
        connection.commit()

    await server.bot.store._run(current_streaks)
    own = await (await client.get("/api/dashboard?view=history", headers=headers)).json()
    assert own["history_target"] is None
    assert own["account"]["discord_user_id"] == 9001
    assert {player["key"] for player in own["history_players"]} == {
        "100:9001",
        "100:9002",
    }

    response = await client.get(
        "/api/dashboard?view=history&history_player=100%3A9002", headers=headers
    )
    assert response.status == 200
    friend = await response.json()
    assert friend["history_target"]["member_name"] == "Friend"
    assert friend["account"]["player_id"] == "Epic|friend|0"
    assert friend["source"]["key"] == "100"
    assert {
        (streak["streak_class"], streak["result"], streak["count"])
        for streak in friend["current_streaks"]
    } == {("main", "loss", 2), ("all", "win", 4)}
    assert friend["history"] and friend["matches"] and friend["events"]

    denied = await client.get(
        "/api/dashboard?view=history&history_player=100%3A9999", headers=headers
    )
    assert denied.status == 403


async def test_friend_session_selection_does_not_replace_profile_rank_estimates(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")

    def ranks(c):
        c.execute(
            "UPDATE rocket_league_activity_samples SET tier=15,division=2,"
            "rank='Diamond III, Division III' WHERE discord_user_id=9001"
        )
        c.execute(
            "UPDATE rocket_league_activity_samples SET tier=9,division=1,mmr=500,"
            "rank='Gold III, Division II' WHERE discord_user_id=9002"
        )
        c.commit()

    await server.bot.store._run(ranks)
    selected = await (
        await client.get("/api/dashboard?session_player=100%3A9002", headers=headers)
    ).json()
    assert selected["account"]["player_id"] == "Epic|known|0"
    assert selected["rank_estimates"]["11"]["current_rank"] == "Diamond III, Division III"
    assert selected["rank_estimates"]["11"]["current_mmr"] == 1020
    assert selected["session_rank_estimates"]["11"]["current_rank"] == "Gold III, Division II"
    assert selected["session_rank_estimates"]["11"]["current_mmr"] == 500
    own = await (await client.get("/api/dashboard", headers=headers)).json()
    assert own["session_rank_estimates"] == own["rank_estimates"]


@pytest.mark.parametrize("ago,active", [(120, True), (7200, False)])
async def test_session_preview_estimates_use_visible_members_latest_ranks(site, ago, active):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9002, player="Epic|friend|0", ago=ago)
    await seed_player(server, 100, 9999, player="Epic|departed|0", ago=ago)
    await seed_player(server, 200, 9002, player="Epic|private|0", ago=ago)

    def evidence(c):
        c.execute(
            "UPDATE rocket_league_activity_samples SET tier=14,division=1,"
            "rank='Diamond II, Division II'"
        )
        c.execute(
            "INSERT INTO rocket_league_rank_boundaries "
            "(playlist,from_tier,from_division,to_tier,to_division,direction,mean_mmr,"
            "mean_results,result_sample_count,sample_count,last_observed) "
            "VALUES (11,14,1,14,2,'win',1040,2,4,4,?)",
            (int(time.time()),),
        )
        c.commit()

    await server.bot.store._run(evidence)
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert data["account"] is None  # Server previews also work without a personal profile.
    assert len(data["session_overview"]) == 1
    preview = data["session_overview"][0]
    assert preview["target_key"] == "100:9002"
    assert preview["active"] is active
    estimate = preview["rank_estimates"]["11"]
    assert estimate["current_mmr"] == 1020
    assert estimate["observed_at"] == preview["last_at"]
    assert estimate["steps"][0]["games"] == 2
    assert estimate["steps"][0]["target"] == "Diamond II, Division III"
    guild.fetch_member.side_effect = PermissionError("Removed")
    denied = await (await client.get("/api/dashboard", headers=headers)).json()
    assert denied["session_overview"] == []
    assert (await client.get("/api/dashboard")).status == 401


async def seed_player(server, guild_id, user_id, *, player="Epic|known|0", ago=7200):
    await server.bot.store.save_rocket_league_account(
        guild_id=guild_id,
        discord_user_id=user_id,
        player_id=player,
        player_name="Known Player",
    )
    now = int(time.time()) - ago
    for i in range(3):
        await server.bot.store.record_rocket_league_activity_batch(
            guild_id=guild_id,
            discord_user_id=user_id,
            player_id=player,
            player_name="Known Player",
            activities=[(11, 100 + i, 2 + i, 1000 + 10 * i, "Diamond II")],
            threshold=3,
            observed_at=now + i * 30,
        )


async def test_dashboard_views_only_load_data_used_by_each_page(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")

    account = await (await client.get("/api/dashboard?view=account", headers=headers)).json()
    assert account["profiles"]
    assert account["account"] is None
    assert "histories" not in account
    assert account["session_overview"] == []

    history = await (await client.get("/api/dashboard?view=history", headers=headers)).json()
    assert history["history"] and history["matches"] and history["events"]
    assert "peaks" not in history
    assert "sessions" not in history
    assert history["session_overview"] == []
    assert history["history_players"]
    assert history["history_target"] is None

    overview = await (await client.get("/api/dashboard?view=overview", headers=headers)).json()
    assert overview["history"] and overview["peaks"] and overview["samples"]
    assert len(overview["sessions"]) <= 1
    assert overview["session_players"] == []
    assert overview["session_overview"] == []

    sessions = await (await client.get("/api/dashboard?view=sessions", headers=headers)).json()
    assert sessions["sessions"]
    assert sessions["session_players"]
    assert sessions["session_overview"]
    assert "histories" not in sessions
    assert "matches" not in sessions

    assert (await client.get("/api/dashboard?view=everything", headers=headers)).status == 400


async def test_guild_memberships_are_checked_concurrently(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    first = dashboard_guild(server, 100)
    second = dashboard_guild(server, 200)
    started = 0
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(member_id):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return SimpleNamespace(
            id=member_id,
            guild_permissions=discord.Permissions.none(),
        )

    first.fetch_member.side_effect = fetch
    second.fetch_member.side_effect = fetch
    request = asyncio.create_task(client.get("/api/guilds", headers=headers))
    await asyncio.wait_for(both_started.wait(), 1)
    release.set()
    response = await request
    assert response.status == 200
    assert len((await response.json())["guilds"]) == 2


async def test_slow_membership_read_uses_gateway_cache_but_write_waits(site, monkeypatch):
    server, _ = site
    user, _ = await dashboard_user(server, discord_id=9001)
    user["discord_id"] = "9001"
    guild = dashboard_guild(server)
    release = asyncio.Event()

    async def fetch(_member_id):
        await release.wait()
        return guild.get_member(9001)

    guild.fetch_member.side_effect = fetch
    monkeypatch.setattr("slopbot.web.server.MEMBERSHIP_READ_TIMEOUT_SECONDS", 0.01)
    _, cached_member, _ = await server.context(user, 100, allow_cached=True)
    assert cached_member is guild.get_member(9001)

    write_check = asyncio.create_task(server.context(user, 100))
    await asyncio.sleep(0)
    assert not write_check.done()
    release.set()
    _, fresh_member, _ = await write_check
    assert fresh_member is guild.get_member(9001)


async def test_sessions_without_own_profile_validate_filters_and_membership(site):
    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9002, player="Epic|friend|0")
    await server.identities.save_preferences(user["id"], {"days": 14})
    overview = await (await client.get("/api/dashboard", headers=headers)).json()
    assert overview["account"] is None
    assert overview["days"] == 14
    assert overview["session_overview"][0]["target_key"] == "100:9002"
    url = "/api/dashboard?session_player=100%3A9002"
    data = await (await client.get(url, headers=headers)).json()
    assert data["days"] == 14
    assert data["sessions"]
    assert data["session_own"] is False
    for query in ("days=999", "days=-1", "playlist=999", "days=bad"):
        assert (await client.get(url + "&" + query, headers=headers)).status == 400
    assert (await client.get(url + "&days=1", headers=headers)).status == 200
    guild.fetch_member.side_effect = PermissionError("Removed")
    assert (await client.get(url, headers=headers)).status == 403


async def test_activity_feed_requires_membership_without_loading_live_stats(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")
    await seed_player(server, 100, 9999, player="Epic|departed|0")
    server.dashboard.board = AsyncMock(side_effect=AssertionError("Unrelated leaderboard read"))
    server.bot.rocket_league = SimpleNamespace(
        skills=AsyncMock(side_effect=AssertionError("Unrelated live stats read"))
    )
    url = "/api/activity?guild_id=100"
    response = await client.get(url, headers=headers)
    assert response.status == 200
    data = await response.json()
    assert {e["player_id"] for e in data["events"]} == {"Epic|known|0", "Epic|friend|0"}
    assert data["guild"] == guild.name
    assert server.dashboard.pending == {}
    server.bot.rocket_league.skills.assert_not_called()
    server.dashboard.board.assert_not_called()
    assert (await client.get(url)).status == 401
    assert (await client.get(url + "&days=999", headers=headers)).status == 400
    guild.fetch_member.side_effect = PermissionError("Removed")
    assert (await client.get(url, headers=headers)).status == 403


async def test_discord_first_reuses_known_profile_even_when_rl_offline(site):
    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    response = await client.get("/api/dashboard", headers=headers)
    assert response.status == 200
    data = await response.json()
    assert data["account"]["player_id"] == "Epic|known|0"
    assert data["source"]["key"] == "100"
    assert len(data["history"]) == 3
    assert len(data["guilds"]) == 1
    linked = await client.post(
        "/api/personal/link", json={"platform": "epic", "account_id": "known"}, headers=headers
    )
    assert linked.status == 200
    assert (await linked.json())["reused"] is True
    assert (
        await server.bot.store.get_rocket_league_account(
            guild_id=-user["id"], discord_user_id=user["id"]
        )
        is None
    )


async def test_rl_first_then_discord_groups_profiles_and_preserves_both_histories(site):
    server, client = site
    user, headers = await dashboard_user(server)
    await seed_player(server, -user["id"], user["id"], ago=3600)
    initial = await (await client.get("/api/dashboard", headers=headers)).json()
    assert initial["source"]["personal"] is True
    dashboard_guild(server)
    await seed_player(server, 100, 9001, ago=7200)
    await server.identities.link_discord(user["id"], "9001")
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert len(data["profiles"]) == 1
    assert len(data["profiles"][0]["sources"]) == 2
    assert data["source"]["key"] == "100"
    personal = await (
        await client.get(f"/api/dashboard?source=-{user['id']}", headers=headers)
    ).json()
    assert personal["source"]["personal"] is True
    assert len(personal["history"]) == 3
    await client.post("/api/discord/unlink", json={}, headers=headers)
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert data["source"]["personal"] is True
    assert len(data["profiles"][0]["sources"]) == 1


async def test_different_profiles_are_selectable_not_merged(site):
    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, -user["id"], user["id"], player="Epic|other|0")
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert len(data["profiles"]) == 2
    selected = await client.get("/api/dashboard?player=Epic%7Cother%7C0", headers=headers)
    assert (await selected.json())["account"]["player_id"] == "Epic|other|0"
    assert (
        await client.get("/api/dashboard?player=Epic%7Cinaccessible%7C0", headers=headers)
    ).status == 403


async def test_dashboard_filters_events_to_selected_player_and_checks_fresh_membership(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|someoneelse|0")
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert data["events"]
    assert all(e["player_id"] == "Epic|known|0" for e in data["events"])
    assert {e["player_id"] for e in data["server_activity"]["events"]} == {
        "Epic|known|0",
        "Epic|someoneelse|0",
    }
    guild.fetch_member.side_effect = PermissionError("Left the server")
    assert (await client.get("/api/dashboard?source=100", headers=headers)).status == 200
    no_access = await (await client.get("/api/dashboard", headers=headers)).json()
    assert no_access["account"] is None
    assert no_access["guilds"] == []
    assert (await client.get("/api/leaderboards?guild_id=100", headers=headers)).status == 403


async def test_native_leaderboard_does_not_require_discord_command_channel(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    # An empty channel allowlist blocks Discord commands, but not native web reads.
    await server.bot.store.allow_command_channel(guild_id=100, command_name="rank", channel_id=300)
    await server.bot.store.disallow_command_channel(
        guild_id=100, command_name="rank", channel_id=300
    )
    response = await client.get("/api/leaderboards?guild_id=100&playlist=11", headers=headers)
    assert response.status == 200
    data = await response.json()
    assert data["rows"][0]["skill"]["mmr"] == 1020
    assert data["rows"][0]["you"] is True
    assert data["events"]


async def test_homepage_discovers_servers_without_personal_rl_link(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    response = await client.get("/api/dashboard", headers=headers)
    data = await response.json()
    assert data["account"] is None
    assert data["guilds"][0]["name"] == "Friends 100"
    cards = [
        {"kind": "ranks", "guild_id": "100", "playlist": 11, "days": 7},
        {"kind": "server_activity", "guild_id": "100", "playlist": 11, "days": 7},
    ]
    patch = {
        "widgets": ["trend", "ranks"],
        "server_widgets": cards,
        "layout": ["server-0", "trend", "ranks"],
        "card_sizes": {"trend": 1},
    }
    assert (await client.post("/api/preferences", json=patch, headers=headers)).status == 200
    data = await (await client.get("/api/dashboard", headers=headers)).json()
    assert data["preferences"] == {
        **patch,
        "timezone": "America/New_York",
        "report_view": {"timezone": "America/New_York"},
    }
    cards[0]["guild_id"] = "999"
    assert (
        await client.post("/api/preferences", json={"server_widgets": cards}, headers=headers)
    ).status == 403


async def test_theme_preferences_are_saved_and_returned_on_boot(site):
    server, client = site
    _, headers = await dashboard_user(server)
    patch = {
        "theme_preset": "xkcd",
        "theme_mode": "dark",
        "theme_radius": "soft",
        "theme_density": "compact",
    }
    assert (await client.post("/api/preferences", json=patch, headers=headers)).status == 200
    data = await (await client.get("/api/me", headers=headers)).json()
    assert data["preferences"] == {
        **patch,
        "timezone": "America/New_York",
        "report_view": {"timezone": "America/New_York"},
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"widgets": ["ranks", "ranks"]},
        {"widgets": ["unknown"]},
        {"days": 999},
        {"card_sizes": {"trend": 3}},
        {"layout": ["server-999"]},
        {"playlist": True},
        {"server_widgets": [{"kind": "ranks"}]},
        {"compare": "yes"},
        {"theme_preset": "neon-space"},
        {"theme_mode": []},
        {"theme_radius": "roundest"},
        {"theme_density": True},
    ],
)
async def test_invalid_dashboard_preferences_rejected(site, patch):
    server, client = site
    _, headers = await dashboard_user(server)
    assert (await client.post("/api/preferences", json=patch, headers=headers)).status == 400


async def test_web_result_policy_does_not_change_discord_policy(site):
    from slopbot.visibility import command_channel_is_allowed

    server, _ = site
    await server.bot.store.disallow_command_channel(
        guild_id=100, command_name="rank", channel_id=300
    )
    normal = SimpleNamespace(guild_id=100, channel_id=300, channel=None)
    website = SimpleNamespace(guild_id=100, channel_id=300, channel=None, slopbot_web=True)
    assert not await command_channel_is_allowed(
        store=server.bot.store, interaction=normal, command_name="rank"
    )
    assert await command_channel_is_allowed(
        store=server.bot.store, interaction=website, command_name="rank"
    )
    assert WebInteraction.slopbot_web is True


async def test_dashboard_returns_saved_stats_while_live_request_is_blocked(site):
    import asyncio

    from slopbot.rocketleague import RocketLeaguePlayerDetails, RocketLeagueSeasonRewards

    server, client = site
    user, headers = await dashboard_user(server)
    await seed_player(server, -user["id"], user["id"])
    gate = asyncio.Event()

    async def slow(player):
        await gate.wait()
        return RocketLeaguePlayerDetails((), RocketLeagueSeasonRewards(5, 6), None)

    server.bot.rocket_league = SimpleNamespace(player_details=AsyncMock(side_effect=slow))
    try:
        response = await asyncio.wait_for(client.get("/api/dashboard", headers=headers), 1)
        data = await response.json()
        assert data["samples"][0]["mmr"] == 1020
        assert data["details"] is None
        assert data["live_available"] is True
        await client.get("/api/dashboard", headers=headers)
        assert server.bot.rocket_league.player_details.await_count == 1
        gate.set()
        await asyncio.gather(*server.dashboard.pending.values())
        refreshed = await (await client.get("/api/dashboard", headers=headers)).json()
        assert refreshed["details"]["rewards"]["level"] == 5
        assert refreshed["service_error"] is None
        assert server.bot.rocket_league.player_details.await_count == 1
    finally:
        await server.dashboard.close()


async def test_leaderboard_refresh_rechecks_membership_after_live_results_arrive(site):
    import asyncio

    from slopbot.rocketleague import RocketLeagueSkill

    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    await seed_player(server, 100, 9001)
    gate = asyncio.Event()

    async def slow(**kwargs):
        await gate.wait()
        return [RocketLeagueSkill("Epic|known|0", 1100, 1, 1, "Live rank", playlist=11)]

    server.bot.rocket_league = SimpleNamespace(skills=AsyncMock(side_effect=slow))
    url = "/api/leaderboards?guild_id=100&playlist=11"
    try:
        saved = await (await client.get(url, headers=headers)).json()
        assert saved["live"] is False
        assert saved["live_available"] is True
        assert saved["rows"][0]["skill"]["mmr"] == 1020
        gate.set()
        await asyncio.gather(*server.dashboard.pending.values())
        fresh = await (await client.get(url, headers=headers)).json()
        assert fresh["live"] is True
        assert fresh["rows"][0]["skill"]["mmr"] == 1100
        guild.fetch_member.side_effect = PermissionError("Left the server")
        assert (await client.get(url, headers=headers)).status == 403
    finally:
        await server.dashboard.close()


async def test_settings_read_and_policy_writes_require_current_admin(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    member = await guild.fetch_member(9001)
    channel = SimpleNamespace(
        id=300,
        type=SimpleNamespace(value=0),
        permissions_for=lambda m: SimpleNamespace(view_channel=True),
    )
    guild.get_channel = lambda cid: channel if cid == 300 else None
    patch = {"command": "rank", "visibility": "private", "restricted": True, "channels": ["300"]}
    url = "/api/settings/100/policy"
    assert (await client.post(url, json=patch, headers=headers)).status == 403
    member.guild_permissions = discord.Permissions(manage_guild=True)
    assert (await client.post(url, json=patch, headers=headers)).status == 200
    data = await (await client.get("/api/settings/100", headers=headers)).json()
    assert data["policies"][0]["channel_ids"] == ["300"]
    assert data["policies"][0]["default_visibility"] == "private"
    assert data["event_labels"]["leaderboard_movement"] == "Rank leaderboard movements"
    assert (
        data["event_labels"]["streak_leaderboard_competitive"]
        == "Competitive streak leaderboard entries"
    )
    assert (
        data["event_labels"]["streak_leaderboard_all_ranked"]
        == "All-ranked streak leaderboard entries"
    )
    patch["channels"] = ["999"]
    assert (await client.post(url, json=patch, headers=headers)).status == 403
    assert (
        await server.bot.store.get_command_policy(guild_id=100, command_name="rank")
    ).channel_ids == (300,)
    member.guild_permissions = discord.Permissions.none()
    data = await (await client.get("/api/settings/100", headers=headers)).json()
    assert "policies" not in data
    assert (await client.post(url, json=patch, headers=headers)).status == 403


async def test_personal_settings_are_scoped_and_validate_channels(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    guild = dashboard_guild(server)
    channel = SimpleNamespace(
        id=300,
        type=SimpleNamespace(value=2),
        permissions_for=lambda m: SimpleNamespace(view_channel=True),
    )
    guild.get_channel = lambda cid: channel if cid == 300 else None
    url = "/api/settings/100/personal"
    assert (
        await client.post(url, json={"action": "keyword_add", "keyword": "ranked"}, headers=headers)
    ).status == 200
    assert (
        await client.post(
            url,
            json={
                "action": "keyword_options",
                "enabled": False,
                "mode": "offline_only",
                "channels": [],
            },
            headers=headers,
        )
    ).status == 200
    voice = {"action": "voice", "channel": "300", "threshold": 3, "reach": True, "drop": False}
    assert (await client.post(url, json=voice, headers=headers)).status == 200
    data = await (await client.get("/api/settings/100", headers=headers)).json()
    assert data["keywords"]["keywords"] == ["ranked"]
    assert data["keywords"]["enabled"] is False
    assert data["voice"][0]["channel_id"] == "300"
    assert data["downtime"] == {
        "enabled": False,
        "start_minute": 540,
        "end_minute": 1020,
        "timezone": "America/New_York",
    }
    assert await server.bot.store.list_for_user(guild_id=100, user_id=9002) == []
    voice["channel"] = "999"
    assert (await client.post(url, json=voice, headers=headers)).status == 403
    voice["channel"], voice["threshold"] = "300", True
    assert (await client.post(url, json=voice, headers=headers)).status == 400
    guild.fetch_member.side_effect = PermissionError("Removed")
    assert (await client.get("/api/settings/100", headers=headers)).status == 403


async def test_item_shop_watch_web_actions_are_member_scoped(site):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    url = "/api/settings/100/personal"
    payload = {"action": "itemshop_watch", "query": "crimson octane", "max_price": 800}
    assert (await client.post(url, json=payload, headers=headers)).status == 400
    server.bot.rocket_league = object()
    assert (await client.post(url, json=payload, headers=headers)).status == 200
    settings = await (await client.get("/api/settings/100", headers=headers)).json()
    assert settings["item_shop_watch_limit"] == 25
    assert settings["watches"][0]["query"] == "crimson octane"
    assert settings["watches"][0]["max_price"] == 800
    watch_id = settings["watches"][0]["id"]
    assert (
        await client.post(
            url,
            json={"action": "itemshop_unwatch", "id": watch_id},
            headers=headers,
        )
    ).status == 200
    assert (await server.bot.store.list_item_shop_watches(guild_id=100, user_id=9001)) == []
    assert (
        await client.post(
            url,
            json={"action": "itemshop_unwatch", "id": watch_id},
            headers=headers,
        )
    ).status == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "itemshop_watch", "query": 123, "max_price": None},
        {"action": "itemshop_watch", "query": "octane", "max_price": True},
        {"action": "itemshop_unwatch", "id": True},
    ],
)
async def test_item_shop_watch_web_actions_validate_types(site, payload):
    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    server.bot.rocket_league = object()
    assert (
        await client.post("/api/settings/100/personal", json=payload, headers=headers)
    ).status == 400


async def test_status_reuses_background_snapshot_without_service_call(site):
    from slopbot.rocketleague import SlopbotStatusSnapshot

    server, client = site
    _, headers = await dashboard_user(server)
    snapshot = SlopbotStatusSnapshot(("Operational",), True, int(time.time()))
    server.bot.get_cog = lambda name: SimpleNamespace(_latest=snapshot)
    assert (await client.get("/api/status")).status == 401
    data = await (await client.get("/api/status", headers=headers)).json()
    assert data["healthy"] is True
    assert data["stale"] is False
    assert data["lines"] == ["Operational"]


async def test_report_range_and_player_selection_are_view_only_and_authorized(site):
    from datetime import date, timedelta

    server, client = site
    _, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    await seed_player(server, 100, 9001)
    await seed_player(server, 100, 9002, player="Epic|friend|0")
    base = "/api/reports?guild_id=100&scope=server&timezone=UTC"
    initial = await (await client.get(base, headers=headers)).json()
    before = await server.reports.cog.service.list_settings(100, 9001)
    limits = initial["date_limits"]
    query = base + "&range_start=" + limits["min"] + "&range_end=" + limits["max"]
    all_players = await (await client.get(query, headers=headers)).json()
    assert all_players["report"]["totals"]["games"] == 4
    selected = await (
        await client.get(query + "&players=Epic%7Cfriend%7C0", headers=headers)
    ).json()
    assert selected["report"]["totals"]["games"] == 2
    assert selected["report"]["tracked_players"] == 1
    assert selected["selected_players"] == ["Epic|friend|0"]
    assert len(selected["available_players"]) == 2
    assert selected["report"]["cadence"] == "custom"
    assert await server.reports.cog.service.list_settings(100, 9001) == before
    for suffix, status in [("&players=unknown", 403), ("&players=", 400)]:
        assert (await client.get(query + suffix, headers=headers)).status == status
    assert (
        await client.get(
            query.replace("scope=server", "scope=self") + "&players=Epic%7Cfriend%7C0",
            headers=headers,
        )
    ).status == 403
    for extra in [
        "&range_start=" + limits["max"],
        "&range_start=bad&range_end=bad",
        "&range_start="
        + limits["max"]
        + "&range_end="
        + (date.fromisoformat(limits["max"]) + timedelta(days=1)).isoformat(),
    ]:
        assert (await client.get(base + extra, headers=headers)).status == 400
    assert (await client.get(query)).status == 401


async def test_account_timezone_persists_and_updates_report_default(site):
    server, client = site
    user, headers = await dashboard_user(server, discord_id=9001)
    dashboard_guild(server)
    prefs = await server.identities.preferences(user["id"])
    assert prefs["timezone"] == "America/New_York"
    assert (
        await client.post(
            "/api/preferences", json={"timezone": "America/Los_Angeles"}, headers=headers
        )
    ).status == 200
    prefs = await server.identities.preferences(user["id"])
    assert prefs["report_view"]["timezone"] == prefs["timezone"] == "America/Los_Angeles"
    report = await (await client.get("/api/reports?guild_id=100", headers=headers)).json()
    assert report["report"]["timezone"] == "America/Los_Angeles"
    assert (
        await client.post(
            "/api/preferences", json={"report_view": {"timezone": "Europe/London"}}, headers=headers
        )
    ).status == 200
    assert (await server.identities.preferences(user["id"]))["timezone"] == "Europe/London"
    for zone in ("Not/AZone", "", 123):
        assert (
            await client.post("/api/preferences", json={"timezone": zone}, headers=headers)
        ).status == 400
