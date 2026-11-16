from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord

from slopbot.models import (
    CommandPolicy,
    CompletedRocketLeagueStreak,
    ResponseVisibility,
    RocketLeagueAccount,
    RocketLeagueEvent,
    RocketLeaguePeakMMR,
    RocketLeagueRecord,
    RocketLeagueTrackingConfig,
    TournamentChannelAlert,
)
from slopbot.rank_estimates import RankStep, RankStepEstimates
from slopbot.rocketleague import (
    GAME_MODES,
    PLAYER_ACCOUNT_ID_PATTERN,
    STANDARD_RANK_MODES,
    RocketLeagueActivity,
    RocketLeagueAuthorization,
    RocketLeagueClub,
    RocketLeagueClubBadge,
    RocketLeagueClubMember,
    RocketLeagueCog,
    RocketLeaguePlayerDetails,
    RocketLeaguePlaylist,
    RocketLeagueSeasonRewards,
    RocketLeagueService,
    RocketLeagueSkill,
    RocketLeagueStatus,
    SlopbotStatusSnapshot,
    StatusCog,
    all_time_streaks_embed,
    background_worker_status,
    cloudflare_tunnel_connections,
    club_details_embed,
    leaderboard_window,
    match_history_value,
    peak_rank_response,
    player_overview_embed,
    rank_response,
    ranked_skill_entries,
    rocket_league_event_line,
    rocket_league_status_detail,
    server_configuration_embed,
)


def test_rocket_league_status_explains_expired_authorization() -> None:
    assert (
        rocket_league_status_detail(RocketLeagueStatus(False, "authorization_required"))
        == "Reauthorization required; use /reauthorizerocketleague"
    )
    assert (
        rocket_league_status_detail(RocketLeagueStatus(False, "reconnecting"))
        == "Unavailable; check service logs"
    )


async def test_service_client_starts_device_authorization() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "verification_uri": "https://www.epicgames.com/activate",
            "user_code": "ABCDEFGH",
            "expires_at": 1_800_000_000,
        }
    )

    authorization = await service.start_authorization()

    assert authorization == RocketLeagueAuthorization(
        "https://www.epicgames.com/activate", "ABCDEFGH", 1_800_000_000
    )
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with("POST", "/v1/authorization")


def test_slopbot_status_snapshot_distinguishes_issue_and_recovery_alerts() -> None:
    issue = SlopbotStatusSnapshot(("❌ **Database:** Unavailable",), False, 100)
    recovery = SlopbotStatusSnapshot(("✅ **Database:** Operational",), True, 200)

    assert issue.embed(alert=True).title == "Slopbot issue detected"
    assert recovery.embed(alert=True).title == "Slopbot recovered"
    assert issue.fingerprint != recovery.fingerprint


def test_slopbot_status_fingerprint_ignores_worker_timestamp_changes() -> None:
    earlier = SlopbotStatusSnapshot(
        ("❌ **Worker:** Last successful run <t:100:R> (stale)",), False, 200
    )
    later = SlopbotStatusSnapshot(
        ("❌ **Worker:** Last successful run <t:150:R> (stale)",), False, 250
    )

    assert earlier.fingerprint == later.fingerprint


def test_background_worker_status_detects_stale_and_starting_workers() -> None:
    assert background_worker_status(
        running=True,
        started_at=990,
        last_success_at=None,
        now=1000,
        stale_after=60,
        active_detail="Polling",
    ) == (True, "Starting; awaiting first successful run")

    healthy, healthy_detail = background_worker_status(
        running=True,
        started_at=800,
        last_success_at=990,
        now=1000,
        stale_after=60,
        active_detail="Polling",
    )
    stale, stale_detail = background_worker_status(
        running=True,
        started_at=800,
        last_success_at=900,
        now=1000,
        stale_after=60,
        active_detail="Polling",
    )
    never_succeeded, never_succeeded_detail = background_worker_status(
        running=True,
        started_at=900,
        last_success_at=None,
        now=1000,
        stale_after=60,
        active_detail="Polling",
    )

    assert healthy and "<t:990:R>" in healthy_detail
    assert not stale and "stale" in stale_detail
    assert not never_succeeded and "since startup" in never_succeeded_detail


