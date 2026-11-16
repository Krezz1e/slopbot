import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from slopbot.database import SubscriptionStore
from slopbot.summaries import (
    SECTIONS,
    Summaries,
    defaults,
    next_delivery,
    period,
    report_csv,
    report_text,
    validate,
)
from slopbot.summary_alerts import SummaryCog


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())


NOW = stamp("2026-09-07T12:00:00")


@pytest.fixture
async def summaries(tmp_path):
    store = SubscriptionStore(tmp_path / "summaries.db")
    await store.initialize()
    return Summaries(store)


async def match(
    service,
    at,
    *,
    pid="p",
    uid=2,
    playlist=11,
    result="win",
    count=1,
    before=1000,
    after=1010,
    gid=1,
):
    def insert(c):
        c.execute(
            "INSERT INTO rocket_league_match_history "
            "(guild_id,discord_user_id,player_id,playlist,result,match_count,observed_at,"
            "mmr_before,mmr_after) VALUES(?,?,?,?,?,?,?,?,?)",
            (gid, uid, pid, playlist, result, count, at, before, after),
        )
        c.commit()

    await service.store._run(insert)


async def event(
    service,
    at,
    kind,
    *,
    pid="p",
    playlist=11,
    scope=None,
    count=1,
    rank_before="Diamond I",
    rank_after="Diamond II",
    result=None,
):
    def insert(c):
        c.execute(
            "INSERT INTO rocket_league_events (guild_id,discord_user_id,player_id,player_name,"
            "event_type,playlist,occurred_at,scope,event_count,rank_before,rank_after,"
            "mmr_before,mmr_after,result,position_before,position_after) "
            "VALUES(1,2,?,'Player',?,?,?,?,?,?,?,1000,1010,?,3,1)",
            (pid, kind, playlist, at, scope, count, rank_before, rank_after, result),
        )
        c.commit()

    await service.store._run(insert)


def test_calendar_periods_and_weekday():
    p = {**defaults(), "timezone": "America/La_Paz"}
    assert period(NOW, "daily", p) == (stamp("2026-09-06T04:00:00"), stamp("2026-09-07T04:00:00"))
    assert period(NOW, "weekly", p) == (stamp("2026-08-31T04:00:00"), stamp("2026-09-07T04:00:00"))
    p["weekday"] = 6
    assert period(NOW, "weekly", p)[1] == stamp("2026-09-06T04:00:00")
    assert next_delivery(NOW, "daily", p) == stamp("2026-09-07T13:00:00")


def test_dst_short_long_days_and_nonexistent_or_repeated_delivery():
    p = {**defaults(), "timezone": "America/New_York", "hour": 2, "minute": 30}
    start, end = period(stamp("2026-03-09T12:00:00"), "daily", p)
    assert end - start == 23 * 3600
    start, end = period(stamp("2026-11-02T12:00:00"), "daily", p)
    assert end - start == 25 * 3600
    due = next_delivery(stamp("2026-03-08T05:00:00"), "daily", p)
    assert due == stamp("2026-03-08T07:30:00")  # gap shifts forward
    p["hour"] = 1
    due = next_delivery(stamp("2026-11-01T04:00:00"), "daily", p)
    assert due == stamp("2026-11-01T05:30:00")
    assert next_delivery(due, "daily", p) == stamp("2026-11-02T06:30:00")


@pytest.mark.parametrize(
    "changes",
    [
        {"hour": True},
        {"minute": 60},
        {"weekday": 7},
        {"minimum": -1},
        {"top": 16},
        {"timezone": "Not/AZone"},
        {"timezone": "../../secret"},
        {"enabled": 1},
        {"skip_empty": "false"},
        {"sections": []},
        {"sections": ["results", "results"]},
        {"sections": ["nope"]},
        {"sections": [True]},
        {"playlists": [True]},
        {"playlists": [999]},
        {"channel_id": 7},
    ],
)
def test_settings_validation(changes):
    with pytest.raises(ValueError):
        validate("daily", "self", {**defaults(), **changes}, 2)


