from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from slopbot.database import SubscriptionStore
from slopbot.itemshop import ItemShopCog
from slopbot.models import CommandPolicy, ResponseVisibility, RocketLeagueAccount, RocketLeagueEvent
from slopbot.rocketleague import (
    PlayerDashboardView,
    RocketLeagueActivity,
    RocketLeagueCog,
    leaderboard_change_description,
)
from slopbot.tournaments import TournamentAlertsCog
from slopbot.ui import EmbedPagesView
from slopbot.visibility import defer_result_response


async def test_standings_card_survives_retry_without_duplicate_movement_cards(
    tmp_path: Path,
) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    await store.configure_rocket_league_event_notification(
        guild_id=1, event_key="leaderboard_movement", channel_id=99, configured_at=0
    )
    config = await store.get_rocket_league_tracking_config(guild_id=1)
    names = {1: "William", 2: "gorb floy", 3: "Ethan", 4: "Krezzle", 5: "Mitch", 6: "Elijah"}
    guild = SimpleNamespace(get_member=lambda user_id: SimpleNamespace(display_name=names[user_id]))
    channel = Mock(spec=discord.TextChannel, id=99, guild=guild)
    channel.send = AsyncMock()
    bot: Any = SimpleNamespace(get_guild=lambda _: guild, get_channel=lambda _: channel)
    cog = RocketLeagueCog(bot, store, None)
    accounts = [
        RocketLeagueAccount(1, user_id, str(user_id), name) for user_id, name in names.items()
    ]

    async def poll(mmrs: list[int], observed_at: int) -> None:
        activity = {
            str(user_id): [RocketLeagueActivity(str(user_id), 11, mmr, 1, 0, "Bronze", 0, 10)]
            for user_id, mmr in enumerate(mmrs, 1)
        }
        await cog._update_leaderboard(config, 11, accounts, activity, observed_at)

    await poll([1020, 1002, 944, 938, 790, 602], 100)
    assert await store.list_pending_rocket_league_event_notifications() == []
    await poll([1021, 1003, 945, 939, 790, 602], 200)
    assert await store.list_pending_rocket_league_event_notifications() == []
    await poll([1021, 1030, 945, 950, 790, 602], 300)
    pending = await store.list_pending_rocket_league_event_notifications(limit=1)
    assert len(pending) == 1
    event = pending[0][0]
    assert event.event_type == "leaderboard_update"
    assert event.notification_text is not None
    assert "**1.** gorb floy — **1,030 MMR** (↑1)" in event.notification_text
    assert "**2.** William — **1,021 MMR** (↓1)" in event.notification_text
    assert "**5.** Mitch — **790 MMR** (—)" in event.notification_text
    assert len(event.notification_text.splitlines()) == 6
    history = await store.list_rocket_league_events(guild_id=1, since=0)
    assert len(history) == 4
    assert all(item.event_type == "leaderboard_movement" for item in history)

    channel.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=503, reason="Unavailable"), "Try again"
    )
    await cog._deliver_event_notifications()
    assert len(await store.list_pending_rocket_league_event_notifications()) == 1
    # A later same-order poll must not change a queued card's numbers.
    await poll([1022, 1031, 946, 951, 790, 602], 400)
    reopened = SubscriptionStore(tmp_path / "bot.db")
    await reopened.initialize()
    cog.store = reopened
    channel.send.reset_mock(side_effect=True)
    await cog._deliver_event_notifications()
    await cog._deliver_event_notifications()
    channel.send.assert_awaited_once()
    sent = channel.send.call_args.kwargs
    assert sent["embed"].title == "Ranked Doubles (2v2) leaderboard changed"
    assert sent["embed"].description == event.notification_text
    assert sent["allowed_mentions"].everyone is False
    assert await reopened.list_pending_rocket_league_event_notifications() == []


