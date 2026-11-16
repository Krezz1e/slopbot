from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from slopbot.bot import SlopBot
from slopbot.database import SubscriptionStore
from slopbot.engagement import DeliveryOutcome
from slopbot.models import TournamentChannelAlert, TournamentOptInDelivery, TournamentUserAlert
from slopbot.rocketleague import RocketLeagueTournament
from slopbot.tournaments import (
    REGION_CHOICES,
    CachedSchedule,
    TournamentAlertConfigView,
    TournamentAlertsCog,
    TournamentLeadTimeModal,
    TournamentReminderButton,
    schedule_embed,
    tournament_alert_embed,
    tournament_game_mode,
)


def tournament(
    *, tournament_id: int, start_time: int, team_size: int, title: str | None = None
) -> RocketLeagueTournament:
    return RocketLeagueTournament(
        id=tournament_id,
        title=title or f"{team_size}v{team_size} Soccar Test Cup",
        start_time=start_time,
        team_size=team_size,
        game_mode=0,
        max_bracket_size=32,
        rank_min=0,
        rank_max=22,
        region="USC",
        schedule_id=99,
    )


def test_tournaments_command_offers_response_visibility() -> None:
    parameters = {
        parameter.name: parameter for parameter in TournamentAlertsCog.tournaments.parameters
    }

    assert [choice.value for choice in parameters["visibility"].choices] == [
        "public",
        "private",
    ]


async def test_tournament_lead_time_accepts_custom_minutes() -> None:
    panel = TournamentAlertConfigView(
        owner_id=1,
        guild_id=2,
        store=cast(Any, SimpleNamespace()),
        current=None,
    )
    modal = TournamentLeadTimeModal(panel)
    cast(Any, modal.lead_input)._value = "47"
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

    await modal.on_submit(cast(Any, interaction))

    assert panel.lead_minutes == 47
    assert panel.lead_button.label == "Lead time: 47 min"
    interaction.response.edit_message.assert_awaited_once()


async def test_tournament_lead_time_rejects_out_of_range_values() -> None:
    panel = TournamentAlertConfigView(
        owner_id=1,
        guild_id=2,
        store=cast(Any, SimpleNamespace()),
        current=None,
    )
    modal = TournamentLeadTimeModal(panel)
    cast(Any, modal.lead_input)._value = "121"
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await modal.on_submit(cast(Any, interaction))

    assert panel.lead_minutes == 15
    interaction.response.send_message.assert_awaited_once_with(
        "Enter a whole number from 5 to 120 minutes.", ephemeral=True
    )


async def test_channel_config_accepts_separate_button_reminder_lead() -> None:
    panel = TournamentAlertConfigView(
        owner_id=1,
        guild_id=2,
        store=cast(Any, SimpleNamespace()),
        current=None,
        channel=cast(Any, SimpleNamespace(mention="#tournaments")),
    )
    modal = TournamentLeadTimeModal(panel)
    cast(Any, modal.lead_input)._value = "60"
    assert modal.opt_in_lead_input is not None
    cast(Any, modal.opt_in_lead_input)._value = "20"
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

    await modal.on_submit(cast(Any, interaction))

    assert panel.lead_minutes == 60
    assert panel.opt_in_lead_minutes == 20