async def test_exact_boundaries_scopes_gaps_mmr_and_comparison(summaries):
    start, end = period(NOW, "daily", defaults())
    await match(summaries, start - 1)  # previous period
    await match(summaries, start, before=1000, after=1040, result="gap", count=3)
    await match(summaries, start, before=1000, after=1040)  # known tail of same poll
    await match(summaries, end - 1, before=1040, after=1030, result="loss")
    await match(summaries, end)  # excluded
    await match(summaries, start, pid="friend", uid=3, playlist=10)
    await match(summaries, start, pid="left", uid=4)
    await match(summaries, start, gid=99, count=1000)
    r = await summaries.report(1, {"p": "Me"}, "daily", defaults(), NOW)
    assert r["totals"] == dict(games=5, wins=1, losses=1, unknown=3, win_rate=50, mmr=30)
    assert r["previous"]["games"] == 1
    assert r["daily"][0]["games"] == 5
    all_players = await summaries.report(
        1, {"p": "Me", "friend": "Friend"}, "daily", defaults(), NOW
    )
    assert all_players["totals"]["games"] == 6
    assert all_players["totals"]["mmr"] == 40
    assert len(all_players["players"]) == 2
    filtered = await summaries.report(
        1, {"p": "Me", "friend": "Friend"}, "daily", {**defaults(), "playlists": [10]}, NOW
    )
    assert filtered["totals"]["games"] == 1


async def test_missing_mmr_is_not_claimed_as_zero(summaries):
    await match(summaries, NOW - 86400, before=None)
    r = await summaries.report(1, {"p": "P"}, "daily", defaults(), NOW)
    assert r["totals"]["mmr"] is None
    assert "Net MMR: unavailable" in report_text(r)


async def test_event_categories_rank_endpoints_and_section_omission(summaries):
    at = NOW - 86400
    for kind in (
        "rank_up",
        "rank_down",
        "peak_mmr",
        "session_streak_completed",
        "all_time_streak_completed",
        "leaderboard_movement",
    ):
        await event(summaries, at, kind)
    await event(summaries, at, "streak_leaderboard_movement", scope="main", playlist=0, count=5)
    await event(summaries, at, "streak_leaderboard_movement", scope="all", playlist=0, count=8)
    await event(summaries, at, "rank_up", pid="left")
    r = await summaries.report(1, {"p": "P"}, "daily", defaults(), NOW)
    assert r["highlights"]["rank_change"]["count"] == 2
    assert all(h["count"] for h in r["highlights"].values())
    assert r["highlights"]["streak_leaderboard_competitive"]["items"][0]["best"] == 5
    assert r["highlights"]["streak_leaderboard_all_ranked"]["items"][0]["best"] == 8
    selected = await summaries.report(
        1, {"p": "P"}, "daily", {**defaults(), "sections": ["rank_change"]}, NOW
    )
    text = report_text(selected)
    assert set(selected["highlights"]) == {"rank_change"}
    assert "Diamond I → Diamond II" in text and "Net MMR" not in text
    assert "New MMR peaks" not in text and "0 games" not in text


async def test_retention_current_preview_and_bounds(summaries):
    await match(summaries, NOW - 1)
    await match(summaries, NOW + 1)
    r = await summaries.report(1, {"p": "P"}, "daily", defaults(), NOW, 0)
    assert not r["complete"] and r["totals"]["games"] == 1
    r = await summaries.report(1, {"p": "P"}, "weekly", defaults(), NOW + 5 * 86400, 3)
    assert not r["comparison_available"]
    with pytest.raises(ValueError):
        await summaries.report(1, {}, "weekly", defaults(), NOW, 5)


async def test_scheduler_optin_independence_restart_coalescing_and_cancel(summaries):
    p = {**defaults(), "enabled": True}
    assert await summaries.due(NOW) == []
    for cadence, scope in (("daily", "self"), ("daily", "server"), ("weekly", "self")):
        await summaries.save(1, 2, cadence, scope, p, NOW)
    assert await summaries.due(NOW) == []
    due = next_delivery(NOW, "daily", p)
    jobs = await summaries.due(due)
    assert len(jobs) == 2
    await summaries.finish(jobs[0], "delivered", due)
    await summaries.store.initialize()
    assert len(await summaries.due(due)) == 1
    await summaries.save(1, 2, "daily", "server", {**p, "enabled": False}, due)
    assert await summaries.due(due) == []
    jobs = await summaries.due(NOW + 15 * 86400)
    assert len(jobs) == 2  # one newest daily + one newest weekly, not 17 reports
    assert {j["cadence"] for j in jobs} == {"daily", "weekly"}


async def test_retry_bounds_expiry_and_guild_channel_cleanup(summaries):
    p = {**defaults(), "enabled": True, "channel_id": 99}
    await summaries.save(1, 0, "daily", "server", p, NOW)
    due = next_delivery(NOW, "daily", p)
    for attempt in range(5):
        job = (await summaries.due(due))[0]
        assert job["attempts"] == attempt
        await summaries.finish(job, "retry", due)
        due += 60 * 2**attempt
    assert await summaries.due(due) == []
    assert (await summaries.deliveries(1, 0))[0]["status"] == "failed"
    await summaries.store.delete_channel(guild_id=1, channel_id=99)
    s = await summaries.settings(1, 0, "daily", "server")
    assert s["config"]["enabled"] is False and not s["next_at"]
    await summaries.store.delete_guild(guild_id=1)
    assert await summaries.deliveries(1, 0) == []
    assert not (await summaries.settings(1, 0, "daily", "server"))["revision"]


