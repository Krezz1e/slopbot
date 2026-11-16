import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from slopbot.database import SubscriptionStore
from slopbot.rocketleague import RocketLeagueCog
from slopbot.sessions import Sessions, initialize, summary


@pytest.fixture
async def tracking(tmp_path):
    store = SubscriptionStore(tmp_path / "sessions.db")
    await store.initialize()
    await store.save_rocket_league_account(
        guild_id=1, discord_user_id=2, player_id="p", player_name="Player"
    )
    return store, Sessions(store)


async def observe(store, at, games, *, playlist=11, streak=None):
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=2,
        player_id="p",
        player_name="Player",
        activities=[
            (playlist, games, games if streak is None else streak, 1000 + games * 10, "Rank")
        ],
        threshold=3,
        observed_at=at,
    )


def test_existing_session_preferences_add_observation_setting(tmp_path):
    path = tmp_path / "legacy-sessions.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE play_session_preferences ("
            "guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, "
            "cutoff INTEGER NOT NULL DEFAULT 1800, enabled INTEGER NOT NULL DEFAULT 0, "
            "channel_id INTEGER, minimum INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY (guild_id, owner_id))"
        )
        connection.execute("INSERT INTO play_session_preferences VALUES(1,2,600,1,NULL,3)")
        initialize(connection)
        row = connection.execute(
            "SELECT cutoff,enabled,minimum,include_observations "
            "FROM play_session_preferences WHERE guild_id=1 AND owner_id=2"
        ).fetchone()
    assert row == (600, 1, 3, 1)


async def test_boundary_playlists_and_independent_cutoffs(tracking):
    store, service = tracking
    await service.save(1, 0, cutoff=1800, enabled=False)
    await service.save(1, 2, cutoff=300, enabled=False)
    await observe(store, 1, 0)
    await observe(store, 1, 0, playlist=27)
    assert await service.history(1, "p", 0) == []
    await observe(store, 100, 1)
    await observe(store, 1900, 1, playlist=27)  # exactly 30 min, another playlist
    server = await service.history(1, "p", 0)
    assert len(server) == 1 and server[0]["total"] == 2
    assert len(server[0]["playlists"]) == 2
    assert len(await service.history(1, "p", 2)) == 2
    await service.finish(3700)
    assert not (await service.history(1, "p", 0))[0]["closed"]
    await observe(store, 3701, 2)
    assert len(await service.history(1, "p", 0)) == 2
    # Other-playlist activity kept the competitive streak alive until this boundary.
    events = await store.list_rocket_league_events(guild_id=1, since=0)
    assert any(e.event_type == "session_streak_completed" for e in events)


async def test_settings_snapshot_optout_restart_and_retention(tracking):
    store, service = tracking
    await service.save(1, 2, cutoff=300, enabled=True)
    await observe(store, 1, 0)
    await observe(store, 100, 1)
    await service.save(1, 2, cutoff=1800, enabled=True)
    await observe(store, 401, 2)
    rows = await service.history(1, "p", 2)
    assert [s["cutoff"] for s in rows] == [1800, 300]
    pending = await service.finish(401)
    assert len(pending) == 1
    await service.delivered(pending[0]["id"])
    await store.initialize()  # schema restart doesn't reset or duplicate sessions
    assert await service.finish(402) == []
    await service.save(1, 2, cutoff=1800, enabled=False)
    assert await service.finish(2300) == []
    await service.finish(401 + 30 * 86400 + 1)
    assert await service.history(1, "p", 2) == []
    assert (
        await store._run(
            lambda c: c.execute("SELECT COUNT(*) FROM play_session_games").fetchone()[0]
        )
        == 0
    )