def test_cloudflare_tunnel_connections_reads_prometheus_gauge() -> None:
    assert cloudflare_tunnel_connections("cloudflared_tunnel_ha_connections 4\n") == 4
    assert (
        cloudflare_tunnel_connections(
            'cloudflared_tunnel_ha_connections{connector="one"} 2\n'
            'cloudflared_tunnel_ha_connections{connector="two"} 1\n'
        )
        == 3
    )
    assert cloudflare_tunnel_connections("unrelated_metric 1\n") is None


async def test_public_web_health_checks_the_user_facing_origin() -> None:
    response = SimpleNamespace(status=200, json=AsyncMock(return_value={"web": "ok"}))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    http = SimpleNamespace(get=MagicMock(return_value=context))
    bot = SimpleNamespace(
        web=SimpleNamespace(http=http, settings=SimpleNamespace(origin="https://slopbot.example"))
    )

    assert await StatusCog(bot)._public_web_status() == (
        True,
        "Operational through the public tunnel",
    )
    assert http.get.call_args.args[0] == "https://slopbot.example/healthz"

    response.status = 502
    assert await StatusCog(bot)._public_web_status() == (
        False,
        "Public endpoint returned HTTP 502",
    )


async def test_public_web_health_is_omitted_when_web_is_not_configured() -> None:
    assert await StatusCog(SimpleNamespace(web=None))._public_web_status() is None


async def test_service_client_parses_skill_response() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "skills": [
                {
                    "player_id": "Epic|abc|0",
                    "mmr": 1234.5,
                    "tier": 14,
                    "division": 2,
                    "rank": "Diamond II, Division III",
                }
            ]
        }
    )

    skills = await service.skills(playlist=11, player_ids=["Epic|abc|0"])

    assert len(skills) == 1
    assert skills[0].mmr == 1234.5
    assert skills[0].rank == "Diamond II, Division III"
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with(
        "POST",
        "/v1/skills",
        {"playlist": 11, "player_ids": ["Epic|abc|0"]},
    )


async def test_service_client_parses_player_details() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "skills": [
                {
                    "player_id": "Epic|abc|0",
                    "playlist": 11,
                    "mmr": 1234.5,
                    "tier": 14,
                    "division": 2,
                    "rank": "Diamond II, Division III",
                    "win_streak": 3,
                    "matches_played": 81,
                    "placement_matches_played": 10,
                }
            ],
            "rewards": {"level": 6, "wins": 4},
            "club": {
                "id": 7,
                "name": "Sloppers",
                "tag": "SLOP",
                "primary_color": 255,
                "accent_color": 65280,
                "title": "Champions",
                "owner_player_id": "Epic|abc|0",
                "verified": True,
                "members": [
                    {
                        "player_id": "Epic|abc|0",
                        "player_name": "Player",
                        "role_id": 1,
                    }
                ],
                "badges": [{"stat": "Goal", "badge": 2}],
            },
        }
    )

    details = await service.player_details("Epic|abc|0")

    assert details.skills[0].playlist == 11
    assert details.skills[0].placement_matches_played == 10
    assert details.rewards == RocketLeagueSeasonRewards(6, 4)
    assert details.club is not None and details.club.tag == "SLOP"
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with("POST", "/v1/player-details", {"player_id": "Epic|abc|0"})