def fake_bot(summaries):
    member = SimpleNamespace(id=2, guild_permissions=discord.Permissions(manage_guild=True))
    guild = SimpleNamespace(
        id=1,
        name="Friends",
        me=member,
        get_member=lambda _: member,
        fetch_member=AsyncMock(return_value=member),
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild, channel.send = 99, guild, AsyncMock()
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True
    )
    guild.get_channel = lambda cid: channel if cid == 99 else None
    user = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(
        store=summaries.store,
        get_guild=lambda gid: guild if gid == 1 else None,
        is_ready=lambda: True,
        telegram=None,
        web=None,
        get_user=lambda _: user,
    )
    return bot, guild, member, channel, user


async def test_shared_admin_authorization_scope_and_destination(summaries):
    bot, guild, member, channel, _ = fake_bot(summaries)
    cog = SummaryCog(bot)
    p = {**defaults(), "enabled": True, "channel_id": 99}
    member.guild_permissions = discord.Permissions.none()
    with pytest.raises(PermissionError):
        await cog.configure(guild, member, "daily", "server", p, channel_alert=True)
    member.guild_permissions = discord.Permissions(manage_guild=True)
    with pytest.raises(ValueError):
        await cog.configure(guild, member, "daily", "self", p, channel_alert=True)
    channel.permissions_for.return_value = discord.Permissions.none()
    with pytest.raises(PermissionError):
        await cog.configure(guild, member, "daily", "server", p, channel_alert=True)


async def test_delivery_rechecks_membership_permissions_and_selected_empty(summaries):
    bot, guild, _, channel, user = fake_bot(summaries)
    cog = SummaryCog(bot)
    await summaries.store.save_rocket_league_account(
        guild_id=1, discord_user_id=2, player_id="p", player_name="P"
    )
    p = {**defaults(), "enabled": True, "minimum": 0, "sections": ["peak_mmr"]}
    await summaries.save(1, 2, "daily", "self", p, NOW)
    due = next_delivery(NOW, "daily", p)
    await match(summaries, due - 36000)
    await cog.deliver(due)  # games alone should not trigger a peaks-only report
    user.send.assert_not_awaited()
    assert (await summaries.deliveries(1, 2))[0]["status"] == "skipped"
    await event(summaries, due + 86400 - 36000, "peak_mmr")
    await cog.deliver(due + 86400)
    user.send.assert_awaited_once()
    await cog.deliver(due + 86400)
    user.send.assert_awaited_once()
    guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "left")
    await cog.deliver(due + 2 * 86400)
    user.send.assert_awaited_once()
    await summaries.save(1, 0, "daily", "server", {**p, "channel_id": 99}, NOW)
    channel.permissions_for.return_value = discord.Permissions.none()
    await cog.deliver(due)
    channel.send.assert_not_awaited()


async def test_compact_large_server_and_hostile_names(summaries):
    accounts = {f"p{i}": "_*@everyone_" * 30 + str(i) for i in range(50)}
    for pid in accounts:
        await match(summaries, NOW - 86400, pid=pid)
        await event(summaries, NOW - 86400, "peak_mmr", pid=pid)
    r = await summaries.report(1, accounts, "daily", {**defaults(), "top": 15}, NOW)
    assert r["totals"]["games"] == 50 and len(r["players"]) == 50
    assert len(report_text(r)) < 3700
    bot, *_ = fake_bot(summaries)
    embed = SummaryCog(bot).embed(r, "_" * 100, "server")
    assert len(embed.description) <= 4096 and len(embed.title) <= 256 and len(embed) <= 6000
    assert "@everyone" not in embed.description
    assert set(r["sections"]) == set(SECTIONS)
    json.dumps(r)