def test_large_standings_neutralize_names_and_fit_one_embed() -> None:
    description = leaderboard_change_description(
        [(index, "@everyone\n" + "_*" * 100, 1234, None) for index in range(1, 501)]
    )
    assert len(description) <= 4096
    assert "@everyone" not in description
    assert "more players" in description
    assert "(new)" in description
    assert len(description.splitlines()) <= 26


@pytest.mark.parametrize(
    ("cog_type", "command_name"),
    [
        (RocketLeagueCog, "today"),
        (RocketLeagueCog, "match_history"),
        (RocketLeagueCog, "rank_current"),
        (RocketLeagueCog, "rank_peak"),
        (RocketLeagueCog, "mmr_history"),
        (RocketLeagueCog, "movers"),
        (RocketLeagueCog, "player_overview"),
        (RocketLeagueCog, "leaderboard"),
        (RocketLeagueCog, "streak_leaderboard"),
        (RocketLeagueCog, "peak_leaderboard"),
        (ItemShopCog, "current"),
        (ItemShopCog, "changes"),
        (TournamentAlertsCog, "tournaments"),
    ],
)
async def test_result_commands_acknowledge_before_slow_denial(
    monkeypatch: pytest.MonkeyPatch, cog_type: Any, command_name: str
) -> None:
    monkeypatch.setattr("slopbot.visibility.RESULT_DEFER_TIMEOUT_SECONDS", 0.001)
    deferred = asyncio.Event()
    response = SimpleNamespace(
        defer=AsyncMock(side_effect=lambda **_: deferred.set()), send_message=AsyncMock()
    )

    async def deny(**_: Any) -> bool:
        # Hold the lookup until acknowledgement instead of racing short sleeps on CI.
        await asyncio.wait_for(deferred.wait(), timeout=2)
        response.defer.assert_awaited_once_with(ephemeral=True)
        return False

    store = SimpleNamespace(
        command_channel_is_allowed=AsyncMock(side_effect=deny),
        get_command_policy=AsyncMock(
            return_value=CommandPolicy("test", ResponseVisibility.PRIVATE, True, ())
        ),
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        guild_id=1,
        channel_id=99,
        channel=None,
        user=Mock(spec=discord.Member, id=1),
        response=response,
        edit_original_response=AsyncMock(),
    )
    # Denial happens before any cog-specific service or account work.
    cog = SimpleNamespace(store=store)
    await getattr(cog_type, command_name).callback(cog, interaction)
    response.send_message.assert_not_awaited()
    assert "not allowed" in interaction.edit_original_response.call_args.kwargs["content"]


async def test_dashboard_defers_graph_work_and_tracks_selection() -> None:
    response = SimpleNamespace(defer=AsyncMock())

    async def graph(**_: Any) -> tuple[discord.Embed, None]:
        response.defer.assert_awaited_once()
        return discord.Embed(title="Chart"), None

    view = PlayerDashboardView(
        owner_id=1,
        guild_id=1,
        cog=SimpleNamespace(_mmr_history_payload=graph),
        account=RocketLeagueAccount(1, 1, "Epic|abc|0", "Player"),
        overview=discord.Embed(title="Overview"),
        all_time_streaks=discord.Embed(title="Streaks"),
    )
    view.section_select._values = ["11"]
    interaction = SimpleNamespace(response=response, edit_original_response=AsyncMock())
    await view._show_section(interaction)
    assert [option.value for option in view.section_select.options if option.default] == ["11"]
    assert interaction.edit_original_response.call_args.kwargs["embed"].title == "Chart"


async def test_activity_pages_stay_in_one_message_and_restrict_controls() -> None:
    pages = [discord.Embed(title="First"), discord.Embed(title="Second")]
    view = EmbedPagesView(owner_id=1, pages=pages)
    response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=response)
    assert view.previous.disabled
    assert await view.interaction_check(interaction)
    await view.next.callback(interaction)
    assert response.edit_message.call_args.kwargs["embed"] is pages[1]
    assert view.next.disabled
    assert view.page.label == "Page 2 of 2"
    await view.previous.callback(interaction)
    assert response.edit_message.call_args.kwargs["embed"] is pages[0]
    interaction.user.id = 2
    assert not await view.interaction_check(interaction)
    assert response.send_message.call_args.kwargs["ephemeral"]


