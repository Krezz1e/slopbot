from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from slopbot.database import SubscriptionStore
from slopbot.engagement import DeliveryOutcome
from slopbot.models import RocketLeagueAccount, TournamentOptInDelivery, TournamentUserAlert
from slopbot.rocketleague import (
    RocketLeagueActivity,
    RocketLeagueAuthorization,
    RocketLeagueCog,
    SlopbotStatusSnapshot,
    StatusCog,
)
from slopbot.tournaments import MAX_OPT_IN_DELIVERY_ATTEMPTS, TournamentAlertsCog
from slopbot.web.auth import WebIdentityStore


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_database_call_holds_lock_until_thread_exits(
    tmp_path: Path, worker_fails: bool
) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    started = asyncio.Event()
    release = threading.Event()
    second_started = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_operation(connection: sqlite3.Connection) -> None:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test did not release database worker"
        if worker_fails:
            raise ValueError("cancelled worker failed")

    def next_operation(connection: sqlite3.Connection) -> None:
        second_started.set()

    first = asyncio.create_task(store._run(slow_operation))
    await asyncio.wait_for(started.wait(), timeout=2)
    first.cancel()
    second = asyncio.create_task(store._run(next_operation))
    try:
        await asyncio.sleep(0.02)
        first.cancel()  # Shutdown may cancel a task again while its worker is draining.
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    assert second_started.is_set()


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_web_identity_call_holds_lock_until_thread_exits(
    tmp_path: Path, worker_fails: bool
) -> None:
    store = WebIdentityStore(tmp_path / "web.db")
    await store.initialize()
    started = asyncio.Event()
    release = threading.Event()
    second_started = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_operation(connection: sqlite3.Connection) -> None:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test did not release web identity worker"
        if worker_fails:
            raise ValueError("cancelled web identity worker failed")

    def next_operation(connection: sqlite3.Connection) -> None:
        second_started.set()

    first = asyncio.create_task(store.run(slow_operation))
    await asyncio.wait_for(started.wait(), timeout=2)
    first.cancel()
    second = asyncio.create_task(store.run(next_operation))
    try:
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    assert second_started.is_set()