async def test_replacing_instant_routes_is_explicit_atomic_and_survives_restart(summaries):
    store = summaries.store
    for key in ("peak_mmr", "rank_change", "session_streak", "leaderboard_movement"):
        await store.configure_rocket_league_event_notification(
            guild_id=1, event_key=key, channel_id=99
        )
    await store._run(
        lambda c: c.execute(
            "INSERT INTO rocket_league_tracking_config "
            "(guild_id,streak_channel_id,leaderboard_channel_id) VALUES(1,99,99)"
        )
    )
    p = {
        **defaults(),
        "enabled": True,
        "channel_id": 99,
        "sections": ["peak_mmr", "session_streak", "leaderboard_movement"],
    }
    await summaries.save(1, 0, "weekly", "server", p, NOW)
    assert len(await store.list_rocket_league_event_notification_configs(guild_id=1)) == 4
    await summaries.save(1, 0, "weekly", "server", p, NOW, replace_instant=True)
    await store.initialize()
    assert [
        r[0] for r in await store.list_rocket_league_event_notification_configs(guild_id=1)
    ] == ["rank_change"]
    with pytest.raises(ValueError):
        await summaries.save(
            1, 2, "weekly", "self", {**p, "channel_id": None}, NOW, replace_instant=True
        )


async def test_cache_coalesces_and_isolates_accounts_config_and_mutations(summaries):
    await match(summaries, NOW - 86400)
    first, second = await asyncio.gather(
        *[summaries.report(1, {"p": "P"}, "daily", defaults(), NOW) for _ in range(2)]
    )
    assert len(summaries.cache) == 1 and first == second
    first["players"].clear()
    assert second["players"]
    hidden = await summaries.report(1, {}, "daily", defaults(), NOW)
    assert not hidden["players"] and len(summaries.cache) == 2
    selected = await summaries.report(
        1, {"p": "P"}, "daily", {**defaults(), "sections": ["mmr"]}, NOW
    )
    assert selected["sections"] == ["mmr"] and len(summaries.cache) == 3
    await summaries.close()


async def test_additional_channel_routes_delivery_and_independent_cleanup(summaries):
    bot, guild, member, channel, _ = fake_bot(summaries)
    cog = SummaryCog(bot)
    p = {**defaults(), "enabled": True, "channel_id": 99, "skip_empty": False, "minimum": 0}
    await cog.configure(guild, member, "daily", "server", p, channel_alert=True, schedule_id=-99)
    await summaries.save(1, 0, "daily", "server", {**p, "channel_id": 98}, NOW)
    await summaries.save(1, -99, "daily", "server", p, NOW)
    assert len(await summaries.channel_settings(1)) == 3
    await cog.deliver(next_delivery(NOW, "daily", p))
    channel.send.assert_awaited_once()
    records = await summaries.deliveries(1, 0, channels=True)
    assert {r["status"] for r in records} == {"delivered", "blocked"}
    await summaries.store.delete_channel(guild_id=1, channel_id=99)
    assert not (await summaries.settings(1, -99, "daily", "server"))["config"]["enabled"]
    assert (await summaries.settings(1, 0, "daily", "server"))["config"]["enabled"]


async def test_activity_only_reports_snapshots_and_retries(summaries):
    bot, _, _, _, user = fake_bot(summaries)
    cog = SummaryCog(bot)
    await summaries.store.save_rocket_league_account(
        guild_id=1, discord_user_id=2, player_id="p", player_name="P"
    )
    p = {**defaults(), "enabled": True, "sections": ["activity"]}
    await summaries.save(1, 2, "daily", "self", p, NOW)
    due = next_delivery(NOW, "daily", p)
    await match(summaries, due - 36000)
    await cog.deliver(due)
    user.send.assert_awaited_once()
    row = (await summaries.deliveries(1, 2))[0]
    assert json.loads(row["receipts"])["discord"]["status"] == "delivered"
    snapshot = await summaries.snapshot(1, 2, row["id"])
    assert snapshot["totals"]["games"] == 1
    assert snapshot["profile_ids"] == ["p"]
    assert await summaries.snapshot(1, 3, row["id"]) is None
    assert await summaries.snapshot(99, 2, row["id"]) is None


async def test_thresholds_full_exports_and_section_order(summaries):
    for i in range(24):
        await match(summaries, NOW - 86400, pid=f"p{i}")
    await event(summaries, NOW - 86400, "session_streak_completed", count=2)
    await event(summaries, NOW - 86400, "session_streak_completed", count=8)
    await event(summaries, NOW - 86400, "peak_mmr")
    p = {
        **defaults(),
        "sections": ["session_streak", "players", "mmr", "peak_mmr"],
        "minimum_streak": 5,
        "minimum_peak_gain": 11,
        "player_order": "name",
    }
    accounts = {f"p{i}": f"=Player{i:02}" for i in range(24)}
    accounts["p"] = "P"
    r = await summaries.report(1, accounts, "daily", p, NOW)
    assert r["highlights"]["session_streak"]["count"] == 1
    assert r["highlights"]["peak_mmr"]["count"] == 0
    compact, full, csv = report_text(r), report_text(r, compact=False), report_csv(r)
    assert full.index("Completed session streaks") < full.index("Player breakdown")
    assert "=Player23" in full and "'=Player23" in csv
    assert "=Player23" not in compact and "detail(s) in Reports" in compact
    assert "Wins" not in csv and "Uncertain" not in csv