async def test_due_tournaments_honor_window_filter_and_deduplicate(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    cog = TournamentAlertsCog(
        cast(SlopBot, SimpleNamespace()),
        store,
        None,
        None,
    )
    now = 1_800_000_000
    candidates = (
        tournament(tournament_id=1, start_time=now + 600, team_size=2),
        tournament(tournament_id=2, start_time=now + 600, team_size=3),
        tournament(tournament_id=3, start_time=now + 1800, team_size=2),
        tournament(tournament_id=4, start_time=now - 1, team_size=2),
    )

    claimed = await cog._claim_due(
        target_type="user",
        target_id=2,
        guild_id=1,
        lead_minutes=15,
        team_sizes=(2,),
        game_modes=("soccar",),
        tournaments=candidates,
        now=now,
    )
    duplicate = await cog._claim_due(
        target_type="user",
        target_id=2,
        guild_id=1,
        lead_minutes=15,
        team_sizes=(2,),
        game_modes=("soccar",),
        tournaments=candidates,
        now=now,
    )

    assert [item.id for item in claimed] == [1]
    assert duplicate == []


async def test_due_tournaments_filter_multiple_sizes_and_modes(tmp_path: Path) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    cog = TournamentAlertsCog(cast(SlopBot, SimpleNamespace()), store, None, None)
    now = 1_800_000_000
    candidates = (
        tournament(
            tournament_id=1,
            start_time=now + 600,
            team_size=2,
            title="2v2 Hoops",
        ),
        tournament(
            tournament_id=2,
            start_time=now + 600,
            team_size=3,
            title="3v3 Pentathlon",
        ),
        tournament(
            tournament_id=3,
            start_time=now + 600,
            team_size=1,
            title="1v1 Hoops",
        ),
    )

    claimed = await cog._claim_due(
        target_type="channel",
        target_id=3,
        guild_id=1,
        lead_minutes=15,
        team_sizes=(2, 3),
        game_modes=("hoops", "pentathlon"),
        tournaments=candidates,
        now=now,
    )

    assert [item.id for item in claimed] == [1, 2]


def test_tournament_game_mode_classifies_current_schedule_modes() -> None:
    cases = {
        "3v3 Soccar": "soccar",
        "2v2 Rumble": "rumble",
        "2v2 Hoops": "hoops",
        "3v3 Dropshot": "dropshot",
        "3v3 Snow Day": "snow_day",
        "2v2 Heatseeker": "heatseeker",
        "2v2 Pentathlon": "pentathlon",
    }
    for title, expected in cases.items():
        item = tournament(tournament_id=1, start_time=100, team_size=2, title=title)
        assert tournament_game_mode(item) == expected


def test_region_choices_include_schedule_regions_seen_in_api() -> None:
    values = {choice.value for choice in REGION_CHOICES}
    assert {"USE", "USC", "USW", "IND"} <= values


async def test_personal_alert_retries_when_shared_delivery_is_transient(
    tmp_path: Path, monkeypatch: Any
) -> None:
    member = SimpleNamespace(id=2)
    guild = SimpleNamespace(get_member=lambda _user_id: member)
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    send = AsyncMock(return_value=DeliveryOutcome(False, True, "Telegram delivery failed"))
    monkeypatch.setattr("slopbot.tournaments.send_to_destinations", send)
    cog = TournamentAlertsCog(cast(SlopBot, bot), store, None, None)
    alert = TournamentUserAlert(1, 2, "USE", 15, (2,), ("soccar",))
    item = tournament(tournament_id=1, start_time=1_800_000_600, team_size=2)

    complete = await cog._send_user_alert(alert, "USE", [item])

    assert complete is False
    send.assert_awaited_once()


async def test_personal_alert_finishes_permanent_no_destination(
    tmp_path: Path, monkeypatch: Any
) -> None:
    member = SimpleNamespace(id=2)
    guild = SimpleNamespace(get_member=lambda _user_id: member)
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    send = AsyncMock(return_value=DeliveryOutcome(False, False, "Discord DMs are disabled"))
    monkeypatch.setattr("slopbot.tournaments.send_to_destinations", send)
    cog = TournamentAlertsCog(cast(SlopBot, bot), store, None, None)
    alert = TournamentUserAlert(1, 2, "USE", 15, (2,), ("soccar",))
    item = tournament(tournament_id=1, start_time=1_800_000_600, team_size=2)

    complete = await cog._send_user_alert(alert, "USE", [item])

    assert complete is True


def test_schedule_and_alert_embeds_use_discord_local_timestamps() -> None:
    item = tournament(tournament_id=1, start_time=1_800_000_600, team_size=2)

    schedule = schedule_embed([item], resolved_region="USE", total_upcoming=1)
    alert = tournament_alert_embed([item], resolved_region="USE", lead_minutes=15)

    assert "<t:1800000600:F>" in (schedule.description or "")
    assert "<t:1800000600:R>" in (alert.description or "")
    assert alert.footer.text == "Schedule region: USE"


async def test_schedule_cache_refreshes_inside_minimum_alert_window() -> None:
    first = tournament(tournament_id=1, start_time=2_000, team_size=2)
    second = tournament(tournament_id=2, start_time=2_100, team_size=2)
    service = SimpleNamespace(
        tournaments=AsyncMock(side_effect=[("USE", [first]), ("USE", [first, second])])
    )
    cog = TournamentAlertsCog(
        cast(SlopBot, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, service),
        None,
    )

    assert (await cog._get_schedule("USE", now=100)).tournaments == (first,)
    assert (await cog._get_schedule("USE", now=159)).tournaments == (first,)
    assert (await cog._get_schedule("USE", now=160)).tournaments == (first, second)
    assert service.tournaments.await_count == 2


async def test_channel_alert_fetches_uncached_channel_and_persists_button_offer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 3
    channel.send = AsyncMock()
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(),
        fetch_channel=AsyncMock(return_value=channel),
    )
    channel.guild = guild
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild, get_channel=lambda _channel_id: None)
    monkeypatch.setattr("slopbot.tournaments.can_post_tournament_alert", lambda *_args: True)
    cog = TournamentAlertsCog(cast(SlopBot, bot), store, None, None)
    alert = TournamentChannelAlert(1, 3, 4, "USE", 15, (2,), ("soccar",), 15)
    item = tournament(tournament_id=42, start_time=2_000, team_size=2)

    complete = await cog._send_channel_alert(alert, "USE", [item])

    assert complete is True
    guild.fetch_channel.assert_awaited_once_with(3)
    sent_view = channel.send.await_args.kwargs["view"]
    assert isinstance(sent_view.children[0], TournamentReminderButton)
    token = sent_view.children[0].token
    assert (
        await store.subscribe_tournament_opt_in(token=token, guild_id=1, user_id=9, now=100)
        == "created"
    )