async def test_service_client_parses_tournament_schedule() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "region": "USE",
            "tournaments": [
                {
                    "id": 42,
                    "title": "2v2 Soccar",
                    "start_time": 1_800_000_000,
                    "team_size": 2,
                    "game_mode": 0,
                    "max_bracket_size": 32,
                    "rank_min": 0,
                    "rank_max": 22,
                    "region": "USC",
                    "schedule_id": 99,
                }
            ],
        }
    )

    region, tournaments = await service.tournaments(region="USE")

    assert region == "USE"
    assert len(tournaments) == 1
    assert tournaments[0].id == 42
    assert tournaments[0].team_size == 2
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with("GET", "/v1/tournaments?region=USE")


async def test_service_client_parses_active_ranked_playlists() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "ranked_playlists": [
                {"id": 99, "node_id": "RankedLimitedTime"},
                {"id": 11, "node_id": "RankedDoubles"},
                {"id": 99, "node_id": "Duplicate"},
            ],
            "fallback": False,
        }
    )

    playlists = await service.active_playlists()

    assert playlists == (
        RocketLeaguePlaylist(11, "RankedDoubles", "Ranked Doubles (2v2)"),
        RocketLeaguePlaylist(99, "RankedLimitedTime", "Ranked Limited Time (99)"),
    )


def test_leaderboard_appends_calling_member_outside_requested_size() -> None:
    skill = RocketLeagueSkill("Epic|abc|0", 1000, 10, 0, "Platinum I, Division I")
    ranked = [
        (
            position,
            SimpleNamespace(discord_user_id=member_id),
            cast(discord.Member, SimpleNamespace(id=member_id)),
            skill,
        )
        for position, member_id in enumerate([10, 20, 30, 40, 50, 60], 1)
    ]

    visible, appended_position = leaderboard_window(cast(Any, ranked), size=3, caller_id=60)

    assert [entry[0] for entry in visible] == [1, 2, 3, 6]
    assert appended_position == 6


def test_current_rank_positions_share_leaderboard_ordering() -> None:
    accounts = [
        RocketLeagueAccount(1, 10, "Epic|bravo|0", "Bravo"),
        RocketLeagueAccount(1, 20, "Epic|alpha|0", "Alpha"),
        RocketLeagueAccount(1, -1, "Epic|guest|0", "Guest", True),
    ]
    members = {
        10: cast(discord.Member, SimpleNamespace(id=10, display_name="Zulu")),
        20: cast(discord.Member, SimpleNamespace(id=20, display_name="Alpha")),
    }
    fetched = [
        RocketLeagueSkill("Epic|bravo|0", 1000, 1, 0, "Bronze I, Division I"),
        RocketLeagueSkill("Epic|alpha|0", 1000, 1, 0, "Bronze I, Division I"),
        RocketLeagueSkill("Epic|guest|0", 900, 1, 0, "Bronze I, Division I"),
    ]

    ranked = ranked_skill_entries(accounts, members, fetched)

    assert [(position, account.player_id) for position, account, _member, _skill in ranked] == [
        (1, "Epic|alpha|0"),
        (2, "Epic|bravo|0"),
        (3, "Epic|guest|0"),
    ]


def test_match_history_value_reads_oldest_to_newest() -> None:
    assert match_history_value(["W", "L", "W"]) == "W L W"


def test_match_history_value_keeps_newest_hundred_observations() -> None:
    markers = [f"W{index}" for index in range(105)]

    value = match_history_value(markers)

    assert value.startswith("… 5 older observations • W5 W6")
    assert value.endswith("W104")
    assert "W4 " not in value


def test_today_has_only_bounded_event_count_parameter() -> None:
    parameters = {parameter.name: parameter for parameter in RocketLeagueCog.today.parameters}

    assert set(parameters) == {"events"}
    assert parameters["events"].required is False
    assert parameters["events"].default == 10
    assert parameters["events"].min_value == 1
    assert parameters["events"].max_value == 100