async def test_selected_history_categories_are_filtered_before_limit(summaries):
    await event(summaries, NOW - 500, "peak_mmr")
    for i in range(6):
        await event(summaries, NOW - i, "rank_up")
    rows = await summaries.store.list_rocket_league_events(
        guild_id=1, since=NOW - 1000, event_keys=["peak_mmr"], limit=2
    )
    assert len(rows) == 1 and rows[0].event_type == "peak_mmr"
    assert not await summaries.store.list_rocket_league_events(guild_id=1, since=0, event_keys=[])


async def test_outage_supersedes_pending_and_thirty_day_browsing(summaries):
    p = {**defaults(), "enabled": True}
    await summaries.save(1, 2, "daily", "self", p, NOW)
    due = next_delivery(NOW, "daily", p)
    assert len(await summaries.due(due)) == 1
    assert len(await summaries.due(due + 86400)) == 1
    assert {r["status"] for r in await summaries.deliveries(1, 2)} == {"pending", "superseded"}
    await match(summaries, NOW - 10 * 86400)
    r = await summaries.report(1, {"p": "P"}, "daily", defaults(), NOW, 10)
    assert r["totals"]["games"] == 1
    assert (await summaries.report(1, {}, "daily", defaults(), NOW, 30))["retained_partial"]


async def test_preview_sends_are_authorized_throttled_and_do_not_save(summaries, monkeypatch):
    # A fresh Linux runner can have less than 60 seconds of monotonic uptime.
    monkeypatch.setattr("slopbot.summary_alerts.time.monotonic", lambda: 10.0)
    bot, guild, member, channel, _ = fake_bot(summaries)
    cog = SummaryCog(bot)
    config = {**defaults(), "channel_id": 99}
    member.guild_permissions = discord.Permissions.none()
    with pytest.raises(PermissionError):
        await cog.send_preview(guild, member, "daily", "server", config, channel_alert=True)
    channel.send.assert_not_awaited()
    member.guild_permissions = discord.Permissions(manage_guild=True)
    result = await cog.send_preview(guild, member, "daily", "server", config, channel_alert=True)
    assert result["delivered"]
    channel.send.assert_awaited_once()
    assert not (await summaries.settings(1, 0, "daily", "server"))["revision"]
    with pytest.raises(ValueError, match="one minute"):
        await cog.send_preview(guild, member, "daily", "server", config, channel_alert=True)


async def test_custom_report_range_is_inclusive_and_excludes_outside_data(summaries):
    p = {**defaults(), "timezone": "UTC"}
    start, end = stamp("2026-09-03T00:00:00"), stamp("2026-09-06T00:00:00")
    for at in (start - 1, start, end - 1, end):
        await match(summaries, at)
    r = await summaries.report(
        1, {"p": "Player"}, "daily", p, NOW, date_range=(date(2026, 9, 3), date(2026, 9, 5))
    )
    assert (r["start"], r["end"], r["scheduled_end"]) == (start, end, end)
    assert r["totals"]["games"] == 2
    assert r["cadence"] == "custom" and r["complete"]
    assert "custom" in report_csv(r)
    assert "Player" in report_text(r, compact=False)


async def test_custom_report_timezone_dst_and_current_day(summaries):
    p = {**defaults(), "timezone": "America/New_York"}
    now = stamp("2026-03-10T12:00:00")
    r = await summaries.report(
        1, {}, "daily", p, now, date_range=(date(2026, 3, 8), date(2026, 3, 8))
    )
    assert r["end"] - r["start"] == 23 * 3600
    current = await summaries.report(
        1, {}, "daily", p, now, date_range=(date(2026, 3, 10), date(2026, 3, 10))
    )
    assert current["end"] == now
    assert current["scheduled_end"] > now and not current["complete"]


@pytest.mark.parametrize(
    "first,last",
    [
        (date(2026, 8, 8), date(2026, 9, 7)),
        (date(2026, 9, 7), date(2026, 9, 8)),
        (date(2026, 9, 6), date(2026, 9, 5)),
    ],
)
async def test_custom_report_rejects_invalid_retained_range(summaries, first, last):
    with pytest.raises(ValueError, match="last 30 days"):
        await summaries.report(
            1, {}, "daily", {**defaults(), "timezone": "UTC"}, NOW, date_range=(first, last)
        )