async def test_today_large_result_uses_one_message() -> None:
    store = SimpleNamespace(
        command_channel_is_allowed=AsyncMock(return_value=True),
        get_command_policy=AsyncMock(
            return_value=CommandPolicy("today", ResponseVisibility.PUBLIC, False, ())
        ),
        list_rocket_league_events=AsyncMock(
            return_value=[
                RocketLeagueEvent("match", 1, "Epic|abc|0", "Player", 11, 1000, result="win")
                for _ in range(100)
            ]
        ),
    )
    cog = SimpleNamespace(store=store, _playlist_names={})
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, get_member=lambda _: None),
        guild_id=1,
        channel_id=99,
        channel=None,
        user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await RocketLeagueCog.today.callback(cog, interaction, events=100)
    interaction.edit_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()
    view = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view, EmbedPagesView)
    assert len(view.pages) > 1
    assert sum(page.description.count("**won**") for page in view.pages) == 100
    assert all(len(page) < 6000 and len(page.description) <= 4096 for page in view.pages)


async def test_fast_denial_stays_private_for_public_command() -> None:
    store = SimpleNamespace(
        command_channel_is_allowed=AsyncMock(return_value=False),
        get_command_policy=AsyncMock(
            return_value=CommandPolicy("rank", ResponseVisibility.PUBLIC, True, ())
        ),
    )
    interaction = SimpleNamespace(
        guild_id=1,
        channel_id=99,
        channel=None,
        response=SimpleNamespace(defer=AsyncMock()),
    )
    assert await defer_result_response(
        store=store, interaction=interaction, command_name="rank", visibility=None
    ) == (False, True)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


async def test_summary_suppression_is_limited_to_its_guild_playlist_and_poll(
    tmp_path: Path,
) -> None:
    store = SubscriptionStore(tmp_path / "bot.db")
    await store.initialize()
    for guild_id in (1, 2):
        await store.configure_rocket_league_event_notification(
            guild_id=guild_id,
            event_key="leaderboard_movement",
            channel_id=90 + guild_id,
            configured_at=0,
        )
        await store.configure_rocket_league_event_notification(
            guild_id=guild_id,
            event_key="streak_leaderboard_competitive",
            channel_id=90 + guild_id,
            configured_at=0,
        )
        await store.record_rocket_league_events(
            [
                RocketLeagueEvent("leaderboard_movement", 1, "abc", "Player", 11, 100),
                RocketLeagueEvent("leaderboard_movement", 1, "abc", "Player", 13, 100),
                RocketLeagueEvent("leaderboard_movement", 1, "abc", "Player", 11, 200),
                RocketLeagueEvent(
                    "streak_leaderboard_movement",
                    1,
                    "abc",
                    "Player",
                    11,
                    100,
                    scope="main",
                ),
            ],
            guild_id=guild_id,
        )
    await store.record_rocket_league_events(
        [
            RocketLeagueEvent(
                "leaderboard_update", 0, "", "", 11, 100, notification_text="Standings"
            )
        ],
        guild_id=1,
    )
    pending = await store.list_pending_rocket_league_event_notifications()
    assert len(pending) == 8  # One movement replaced by one summary; all other routes survive.
    assert sum(event.event_type == "leaderboard_update" for event, _, _ in pending) == 1
    assert sum(channel_id == 92 for _, channel_id, _ in pending) == 4
    assert sum(event.event_type == "streak_leaderboard_movement" for event, _, _ in pending) == 2
    await store.prune_rocket_league_events(before=201)
    assert await store.list_pending_rocket_league_event_notifications() == []