async def test_uncertain_batch_does_not_multiply_mmr(tracking):
    store, service = tracking
    await observe(store, 1, 0)
    await observe(store, 100, 5, streak=1)
    await observe(store, 101, 5, streak=1)
    row = (await service.history(1, "p", 0))[0]
    assert row["total"] == 5 and row["unknown"] > 0
    assert row["playlists"][0]["after"] - row["playlists"][0]["before"] == 50
    assert len(row["games"]) == 2 and len(row["observations"]) == 1
    assert row["observations"][0]["change"] == 50
    assert row["observations"][0]["per_game_change"] == 10
    assert row["wins"] + row["losses"] + row["unknown"] == 5


async def test_sessions_report_total_mmr_change_while_active_and_after_close(tracking):
    store, service = tracking
    await service.save(1, 0, cutoff=300, enabled=True, channel_id=99)
    await observe(store, 1, 0)
    await observe(store, 100, 1)
    await observe(store, 101, 0, playlist=27)
    await observe(store, 200, 1, playlist=27)

    active = (await service.history(1, "p", 0))[0]
    assert active["mmr_change"] == 20
    assert [game["change"] for game in active["observations"]] == [10, 10]
    assert [game["per_game_change"] for game in active["observations"]] == [10, 10]
    assert "Total MMR change: +20 MMR" in summary(active, {11: "2v2", 27: "Hoops"})
    assert "Game observations:" in summary(active, {11: "2v2", 27: "Hoops"})
    assert "1000→1010 · +10 MMR" in summary(active, {11: "2v2", 27: "Hoops"})

    closed = await service.finish(501)
    assert len(closed) == 1
    assert closed[0]["closed"] == 1
    assert closed[0]["mmr_change"] == 20
    assert "Total MMR change: +20 MMR" in summary(closed[0], {11: "2v2", 27: "Hoops"})
    historic = (await service.history(1, "p", 0))[0]
    assert historic["closed"] == 1 and historic["mmr_change"] == 20


async def test_latest_server_sessions_prefers_current_then_most_recent(tracking):
    store, service = tracking
    await service.save(1, 0, cutoff=300, enabled=False)
    await observe(store, 1, 0)
    await service.finish(302)
    await observe(store, 1000, 2)

    current = await service.latest_server_sessions(1, now=1000)
    assert len(current) == 1
    assert current[0]["total"] == 2
    assert current[0]["closed"] == 0

    await service.finish(1401)
    recent = await service.latest_server_sessions(1, now=1401)
    assert len(recent) == 1
    assert recent[0]["total"] == 2
    assert recent[0]["closed"] == 1


async def test_discord_server_session_overview_shows_current_members(tracking):
    store, _service = tracking
    await store.save_rocket_league_account(
        guild_id=1, discord_user_id=3, player_id="friend", player_name="Friend Player"
    )
    now = int(time.time())
    for user_id, player_id, name in ((2, "p", "Player"), (3, "friend", "Friend Player")):
        for at, matches in ((now - 60, 10), (now, 11)):
            await store.record_rocket_league_activity_batch(
                guild_id=1,
                discord_user_id=user_id,
                player_id=player_id,
                player_name=name,
                activities=[(11, matches, 1, 1000, "Diamond II")],
                threshold=3,
                observed_at=at,
            )
    members = {
        2: MagicMock(spec=discord.Member, id=2, display_name="Me", bot=False),
        3: MagicMock(spec=discord.Member, id=3, display_name="Friend", bot=False),
    }
    guild = SimpleNamespace(id=1, name="Server", get_member=members.get)
    response = AsyncMock()
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=1,
        user=members[2],
        response=SimpleNamespace(send_message=response),
    )
    cog = RocketLeagueCog(SimpleNamespace(), store, None)

    await RocketLeagueCog.play_sessions.callback(cog, interaction, account=None, all_members=True)

    response.assert_awaited_once()
    embed = response.call_args.kwargs["embed"]
    assert {field.name.split(" · ")[0] for field in embed.fields} == {"Me", "Friend"}