async def test_channel_alert_process_sends_once_and_finishes_delivery_claim(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 3
    channel.send = AsyncMock()
    guild = SimpleNamespace(id=1, me=SimpleNamespace())
    channel.guild = guild
    bot = SimpleNamespace(
        get_guild=lambda _guild_id: guild,
        get_channel=lambda _channel_id: channel,
    )
    monkeypatch.setattr("slopbot.tournaments.can_post_tournament_alert", lambda *_args: True)
    cog = TournamentAlertsCog(cast(SlopBot, bot), store, None, None)
    alert = TournamentChannelAlert(1, 3, 4, "USE", 15, (2,), ("soccar",), 15)
    now = 1_800_000_000
    schedule = CachedSchedule(
        now,
        "USE",
        (tournament(tournament_id=42, start_time=now + 600, team_size=2),),
    )

    await cog._process_channel_alert(alert, schedule, now=now)
    await cog._process_channel_alert(alert, schedule, now=now + 1)

    channel.send.assert_awaited_once()


async def test_one_time_button_delivery_uses_notification_routing_and_finishes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    member = SimpleNamespace(id=9)
    guild = SimpleNamespace(get_member=lambda _user_id: member)
    bot = SimpleNamespace(get_guild=lambda _guild_id: guild)
    send = AsyncMock(return_value=DeliveryOutcome(True, False, "Discord DM sent"))
    monkeypatch.setattr("slopbot.tournaments.send_to_destinations", send)
    cog = TournamentAlertsCog(cast(SlopBot, bot), store, None, None)
    delivery = TournamentOptInDelivery("abc123", 1, 9, 42, "2v2 Soccar Cup", 2_000, "USE", 15, 0)
    await store.save_tournament_opt_in_offer(
        token=delivery.token,
        guild_id=delivery.guild_id,
        channel_id=3,
        tournament_id=delivery.tournament_id,
        tournament_title=delivery.tournament_title,
        start_time=delivery.start_time,
        region=delivery.region,
        lead_minutes=delivery.lead_minutes,
        now=100,
    )
    await store.subscribe_tournament_opt_in(
        token=delivery.token, guild_id=delivery.guild_id, user_id=delivery.user_id, now=100
    )

    await cog._process_opt_in_delivery(delivery, now=1_100)

    send.assert_awaited_once()
    assert await store.list_due_tournament_opt_ins(now=1_100) == []


async def test_persistent_reminder_button_creates_only_one_subscription(
    tmp_path: Path,
) -> None:
    store = SubscriptionStore(tmp_path / "slopbot.db")
    await store.initialize()
    await store.save_tournament_opt_in_offer(
        token="abcdef012345abcdef012345",
        guild_id=1,
        channel_id=3,
        tournament_id=42,
        tournament_title="2v2 Soccar Cup",
        start_time=2_000_000_000,
        region="USE",
        lead_minutes=15,
        now=100,
    )
    cog = TournamentAlertsCog(cast(SlopBot, SimpleNamespace()), store, None, None)
    bot = MagicMock(spec=commands.Bot)
    bot.get_cog.return_value = cog
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=9),
        client=bot,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    button = await TournamentReminderButton.from_custom_id(
        cast(Any, interaction),
        cast(Any, None),
        {"token": "abcdef012345abcdef012345"},
    )

    await button.callback(cast(Any, interaction))
    await button.callback(cast(Any, interaction))

    messages = [call.args[0] for call in interaction.followup.send.await_args_list]
    assert "one-time reminder" in messages[0]
    assert "already requested" in messages[1]