@pytest.mark.parametrize(
    "cache_state", ["guild_missing", "member_missing", "forbidden", "outage", "left"]
)
async def test_tournament_cache_misses_do_not_destroy_subscriptions(
    cache_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = SimpleNamespace(id=2)
    fetch = AsyncMock(return_value=member)
    if cache_state in {"forbidden", "outage", "left"}:
        status = {"forbidden": 403, "outage": 503, "left": 404}[cache_state]
        error = {403: discord.Forbidden, 503: discord.HTTPException, 404: discord.NotFound}[status]
        fetch.side_effect = error(SimpleNamespace(status=status, reason="test"), "test")
    guild = SimpleNamespace(get_member=lambda _: None, fetch_member=fetch)
    bot = SimpleNamespace(get_guild=lambda _: None if cache_state == "guild_missing" else guild)
    store = SimpleNamespace(
        delete_tournament_user_alert=AsyncMock(),
        finish_tournament_opt_in=AsyncMock(),
        retry_tournament_opt_in=AsyncMock(),
    )
    send = AsyncMock(return_value=DeliveryOutcome(True, False, "delivered"))
    monkeypatch.setattr("slopbot.tournaments.send_to_destinations", send)
    cog = TournamentAlertsCog(bot, store, None, None)
    alert = TournamentUserAlert(1, 2, "USE", 15, (2,), ("soccar",))
    # Isolate membership/delivery behavior from tournament formatting.
    monkeypatch.setattr(
        "slopbot.tournaments.tournament_alert_embed", lambda *a, **k: discord.Embed()
    )
    monkeypatch.setattr("slopbot.tournaments.tournament_alert_text", lambda *a, **k: "Reminder")
    complete = await cog._send_user_alert(alert, "USE", [])
    delivery = TournamentOptInDelivery("token", 1, 2, 9, "Cup", 2000, "USE", 15, 0)
    await cog._process_opt_in_delivery(delivery, now=1000)
    if cache_state == "left":
        assert complete
        store.delete_tournament_user_alert.assert_awaited_once()
        store.finish_tournament_opt_in.assert_awaited_once()
        send.assert_not_awaited()
    elif cache_state == "member_missing":
        assert complete
        assert send.await_count == 2
        store.delete_tournament_user_alert.assert_not_awaited()
        store.finish_tournament_opt_in.assert_awaited_once()
    else:
        assert not complete
        store.delete_tournament_user_alert.assert_not_awaited()
        store.finish_tournament_opt_in.assert_not_awaited()
        store.retry_tournament_opt_in.assert_awaited_once()
        assert store.retry_tournament_opt_in.call_args.kwargs["next_attempt_at"] == 1060
        send.assert_not_awaited()


async def test_missing_guild_opt_in_retries_remain_bounded() -> None:
    store = SimpleNamespace(
        finish_tournament_opt_in=AsyncMock(), retry_tournament_opt_in=AsyncMock()
    )
    cog = TournamentAlertsCog(SimpleNamespace(get_guild=lambda _: None), store, None, None)
    delivery = TournamentOptInDelivery(
        "token", 1, 2, 9, "Cup", 2000, "USE", 15, MAX_OPT_IN_DELIVERY_ATTEMPTS - 1
    )
    await cog._process_opt_in_delivery(delivery, now=1000)
    store.finish_tournament_opt_in.assert_awaited_once()
    store.retry_tournament_opt_in.assert_not_awaited()


async def test_claiming_epic_account_preserves_mmr_ownership(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    account = await store.save_unlinked_rocket_league_account(
        guild_id=1, player_id="Epic|abc|0", player_name="Player"
    )
    for observed_at, mmr, matches in ((1000, 1000, 10), (2000, 1010, 11)):
        await store.record_rocket_league_activity_batch(
            guild_id=1,
            discord_user_id=account.discord_user_id,
            player_id=account.player_id,
            player_name=account.player_name,
            activities=[(27, matches, 1, mmr, "Gold")],
            threshold=3,
            observed_at=observed_at,
        )
    await store.save_rocket_league_account(
        guild_id=1, discord_user_id=42, player_id=account.player_id, player_name="Player"
    )
    movers = await store.list_rocket_league_movers(guild_id=1, playlist=27, since=0)
    assert movers == [(account.player_id, "Player", 42, 1000.0, 1010.0)]
    peaks = await store.list_rocket_league_peak_mmr(guild_id=1, playlist=27)
    assert peaks[0].discord_user_id == 42
    assert peaks[0].mmr == 1010
    # Special-mode match observations also transfer to the linked member.
    history = await store.list_rocket_league_match_history(guild_id=1, discord_user_id=42, since=0)
    assert [(event.playlist, event.result) for event in history] == [(27, "win")]
    # Reproduce rows written by the older claim path and check startup repairs them.
    with sqlite3.connect(tmp_path / "bot.db") as connection:
        for table in ("rocket_league_mmr_history", "rocket_league_peak_mmr"):
            connection.execute(
                f"UPDATE {table} SET discord_user_id = ?", (account.discord_user_id,)
            )
    await store.initialize()
    await store.initialize()
    assert await store.list_rocket_league_movers(guild_id=1, playlist=27, since=0) == movers
    assert (await store.list_rocket_league_peak_mmr(guild_id=1, playlist=27))[
        0
    ].discord_user_id == 42


async def test_movers_keeps_both_directions_past_twenty_candidates(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    now = int(time.time())
    for user_id in range(1, 23):
        delta = 100 if user_id <= 21 else -10
        for observed_at, mmr, matches in ((now - 100, 1000, 10), (now, 1000 + delta, 11)):
            await store.record_rocket_league_activity_batch(
                guild_id=1,
                discord_user_id=user_id,
                player_id=f"Epic|{user_id}|0",
                player_name=f"Player {user_id}",
                activities=[(11, matches, 0, mmr, "Gold")],
                threshold=3,
                observed_at=observed_at,
            )
    guild = SimpleNamespace(
        id=1, get_member=lambda uid: SimpleNamespace(display_name=f"Player {uid}")
    )
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=1,
        channel_id=99,
        channel=None,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    await RocketLeagueCog.movers.callback(SimpleNamespace(store=store), interaction)
    fields = {
        field.name: field.value
        for field in interaction.edit_original_response.call_args.kwargs["embed"].fields
    }
    assert "Player 22" in fields["↓ Fallers"]
    assert "-10" in fields["↓ Fallers"]
    assert len(fields["↑ Climbers"].splitlines()) == 5


async def test_status_command_reads_current_health_instead_of_ten_minute_cache() -> None:
    old = SlopbotStatusSnapshot(("Healthy",), True, 1000)
    current = SlopbotStatusSnapshot(("Database unavailable",), False, 1600)
    cog = SimpleNamespace(_latest=old, _collect_status=AsyncMock(return_value=current))
    member = Mock(spec=discord.Member)
    member.guild_permissions.manage_guild = True
    interaction = SimpleNamespace(
        user=member,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    await StatusCog.slopbot_status.callback(cog, interaction)
    cog._collect_status.assert_awaited_once()
    assert (
        "Database unavailable"
        in interaction.edit_original_response.call_args.kwargs["embed"].description
    )


async def test_reauthorization_command_uses_shared_service_flow() -> None:
    service = SimpleNamespace(
        start_authorization=AsyncMock(
            return_value=RocketLeagueAuthorization(
                "https://www.epicgames.com/activate", "ABCDEFGH", 1_800_000_000
            )
        )
    )
    cog = SimpleNamespace(bot=SimpleNamespace(rocket_league=service))
    member = Mock(spec=discord.Member)
    member.guild_permissions.manage_guild = True
    interaction = SimpleNamespace(
        user=member,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await StatusCog.reauthorize_rocket_league.callback(cog, interaction)

    service.start_authorization.assert_awaited_once()
    content = interaction.edit_original_response.call_args.kwargs["content"]
    assert "https://www.epicgames.com/activate" in content
    assert "ABCDEFGH" in content


async def test_leaderboard_write_failure_keeps_previous_snapshot_for_retry(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    config = await store.get_rocket_league_tracking_config(guild_id=1)
    accounts = [RocketLeagueAccount(1, uid, f"Epic|{uid}|0", f"Player {uid}") for uid in (1, 2)]
    cog = RocketLeagueCog(SimpleNamespace(get_guild=lambda _: None), store, None)
    baseline = [(1, 1, 1100.0), (2, 2, 1000.0)]
    await store.replace_rocket_league_leaderboard_snapshot(
        guild_id=1, playlist=11, entries=baseline
    )
    activity = {
        account.player_id: [RocketLeagueActivity(account.player_id, 11, mmr, 1, 0, "Gold", 1, 10)]
        for account, mmr in zip(accounts, (1100, 1200), strict=True)
    }
    with sqlite3.connect(tmp_path / "bot.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_summary BEFORE INSERT ON rocket_league_events "
            "WHEN NEW.event_type = 'leaderboard_update' "
            "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
        await cog._update_leaderboard(config, 11, accounts, activity, 1000)
    assert await store.get_rocket_league_leaderboard_snapshot(guild_id=1, playlist=11) == baseline
    assert await store.list_rocket_league_events(guild_id=1, since=0) == []
    with sqlite3.connect(tmp_path / "bot.db") as connection:
        connection.execute("DROP TRIGGER reject_summary")
    await cog._update_leaderboard(config, 11, accounts, activity, 1030)
    assert await store.get_rocket_league_leaderboard_snapshot(guild_id=1, playlist=11) == [
        (2, 1, 1200.0),
        (1, 2, 1100.0),
    ]
    assert len(await store.list_rocket_league_events(guild_id=1, since=0)) == 2
    summaries = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=["leaderboard_update"]
    )
    assert len(summaries) == 1