def test_sessions_supports_member_history_and_server_overview() -> None:
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.play_sessions.parameters
    }

    assert set(parameters) == {"account", "all_members"}
    assert parameters["account"].required is False
    assert parameters["account"].autocomplete is not None
    assert parameters["all_members"].required is False
    assert parameters["all_members"].default is False


def test_rocket_league_event_line_formats_unlinked_rank_up_safely() -> None:
    event = RocketLeagueEvent(
        "rank_up",
        -1,
        "Epic|player|0",
        "@everyone",
        11,
        1_800_000_000,
        rank_before="Gold III, Division IV",
        rank_after="Platinum I, Division I",
    )

    line = rocket_league_event_line(event)

    assert "@everyone" not in line
    assert "ranked up" in line
    assert "Gold III" in line
    assert "Platinum I" in line


def test_rocket_league_event_line_separates_session_continuations_and_record_entries() -> None:
    continuation = RocketLeagueEvent(
        "session_streak_completed",
        2,
        "Epic|player|0",
        "Krezzle",
        0,
        1_800_000_000,
        result="win",
        count=1,
        scope="main",
        total_count=4,
    )
    movement = RocketLeagueEvent(
        "streak_leaderboard_movement",
        2,
        "Epic|player|0",
        "Krezzle",
        0,
        1_800_000_010,
        result="win",
        count=8,
        scope="all",
        position_after=2,
    )

    continuation_line = rocket_league_event_line(continuation)
    movement_line = rocket_league_event_line(movement)

    assert "1 more" in continuation_line
    assert "4 total" in continuation_line
    assert "all-ranked-mode" in movement_line
    assert "#2" in movement_line


def test_server_configuration_summary_includes_all_policy_types() -> None:
    embed = server_configuration_embed(
        guild_name="Test server",
        tracking=RocketLeagueTrackingConfig(1, 10, 4, 1200, 11, (11, 99)),
        event_notifications=[
            ("session_streak", 10, 4),
            ("leaderboard_movement", 11, 1),
        ],
        tournament_alerts=[
            TournamentChannelAlert(1, 12, 3, "USE", 17, (2, 3), ("soccar", "rumble"))
        ],
        policies=[CommandPolicy("rank", ResponseVisibility.PRIVATE, True, (13,))],
    )

    assert embed.title == "Slopbot server configuration"
    fields = {field.name: field.value for field in embed.fields}
    assert "<#10>" in fields["Rocket League event notifications"]
    assert "20 minutes" in fields["Session streak calculation"]
    assert "<#12>" in fields["Tournament channel reminders"]
    assert "Only me" in fields["Result-command policies"]
    assert "blocked everywhere" not in fields["Result-command policies"]


def test_server_configuration_limits_status_channels_to_embed_field_size() -> None:
    embed = server_configuration_embed(
        guild_name="Test server",
        tracking=RocketLeagueTrackingConfig(1, None, 3, 900, None, (11,)),
        tournament_alerts=[],
        policies=[],
        status_channel_ids=tuple(range(10**17, 10**17 + 100)),
    )

    status_value = next(
        field.value for field in embed.fields if field.name == "Slopbot status alerts"
    )
    assert len(status_value) <= 1024
    assert "more" in status_value