async def test_personal_summary_delivers_once_and_respects_membership(tracking, monkeypatch):
    store, service = tracking
    await service.save(1, 2, cutoff=300, enabled=True, include_observations=False)
    await observe(store, 1, 0)
    await observe(store, 100, 1)
    guild = SimpleNamespace(get_member=lambda uid: object())
    bot = SimpleNamespace(get_guild=lambda gid: guild, telegram=None)
    send = AsyncMock(return_value=SimpleNamespace(delivered=True, retryable=False))
    monkeypatch.setattr("slopbot.rocketleague.send_to_destinations", send)
    cog = RocketLeagueCog(bot, store, None)
    await cog._deliver_session_summaries(401)
    await cog._deliver_session_summaries(402)
    send.assert_awaited_once()
    assert "Game observations:" not in send.call_args.kwargs["discord_embed"].description
    assert "Ranked Doubles (2v2):" in send.call_args.kwargs["discord_embed"].description
    await observe(store, 500, 2)
    guild.get_member = lambda uid: None
    await cog._deliver_session_summaries(801)
    send.assert_awaited_once()


async def test_minimum_and_bounded_delivery_retry(tracking, monkeypatch):
    store, service = tracking
    await service.save(1, 2, cutoff=300, enabled=True, minimum=2)
    await observe(store, 1, 0)
    await observe(store, 100, 1)
    bot = SimpleNamespace(
        get_guild=lambda gid: SimpleNamespace(get_member=lambda uid: object()), telegram=None
    )
    send = AsyncMock(return_value=SimpleNamespace(delivered=False, retryable=True))
    monkeypatch.setattr("slopbot.rocketleague.send_to_destinations", send)
    cog = RocketLeagueCog(bot, store, None)
    await cog._deliver_session_summaries(401)
    send.assert_not_awaited()
    await observe(store, 500, 3)
    for now in [801, 802, 861, 981, 1221, 1701, 4000]:
        await cog._deliver_session_summaries(now)
    assert send.await_count == 5
    assert await service.finish(5000) == []


async def test_settings_validation_and_unlink_cleanup(tracking):
    store, service = tracking
    for cutoff in [True, 299, 86401, "1800"]:
        with pytest.raises(ValueError):
            await service.save(1, 2, cutoff=cutoff, enabled=False)
    with pytest.raises(ValueError):
        await service.save(1, 0, cutoff=1800, enabled=True)
    await observe(store, 1, 0)
    await observe(store, 100, 1)
    await store.unlink_rocket_league_account(guild_id=1, discord_user_id=2)
    assert await service.history(1, "p", 2) == []
    assert await service.history(1, "p", 0) == []


async def test_server_settings_require_admin_and_shared_timer(tracking):
    store, service = tracking
    response = AsyncMock()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        permissions=SimpleNamespace(manage_guild=False),
        response=SimpleNamespace(send_message=response),
    )
    cog = RocketLeagueCog(SimpleNamespace(), store, None)
    await RocketLeagueCog.setup_sessions.callback(cog, interaction, cutoff_minutes=5)
    assert (await service.settings(1, 0))["cutoff"] == 1800
    assert "Manage Server" in response.call_args.args[0]
    await service.save(1, 0, cutoff=600, enabled=True, channel_id=99)
    assert (await store.get_rocket_league_tracking_config(guild_id=1)).inactivity_seconds == 600
    await store.configure_rocket_league_streaks(
        guild_id=1, channel_id=99, threshold=3, inactivity_seconds=900
    )
    assert (await service.settings(1, 0))["cutoff"] == 900
    assert (await service.settings(1, 2))["cutoff"] == 1800


async def test_existing_database_new_leaderboard_uses_30_minutes(tracking):
    store, service = tracking
    # Old installations retain the original column default. Explicit insertion must
    # still use the new default when a server first configures its leaderboard.
    await store.configure_rocket_league_leaderboard(guild_id=3, channel_id=99, playlists=(11,))
    assert (await service.settings(3, 0))["cutoff"] == 1800