def test_rank_commands_default_to_standard_modes_and_share_account_controls() -> None:
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.rank_current.parameters
    }
    peak_parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.rank_peak.parameters
    }
    skill = RocketLeagueSkill("Epic|abc|0", 1234.6, 14, 2, "Diamond II, Division III")

    response = rank_response("Player_One", "Ranked Doubles (2v2)", skill)

    assert set(parameters) == {"account", "playlist", "visibility"}
    assert parameters["account"].required is False
    assert parameters["account"].autocomplete is not None
    assert parameters["playlist"].required is False
    assert [choice.value for choice in parameters["visibility"].choices] == [
        "public",
        "private",
    ]
    assert [choice.value for choice in parameters["playlist"].choices] == [
        "all",
        "allall",
        *GAME_MODES,
    ]
    assert STANDARD_RANK_MODES == ("1v1", "2v2", "3v3")
    assert set(peak_parameters) == {"account", "playlist", "visibility"}
    assert peak_parameters["account"].required is False
    assert peak_parameters["account"].autocomplete is not None
    assert peak_parameters["playlist"].required is False
    assert [choice.value for choice in peak_parameters["playlist"].choices] == [
        "all",
        "allall",
        *GAME_MODES,
    ]
    assert response == (
        "**Player\\_One — Ranked Doubles (2v2)**\n**1,235 MMR** — **Diamond II, Division III**"
    )
    assert peak_rank_response(
        "Player_One", "Ranked Doubles (2v2)", 1300.2, "Champion I, Division I", 1000
    ) == (
        "**Player\\_One — Ranked Doubles (2v2) peak**\n"
        "**1,300 MMR** — **Champion I, Division I**\nObserved <t:1000:D>"
    )
    assert [command.name for command in RocketLeagueCog.rank_group.commands] == ["current", "peak"]


def test_account_ids_reject_sql_metacharacters_and_limit_length() -> None:
    assert PLAYER_ACCOUNT_ID_PATTERN.fullmatch("a" * 128)
    assert not PLAYER_ACCOUNT_ID_PATTERN.fullmatch("a" * 129)
    assert not PLAYER_ACCOUNT_ID_PATTERN.fullmatch("DROP TABLES;")


def test_rank_response_neutralizes_mentions() -> None:
    skill = RocketLeagueSkill("Epic|abc|0", 1000, 10, 0, "@here")

    response = rank_response("@everyone", "Ranked Doubles (2v2)", skill)

    assert "@everyone" not in response
    assert "@here" not in response


def test_rank_response_includes_the_shared_estimated_outlook() -> None:
    skill = RocketLeagueSkill("Epic|abc|0", 1000, 14, 1, "Diamond II, Division II", playlist=11)
    outlook = RankStepEstimates(
        playlist=11,
        current_mmr=1000,
        current_rank=skill.rank,
        observed_at=100,
        steps=(
            RankStep("win", "division", "Diamond II, Division III", 2, 1020, 4, 8, "playlist"),
            RankStep("loss", "rank", "Diamond I, Division IV", 6, None, 5, 0, "playlist", "usual"),
        ),
        note="test",
    )

    response = rank_response("Player", "Ranked Doubles (2v2)", skill, outlook)

    assert "~2 wins to division up" in response
    assert "~6 losses to rank down" in response


def test_match_history_uses_one_autocomplete_account_selector() -> None:
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.match_history.parameters
    }

    assert set(parameters) == {"days", "account", "visibility"}
    assert parameters["account"].required is False
    assert parameters["account"].autocomplete is not None


def test_leaderboard_offers_response_visibility() -> None:
    parameters = {parameter.name: parameter for parameter in RocketLeagueCog.leaderboard.parameters}

    assert [choice.value for choice in parameters["visibility"].choices] == [
        "public",
        "private",
    ]


async def test_tracking_finishes_inactive_streaks_without_active_accounts() -> None:
    bot: Any = SimpleNamespace(get_guild=lambda _guild_id: None)
    store = SimpleNamespace(
        list_rocket_league_tracking_configs=AsyncMock(
            return_value=[RocketLeagueTrackingConfig(1, 99, 3, 900, None, (11,))]
        ),
        finish_inactive_rocket_league_streaks=AsyncMock(
            return_value=[
                CompletedRocketLeagueStreak(1, 2, "Player", "main", "win", 3, "inactivity")
            ]
        ),
        list_linked_rocket_league_accounts=AsyncMock(return_value=[]),
        prune_rocket_league_match_history=AsyncMock(return_value=0),
        prune_rocket_league_events=AsyncMock(return_value=0),
        prune_rocket_league_mmr_history=AsyncMock(return_value=0),
        list_pending_rocket_league_event_notifications=AsyncMock(return_value=[]),
    )
    cog = RocketLeagueCog(bot, store, SimpleNamespace())
    cog._deliver_session_summaries = AsyncMock()
    await cog.track_rocket_league_activity()

    store.finish_inactive_rocket_league_streaks.assert_awaited_once()
    store.list_pending_rocket_league_event_notifications.assert_awaited_once()


async def test_service_client_parses_activity_response() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "activity": [
                {
                    "player_id": "Epic|abc|0",
                    "playlist": 11,
                    "mmr": 1234.5,
                    "tier": 14,
                    "division": 2,
                    "rank": "Diamond II, Division III",
                    "win_streak": -3,
                    "matches_played": 81,
                }
            ]
        }
    )

    activity = await service.activity(["Epic|abc|0"])

    assert activity == [
        RocketLeagueActivity("Epic|abc|0", 11, 1234.5, 14, 2, "Diamond II, Division III", -3, 81)
    ]
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with("POST", "/v1/activity", {"player_ids": ["Epic|abc|0"]})


async def test_service_client_parses_shop_response() -> None:
    service = RocketLeagueService(base_url="http://127.0.0.1:8765", token="secret")
    service._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "items": [
                {
                    "key": "1:2",
                    "shop_id": 1,
                    "shop_name": "Featured",
                    "shop_type": "Featured",
                    "shop_item_id": 2,
                    "title": "Octane Decal",
                    "description": "Animated",
                    "image_url": "https://example.com/item.png",
                    "start_date": 100,
                    "end_date": 200,
                    "product_ids": [42],
                    "attributes": [{"key": "Painted", "value": "Crimson"}],
                    "costs": [
                        {
                            "cost_id": 3,
                            "prices": [{"currency_id": 1, "amount": 700}],
                        }
                    ],
                }
            ]
        }
    )

    items = await service.shop()

    assert len(items) == 1
    assert items[0].title == "Octane Decal"
    assert items[0].attributes == (("Painted", "Crimson"),)
    assert items[0].prices[0].amount == 700
    cast_mock: Any = service._request
    cast_mock.assert_awaited_once_with("GET", "/v1/shop")


def test_setup_command_contains_nested_notification_groups() -> None:
    groups = {command.name: command for command in RocketLeagueCog.setup_group.commands}

    assert set(groups) == {"events", "commands", "sessions"}
    assert [command.name for command in groups["events"].commands] == ["notifications"]
    assert {command.name for command in groups["commands"].commands} == {
        "visibility",
        "allow",
        "disallow",
        "unrestricted",
        "status",
    }
    event_parameters = {
        parameter.name: parameter for parameter in groups["events"].commands[0].parameters
    }
    assert set(event_parameters) == {
        "event",
        "channel",
        "minimum",
        "inactivity_minutes",
        "enabled",
    }
    assert event_parameters["inactivity_minutes"].min_value == 5
    assert event_parameters["inactivity_minutes"].max_value == 1440
    assert [choice.value for choice in event_parameters["event"].choices] == [
        "match",
        "rank_change",
        "peak_mmr",
        "session_streak",
        "all_time_streak",
        "leaderboard_movement",
        "streak_leaderboard_competitive",
        "streak_leaderboard_all_ranked",
    ]


def test_streak_leaderboard_defaults_to_three_records_and_supports_expansion() -> None:
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.streak_leaderboard.parameters
    }

    assert parameters["result"].required is False
    assert [choice.value for choice in parameters["result"].choices] == ["win", "loss"]
    assert parameters["scope"].required is False
    assert [choice.value for choice in parameters["scope"].choices] == ["main", "all"]
    assert parameters["size"].required is False
    assert parameters["size"].min_value == 1
    assert parameters["size"].max_value == 20


def test_peak_leaderboard_defaults_to_three_players_and_supports_expansion() -> None:
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.peak_leaderboard.parameters
    }

    assert parameters["gamemode"].required is False
    assert parameters["size"].required is False
    assert parameters["size"].min_value == 1
    assert parameters["size"].max_value == 20


def test_player_overview_keeps_playlist_streaks_and_uses_separate_all_time_panel() -> None:
    skill = RocketLeagueSkill("Epic|abc|0", 1200, 10, 0, "Platinum I, Division I", 3, 81, 10, 11)
    loss_skill = RocketLeagueSkill(
        "Epic|abc|0", 900, 12, 0, "Platinum III, Division I", -2, 35, 10, 13
    )
    peak = RocketLeaguePeakMMR(
        1, 2, "Epic|abc|0", "Player", 11, 1300, "Diamond I, Division I", 1000
    )
    records = [
        RocketLeagueRecord(1, 1, 2, "Epic|abc|0", "Player", "main", "win", 5, 1000),
        RocketLeagueRecord(2, 1, 2, "Epic|abc|0", "Player", "main", "loss", 3, 1000),
        RocketLeagueRecord(3, 1, 2, "Epic|abc|0", "Player", "all", "win", 4, 1000),
        RocketLeagueRecord(4, 1, 2, "Epic|abc|0", "Player", "all", "loss", 2, 1000),
    ]

    embed = player_overview_embed(
        "Player",
        [
            ("Ranked Doubles (2v2)", skill, 2),
            ("Ranked Standard (3v3)", loss_skill, 5),
        ],
        [("Ranked Doubles (2v2)", peak, 1)],
        RocketLeaguePlayerDetails((), RocketLeagueSeasonRewards(6, 4), None),
    )
    streak_embed = all_time_streaks_embed(
        "Player",
        [(record, position) for record, position in zip(records, (3, 4, 2, 1), strict=True)],
    )
    fields = {field.name: field.value for field in embed.fields}
    streak_fields = {field.name: field.value for field in streak_embed.fields}

    assert "1,200 MMR" in fields["Current competitive ranks"]
    assert "server #2" in fields["Current competitive ranks"]
    assert "81 matches" in fields["Current competitive ranks"]
    assert "3-game win streak" in fields["Current competitive ranks"]
    assert "2-game loss streak" in fields["Current competitive ranks"]
    assert "-2-game win streak" not in fields["Current competitive ranks"]
    assert "placements complete" in fields["Current competitive ranks"]
    assert "4/10" in fields["Season rewards"]
    assert fields["Club"] == "Not currently in a club"
    assert "1,300 MMR" in fields["Peak competitive ranks"]
    assert "server #1" in fields["Peak competitive ranks"]
    assert "Competitive streak records" not in fields
    assert "Wins: **5 games**" in streak_fields["Competitive"]
    assert "server #3" in streak_fields["Competitive"]
    assert "Losses: **2 games**" in streak_fields["All ranked modes"]
    assert "server #1" in streak_fields["All ranked modes"]
    parameters = {
        parameter.name: parameter for parameter in RocketLeagueCog.player_overview.parameters
    }
    assert parameters["account"].autocomplete is not None
    assert [choice.value for choice in parameters["visibility"].choices] == [
        "public",
        "private",
    ]


def test_club_details_embed_contains_roster_colors_and_badges() -> None:
    club = RocketLeagueClub(
        7,
        "Sloppers",
        "SLOP",
        255,
        65280,
        "Champions",
        "Epic|abc|0",
        True,
        (RocketLeagueClubMember("Epic|abc|0", "Player", 1),),
        (RocketLeagueClubBadge("Goal", 2),),
    )

    embed = club_details_embed("Player", club)
    fields = {field.name: field.value for field in embed.fields}

    assert embed.title == "[SLOP] Sloppers"
    assert "#0000FF" in fields["Club details"]
    assert "Player" in fields["Roster"]
    assert "Goal" in fields["Badges"]
